#!/usr/bin/env python3
"""Enrich Discogs collection CSV exports with release style and genre metadata.

The script treats an enriched CSV as the durable master file. A new Discogs
collection export can be merged into that master, then missing style and genre
values are filled from Discogs release metadata.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import json
import os
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


STYLE_COLUMN = "Style"
GENRE_COLUMN = "Genre"
STYLE_SOURCE_COLUMN = "Style Source"
STYLE_STATUS_COLUMN = "Style Status"
STYLE_NOTES_COLUMN = "Style Notes"
GENRE_NOTES_COLUMN = "Genre Notes"
UPDATED_AT_COLUMN = "Updated At"
LEGACY_UPDATED_AT_COLUMN = "Style Updated At"
RELEASE_ID_COLUMN = "release_id"
DEFAULT_COLLECTION_DIRECTORY = Path("collection")
DEFAULT_MASTER_PATH = DEFAULT_COLLECTION_DIRECTORY / "enriched-collection.csv"
DEFAULT_CACHE_FILENAME = "processing.cache.json"
DEFAULT_INPUT_DIRECTORY = Path("export")
DEFAULT_PROCESSED_DIRECTORY = Path("processed")
DEFAULT_REPORT_DIRECTORY = Path("reports")
DEFAULT_USER_AGENT = "DiscogsStyleEnricher/1.0 +https://www.discogs.com"
DISCOGS_API_ROOT = "https://api.discogs.com"
LOOKUP_CACHE_SCHEMA_VERSION = 2
LOOKUP_CACHE_RECORD_TYPE = "discogs_release_metadata"
ENRICHMENT_COLUMNS = (
    STYLE_COLUMN,
    GENRE_COLUMN,
    STYLE_NOTES_COLUMN,
    GENRE_NOTES_COLUMN,
    UPDATED_AT_COLUMN,
    LEGACY_UPDATED_AT_COLUMN,
    STYLE_SOURCE_COLUMN,
    STYLE_STATUS_COLUMN,
)
CSV_ENRICHMENT_COLUMNS = (
    STYLE_COLUMN,
    GENRE_COLUMN,
    STYLE_NOTES_COLUMN,
    GENRE_NOTES_COLUMN,
    UPDATED_AT_COLUMN,
)
CSV_OMITTED_ENRICHMENT_COLUMNS = (
    STYLE_SOURCE_COLUMN,
    STYLE_STATUS_COLUMN,
    LEGACY_UPDATED_AT_COLUMN,
)
REQUIRED_DISCOGS_EXPORT_COLUMNS = (
    "Catalog#",
    "Artist",
    "Title",
    "Label",
    "Format",
    "Rating",
    "Released",
    RELEASE_ID_COLUMN,
    "CollectionFolder",
    "Date Added",
    "Collection Media Condition",
    "Collection Sleeve Condition",
    "Collection Notes",
)
DEFAULT_MAX_WORKERS = 3
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.0
DISCOGS_RATE_LIMIT_WINDOW_SECONDS = 60.0
DISCOGS_RATE_LIMIT_SAFETY_MARGIN = 2
DISCOGS_AUTHENTICATED_RATE_LIMIT = 60
DISCOGS_UNAUTHENTICATED_RATE_LIMIT = 25
MAX_RETRIES = 3


@dataclass(frozen=True)
class MetadataFieldLookup:
    values: tuple[str, ...]
    source: str
    status: str
    notes: str


@dataclass(frozen=True)
class ReleaseMetadataLookup:
    release_id: str
    looked_up_at: str
    master_id: int
    style: MetadataFieldLookup
    genre: MetadataFieldLookup


@dataclass(frozen=True)
class EnrichmentSummary:
    total_rows: int
    filled_style_count: int
    filled_genre_count: int
    preserved_style_count: int
    preserved_genre_count: int
    blank_count: int
    error_count: int
    not_sure_release_ids: tuple[str, ...]


@dataclass(frozen=True)
class RunSummary:
    input_rows: int
    master_rows_before: int
    output_rows: int
    appended_rows: int
    filled_style_count: int
    filled_genre_count: int
    preserved_style_count: int
    preserved_genre_count: int
    blank_count: int
    error_count: int
    not_sure_release_ids: tuple[str, ...]
    output_path: Path
    report_path: Path
    cache_path: Path
    processed_export_path: Path | None


class ProgressReporter:
    def __init__(
        self,
        stream: TextIO | None = None,
        width: int = 24,
        label: str = "Enriching rows",
        enabled: bool | None = None,
    ) -> None:
        self.stream = stream or sys.stderr
        self.width = max(1, width)
        self.label = label
        self.enabled = self.stream_is_terminal() if enabled is None else enabled
        self.current = 0
        self.total = 0
        self.started = False
        self.last_line_length = 0

    def stream_is_terminal(self) -> bool:
        is_terminal = getattr(self.stream, "isatty", None)
        return bool(is_terminal and is_terminal())

    def start(self, total: int) -> None:
        self.total = max(0, total)
        self.current = 0
        self.started = True
        self.render()

    def update(self, current: int) -> None:
        if not self.started:
            return
        if self.total:
            self.current = min(max(0, current), self.total)
        else:
            self.current = 0
        self.render()

    def finish(self) -> None:
        if not self.started:
            return
        self.started = False
        if not self.enabled:
            return
        self.stream.write("\n")
        self.stream.flush()

    def render(self) -> None:
        if not self.enabled:
            return
        ratio = 1.0 if self.total == 0 else self.current / self.total
        filled_width = int(ratio * self.width)
        bar = "#" * filled_width + "-" * (self.width - filled_width)
        percentage = int(ratio * 100)
        line = f"\r{self.label} [{bar}] {self.current}/{self.total} {percentage}%"
        padding = " " * max(0, self.last_line_length - len(line))
        self.stream.write(line + padding)
        self.stream.flush()
        self.last_line_length = len(line)


class DiscogsRateLimiter:
    def __init__(
        self,
        fallback_request_interval_seconds: float,
        initial_rate_limit: int | None = None,
        window_seconds: float = DISCOGS_RATE_LIMIT_WINDOW_SECONDS,
        safety_margin: int = DISCOGS_RATE_LIMIT_SAFETY_MARGIN,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.fallback_request_interval_seconds = max(0.0, fallback_request_interval_seconds)
        self.window_seconds = max(1.0, window_seconds)
        self.safety_margin = max(0, safety_margin)
        self.now = now
        self.sleep = sleep
        self.lock = threading.Lock()
        self.request_timestamps: deque[float] = deque()
        self.rate_limit = initial_rate_limit if initial_rate_limit and initial_rate_limit > 0 else None
        self.minimum_interval_seconds = self.calculate_minimum_interval()
        self.next_request_time = 0.0
        self.paused_until = 0.0

    def calculate_minimum_interval(self) -> float:
        if not self.rate_limit:
            return self.fallback_request_interval_seconds
        effective_limit = max(1, self.rate_limit - self.safety_margin)
        header_interval_seconds = self.window_seconds / effective_limit
        return max(self.fallback_request_interval_seconds, header_interval_seconds)

    def wait_before_request(self) -> None:
        while True:
            with self.lock:
                current_time = self.now()
                self.prune_old_timestamps(current_time)
                wait_seconds = self.next_wait_seconds(current_time)
                if wait_seconds <= 0:
                    self.request_timestamps.append(current_time)
                    self.next_request_time = current_time + self.minimum_interval_seconds
                    return
            self.sleep(wait_seconds)

    def next_wait_seconds(self, current_time: float) -> float:
        wait_seconds = 0.0
        if self.paused_until > current_time:
            wait_seconds = max(wait_seconds, self.paused_until - current_time)
        if self.next_request_time > current_time:
            wait_seconds = max(wait_seconds, self.next_request_time - current_time)

        if self.rate_limit:
            effective_limit = max(1, self.rate_limit - self.safety_margin)
            if len(self.request_timestamps) >= effective_limit:
                oldest_request_time = self.request_timestamps[0]
                wait_seconds = max(
                    wait_seconds,
                    oldest_request_time + self.window_seconds - current_time + 0.01,
                )

        return wait_seconds

    def prune_old_timestamps(self, current_time: float) -> None:
        oldest_allowed_time = current_time - self.window_seconds
        while self.request_timestamps and self.request_timestamps[0] <= oldest_allowed_time:
            self.request_timestamps.popleft()

    def update_from_headers(self, headers: object) -> None:
        rate_limit = parse_int_header(headers, "x-discogs-ratelimit")
        remaining = parse_int_header(headers, "x-discogs-ratelimit-remaining")
        with self.lock:
            if rate_limit and rate_limit > 0:
                self.rate_limit = rate_limit
                self.minimum_interval_seconds = self.calculate_minimum_interval()
            if remaining is not None and remaining <= self.safety_margin:
                current_time = self.now()
                if self.request_timestamps:
                    self.paused_until = max(
                        self.paused_until,
                        self.request_timestamps[0] + self.window_seconds + 0.25,
                    )
                else:
                    self.paused_until = max(self.paused_until, current_time + self.window_seconds)

    def sleep_for_retry_after(self, retry_after_seconds: float) -> None:
        retry_after_seconds = max(0.0, retry_after_seconds)
        with self.lock:
            self.paused_until = max(self.paused_until, self.now() + retry_after_seconds)
        self.sleep(retry_after_seconds)


def parse_int_header(headers: object, name: str) -> int | None:
    value = get_header_value(headers, name)
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def get_header_value(headers: object, name: str) -> object | None:
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is not None:
            return value
        return getter(name.lower())
    if isinstance(headers, Mapping):
        return headers.get(name) or headers.get(name.lower())
    return None


def parse_retry_after_seconds(headers: object) -> float | None:
    value = get_header_value(headers, "Retry-After")
    if value is None:
        return None
    clean_value = str(value).strip()
    try:
        return max(0.0, float(clean_value))
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(clean_value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=dt.UTC)
        return max(0.0, (retry_at - dt.datetime.now(dt.UTC)).total_seconds())


def default_discogs_rate_limit(token: str) -> int:
    if token:
        return DISCOGS_AUTHENTICATED_RATE_LIMIT
    return DISCOGS_UNAUTHENTICATED_RATE_LIMIT


def read_csv_file(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as input_file:
        reader = csv.DictReader(input_file)
        return list(reader), list(reader.fieldnames or [])


def validate_discogs_export_fieldnames(fieldnames: Sequence[str]) -> None:
    if not fieldnames:
        raise ValueError("export CSV is missing a header row")

    duplicate_fieldnames = sorted(
        fieldname
        for fieldname in set(fieldnames)
        if fieldnames.count(fieldname) > 1
    )
    if duplicate_fieldnames:
        raise ValueError(f"export CSV header has duplicate columns: {', '.join(duplicate_fieldnames)}")

    missing_fieldnames = [
        fieldname
        for fieldname in REQUIRED_DISCOGS_EXPORT_COLUMNS
        if fieldname not in fieldnames
    ]
    if missing_fieldnames:
        raise ValueError(
            "export CSV header is missing required Discogs export columns: "
            + ", ".join(missing_fieldnames)
        )


def find_single_csv_export(input_directory: Path) -> Path:
    if not input_directory.exists():
        raise FileNotFoundError(f"input folder does not exist: {input_directory}")
    if not input_directory.is_dir():
        raise NotADirectoryError(f"input path is not a folder: {input_directory}")

    csv_paths = sorted(
        path
        for path in input_directory.iterdir()
        if path.is_file() and path.suffix.lower() == ".csv"
    )
    if not csv_paths:
        raise FileNotFoundError(f"no CSV export found in input folder: {input_directory}")
    if len(csv_paths) > 1:
        csv_names = ", ".join(path.name for path in csv_paths)
        raise ValueError(
            f"expected exactly one CSV export in {input_directory}, found {len(csv_paths)}: {csv_names}"
        )
    return csv_paths[0]


def write_csv_file(path: Path, fieldnames: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        newline="",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as temporary_file:
        writer = csv.DictWriter(temporary_file, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
        temporary_path = Path(temporary_file.name)
    temporary_path.replace(path)


def ensure_processed_export_target_available(export_path: Path, processed_directory: Path) -> Path:
    processed_path = processed_directory / export_path.name
    if processed_path.exists():
        raise FileExistsError(f"processed export already exists: {processed_path}")
    return processed_path


def move_processed_export(export_path: Path, processed_directory: Path) -> Path:
    processed_path = ensure_processed_export_target_available(export_path, processed_directory)
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.replace(processed_path)
    return processed_path


def build_output_fieldnames(input_fieldnames: Sequence[str]) -> list[str]:
    unique_fieldnames = [
        fieldname
        for fieldname in dict.fromkeys(input_fieldnames)
        if fieldname not in CSV_OMITTED_ENRICHMENT_COLUMNS
    ]
    output_fieldnames = [
        fieldname
        for fieldname in unique_fieldnames
        if fieldname not in CSV_ENRICHMENT_COLUMNS
    ]
    insertion_index = find_enrichment_insert_index(unique_fieldnames, output_fieldnames)
    return [
        *output_fieldnames[:insertion_index],
        *CSV_ENRICHMENT_COLUMNS,
        *output_fieldnames[insertion_index:],
    ]


def find_enrichment_insert_index(unique_fieldnames: Sequence[str], output_fieldnames: Sequence[str]) -> int:
    if STYLE_COLUMN in unique_fieldnames:
        return sum(1 for fieldname in unique_fieldnames[: unique_fieldnames.index(STYLE_COLUMN)] if fieldname in output_fieldnames)
    if "Released" in output_fieldnames:
        return output_fieldnames.index("Released") + 1
    return len(output_fieldnames)


def normalize_row(row: Mapping[str, str], fieldnames: Sequence[str]) -> dict[str, str]:
    normalized_row = {field: str(row.get(field, "") or "") for field in fieldnames}
    if (
        UPDATED_AT_COLUMN in fieldnames
        and not normalized_row.get(UPDATED_AT_COLUMN, "").strip()
        and LEGACY_UPDATED_AT_COLUMN in row
    ):
        normalized_row[UPDATED_AT_COLUMN] = str(row.get(LEGACY_UPDATED_AT_COLUMN, "") or "")
    return normalized_row


def original_data_fields(fieldnames: Sequence[str]) -> tuple[str, ...]:
    return tuple(field for field in fieldnames if field not in ENRICHMENT_COLUMNS)


def row_signature(row: Mapping[str, str], fieldnames: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(row.get(field, "") or "") for field in original_data_fields(fieldnames))


def merge_master_and_export_rows(
    master_rows: Sequence[Mapping[str, str]],
    export_rows: Sequence[Mapping[str, str]],
    output_fieldnames: Sequence[str],
) -> tuple[list[dict[str, str]], int]:
    merged_rows = [normalize_row(row, output_fieldnames) for row in master_rows]
    identity_fieldnames = merge_identity_fieldnames(export_rows, output_fieldnames)
    known_signatures = {row_signature(row, identity_fieldnames) for row in merged_rows}
    appended_count = 0

    for export_row in export_rows:
        normalized_export_row = normalize_row(export_row, output_fieldnames)
        signature = row_signature(normalized_export_row, identity_fieldnames)
        if signature in known_signatures:
            continue
        known_signatures.add(signature)
        merged_rows.append(normalized_export_row)
        appended_count += 1

    return merged_rows, appended_count


def merge_identity_fieldnames(
    export_rows: Sequence[Mapping[str, str]],
    output_fieldnames: Sequence[str],
) -> tuple[str, ...]:
    if not export_rows:
        return original_data_fields(output_fieldnames)
    export_fieldnames = tuple(export_rows[0].keys())
    return tuple(field for field in export_fieldnames if field in output_fieldnames and field not in ENRICHMENT_COLUMNS)


def clean_discogs_values(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    cleaned_values = tuple(
        dict.fromkeys(str(value).strip() for value in values if str(value or "").strip())
    )
    return cleaned_values


def parse_styles_from_api_payload(payload: Mapping[str, object] | None) -> tuple[str, ...]:
    if not payload:
        return ()
    return clean_discogs_values(payload.get("styles"))


def parse_genres_from_api_payload(payload: Mapping[str, object] | None) -> tuple[str, ...]:
    if not payload:
        return ()
    return clean_discogs_values(payload.get("genres"))


def parse_master_id(payload: Mapping[str, object] | None) -> int:
    if not payload:
        return 0
    try:
        return int(payload.get("master_id") or 0)
    except (TypeError, ValueError):
        return 0


def release_api_url(release_id: str) -> str:
    return f"{DISCOGS_API_ROOT}/releases/{release_id}"


def master_api_url(master_id: int) -> str:
    return f"{DISCOGS_API_ROOT}/masters/{master_id}"


def resolve_release_metadata(
    release_id: str,
    get_json: Callable[[str], Mapping[str, object] | None],
) -> ReleaseMetadataLookup:
    release_notes: list[str] = []
    release_payload = fetch_json_or_note(
        url=release_api_url(release_id),
        get_json=get_json,
        source_name="api_release",
        notes=release_notes,
    )
    release_styles = parse_styles_from_api_payload(release_payload)
    release_genres = parse_genres_from_api_payload(release_payload)
    master_id = parse_master_id(release_payload)
    master_payload: Mapping[str, object] | None = None
    master_notes: list[str] = []

    if master_id > 0 and (not release_styles or not release_genres):
        master_payload = fetch_json_or_note(
            url=master_api_url(master_id),
            get_json=get_json,
            source_name="api_master",
            notes=master_notes,
        )

    master_styles = parse_styles_from_api_payload(master_payload)
    master_genres = parse_genres_from_api_payload(master_payload)
    return ReleaseMetadataLookup(
        release_id=release_id,
        looked_up_at="",
        master_id=master_id,
        style=build_metadata_field_lookup(
            release_values=release_styles,
            master_values=master_styles,
            master_id=master_id,
            release_notes=release_notes,
            master_notes=master_notes,
            missing_note="no explicit styles found",
        ),
        genre=build_metadata_field_lookup(
            release_values=release_genres,
            master_values=master_genres,
            master_id=master_id,
            release_notes=release_notes,
            master_notes=master_notes,
            missing_note="no explicit genres found",
        ),
    )


def build_metadata_field_lookup(
    release_values: tuple[str, ...],
    master_values: tuple[str, ...],
    master_id: int,
    release_notes: Sequence[str],
    master_notes: Sequence[str],
    missing_note: str,
) -> MetadataFieldLookup:
    if release_values:
        return MetadataFieldLookup(release_values, "api_release", "filled", "")
    if master_values:
        return MetadataFieldLookup(master_values, "api_master", "filled", join_notes([f"master_id={master_id}", *master_notes]))

    source = "api_release+api_master" if master_id > 0 else "api_release"
    notes = [*release_notes]
    if master_id > 0:
        notes.append(f"master_id={master_id}")
        notes.extend(master_notes)
    status = "error" if release_notes or master_notes else "blank"
    return MetadataFieldLookup((), source, status, join_notes([*notes, missing_note]))


def fetch_json_or_note(
    url: str,
    get_json: Callable[[str], Mapping[str, object] | None],
    source_name: str,
    notes: list[str],
) -> Mapping[str, object] | None:
    try:
        return get_json(url)
    except Exception as error:  # noqa: BLE001 - record uncertainty without aborting the batch.
        notes.append(format_source_failure(source_name, error))
        return None


def format_source_failure(source_name: str, error: Exception) -> str:
    return f"{source_name} failed: {type(error).__name__}: {error}"


def join_notes(notes: Iterable[str]) -> str:
    return "; ".join(note for note in notes if note)


def update_missing_metadata(
    rows: list[dict[str, str]],
    lookup_metadata: Callable[[str], ReleaseMetadataLookup],
    updated_at: str,
    refresh_existing: bool = False,
    progress: ProgressReporter | None = None,
    max_workers: int = 1,
) -> EnrichmentSummary:
    filled_style_count = 0
    filled_genre_count = 0
    preserved_style_count = 0
    preserved_genre_count = 0
    blank_count = 0
    error_count = 0
    not_sure_release_ids: list[str] = []
    lookup_futures, lookup_executor = start_metadata_lookup_futures(
        rows=rows,
        lookup_metadata=lookup_metadata,
        refresh_existing=refresh_existing,
        max_workers=max_workers,
    )

    if progress:
        progress.start(len(rows))
    try:
        for row_number, row in enumerate(rows, start=1):
            try:
                ensure_row_enrichment_columns(row)
                release_id = str(row.get(RELEASE_ID_COLUMN, "") or "").strip()
                style_needs_lookup = refresh_existing or not row.get(STYLE_COLUMN, "").strip()
                genre_needs_lookup = refresh_existing or not row.get(GENRE_COLUMN, "").strip()
                if not style_needs_lookup:
                    preserved_style_count += 1
                if not genre_needs_lookup:
                    preserved_genre_count += 1
                if not style_needs_lookup and not genre_needs_lookup:
                    continue

                if not release_id:
                    if style_needs_lookup:
                        set_blank_metadata_field(row, STYLE_COLUMN, STYLE_NOTES_COLUMN, "missing release_id", updated_at)
                    if genre_needs_lookup:
                        set_blank_metadata_field(row, GENRE_COLUMN, GENRE_NOTES_COLUMN, "missing release_id", updated_at)
                    blank_count += 1
                    continue

                lookup = resolve_metadata_lookup(
                    release_id=release_id,
                    lookup_metadata=lookup_metadata,
                    lookup_futures=lookup_futures,
                )
                row_has_blank = False
                row_has_error = False

                if style_needs_lookup:
                    style_status = apply_metadata_field_lookup(
                        row=row,
                        value_column=STYLE_COLUMN,
                        notes_column=STYLE_NOTES_COLUMN,
                        field_lookup=lookup.style,
                        updated_at=updated_at,
                    )
                    if style_status == "filled":
                        filled_style_count += 1
                    elif style_status == "error":
                        row_has_error = True
                    else:
                        row_has_blank = True

                if genre_needs_lookup:
                    genre_status = apply_metadata_field_lookup(
                        row=row,
                        value_column=GENRE_COLUMN,
                        notes_column=GENRE_NOTES_COLUMN,
                        field_lookup=lookup.genre,
                        updated_at=updated_at,
                    )
                    if genre_status == "filled":
                        filled_genre_count += 1
                    elif genre_status == "error":
                        row_has_error = True
                    else:
                        row_has_blank = True

                if row_has_error:
                    error_count += 1
                    not_sure_release_ids.append(release_id)
                elif row_has_blank:
                    blank_count += 1
                    not_sure_release_ids.append(release_id)
            finally:
                if progress:
                    progress.update(row_number)
    finally:
        if progress:
            progress.finish()
        if lookup_executor:
            lookup_executor.shutdown(wait=True)

    return EnrichmentSummary(
        total_rows=len(rows),
        filled_style_count=filled_style_count,
        filled_genre_count=filled_genre_count,
        preserved_style_count=preserved_style_count,
        preserved_genre_count=preserved_genre_count,
        blank_count=blank_count,
        error_count=error_count,
        not_sure_release_ids=tuple(not_sure_release_ids),
    )


def start_metadata_lookup_futures(
    rows: Sequence[Mapping[str, str]],
    lookup_metadata: Callable[[str], ReleaseMetadataLookup],
    refresh_existing: bool,
    max_workers: int,
) -> tuple[dict[str, Future[ReleaseMetadataLookup]], ThreadPoolExecutor | None]:
    if max_workers <= 1:
        return {}, None

    release_ids = release_ids_needing_lookup(rows, refresh_existing)
    if not release_ids:
        return {}, None

    executor = ThreadPoolExecutor(max_workers=max_workers)
    return {
        release_id: executor.submit(lookup_metadata, release_id)
        for release_id in release_ids
    }, executor


def release_ids_needing_lookup(
    rows: Sequence[Mapping[str, str]],
    refresh_existing: bool,
) -> tuple[str, ...]:
    release_ids: list[str] = []
    seen_release_ids: set[str] = set()
    for row in rows:
        release_id = str(row.get(RELEASE_ID_COLUMN, "") or "").strip()
        if not release_id or release_id in seen_release_ids:
            continue
        if row_needs_metadata_lookup(row, refresh_existing):
            seen_release_ids.add(release_id)
            release_ids.append(release_id)
    return tuple(release_ids)


def row_needs_metadata_lookup(row: Mapping[str, str], refresh_existing: bool) -> bool:
    if refresh_existing:
        return True
    return not row.get(STYLE_COLUMN, "").strip() or not row.get(GENRE_COLUMN, "").strip()


def resolve_metadata_lookup(
    release_id: str,
    lookup_metadata: Callable[[str], ReleaseMetadataLookup],
    lookup_futures: Mapping[str, Future[ReleaseMetadataLookup]],
) -> ReleaseMetadataLookup:
    lookup_future = lookup_futures.get(release_id)
    if lookup_future:
        return lookup_future.result()
    return lookup_metadata(release_id)


def ensure_row_enrichment_columns(row: dict[str, str]) -> None:
    for column_name in CSV_ENRICHMENT_COLUMNS:
        row.setdefault(column_name, "")


def apply_metadata_field_lookup(
    row: dict[str, str],
    value_column: str,
    notes_column: str,
    field_lookup: MetadataFieldLookup,
    updated_at: str,
) -> str:
    if field_lookup.values:
        row[value_column] = ", ".join(field_lookup.values)
        row[notes_column] = field_lookup.notes
        row[UPDATED_AT_COLUMN] = updated_at
        return "filled"
    row[value_column] = ""
    row[notes_column] = field_lookup.notes
    row[UPDATED_AT_COLUMN] = updated_at
    return field_lookup.status


def set_blank_metadata(row: dict[str, str], notes: str, updated_at: str) -> None:
    set_blank_metadata_field(row, STYLE_COLUMN, STYLE_NOTES_COLUMN, notes, updated_at)
    set_blank_metadata_field(row, GENRE_COLUMN, GENRE_NOTES_COLUMN, notes, updated_at)


def set_blank_metadata_field(
    row: dict[str, str],
    value_column: str,
    notes_column: str,
    notes: str,
    updated_at: str,
) -> None:
    row[value_column] = ""
    row[notes_column] = notes
    row[UPDATED_AT_COLUMN] = updated_at


def load_lookup_cache(path: Path) -> dict[str, ReleaseMetadataLookup]:
    if not path.exists():
        return {}
    cache_payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(cache_payload, dict)
        or cache_payload.get("schema_version") != LOOKUP_CACHE_SCHEMA_VERSION
        or cache_payload.get("record_type") != LOOKUP_CACHE_RECORD_TYPE
        or not isinstance(cache_payload.get("records"), dict)
    ):
        raise ValueError("unsupported cache format; delete the old cache or choose a new --cache path")

    records = cache_payload["records"]
    return {
        str(release_id): release_metadata_from_cache_record(str(release_id), record)
        for release_id, record in records.items()
        if isinstance(record, Mapping)
    }


def release_metadata_from_cache_record(release_id: str, record: Mapping[str, object]) -> ReleaseMetadataLookup:
    return ReleaseMetadataLookup(
        release_id=str(record.get("release_id") or release_id),
        looked_up_at=str(record.get("looked_up_at") or ""),
        master_id=parse_cached_master_id(record.get("master_id")),
        style=metadata_field_from_cache_record(record.get("style")),
        genre=metadata_field_from_cache_record(record.get("genre")),
    )


def parse_cached_master_id(master_id: object) -> int:
    try:
        return int(master_id or 0)
    except (TypeError, ValueError):
        return 0


def metadata_field_from_cache_record(record: object) -> MetadataFieldLookup:
    if not isinstance(record, Mapping):
        record = {}
    return MetadataFieldLookup(
        values=clean_discogs_values(record.get("values")),
        source=str(record.get("source") or ""),
        status=str(record.get("status") or ""),
        notes=str(record.get("notes") or ""),
    )


def save_lookup_cache(path: Path, cache: Mapping[str, ReleaseMetadataLookup]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable_cache = {
        "schema_version": LOOKUP_CACHE_SCHEMA_VERSION,
        "record_type": LOOKUP_CACHE_RECORD_TYPE,
        "records": {
            release_id: {
                "release_id": lookup.release_id or release_id,
                "looked_up_at": lookup.looked_up_at,
                "master_id": lookup.master_id,
                "style": metadata_field_to_cache_record(lookup.style),
                "genre": metadata_field_to_cache_record(lookup.genre),
            }
            for release_id, lookup in sorted(cache.items())
        },
    }
    path.write_text(json.dumps(serializable_cache, ensure_ascii=False, indent=2), encoding="utf-8")


def metadata_field_to_cache_record(field_lookup: MetadataFieldLookup) -> dict[str, object]:
    return {
        "values": list(field_lookup.values),
        "source": field_lookup.source,
        "status": field_lookup.status,
        "notes": field_lookup.notes,
    }


def make_cached_lookup(
    cache: dict[str, ReleaseMetadataLookup],
    cache_path: Path,
    get_json: Callable[[str], Mapping[str, object] | None],
) -> Callable[[str], ReleaseMetadataLookup]:
    cache_lock = threading.Lock()

    def lookup_metadata(release_id: str) -> ReleaseMetadataLookup:
        with cache_lock:
            cached_lookup = cache.get(release_id)
            if cached_lookup and not release_metadata_has_error(cached_lookup):
                return cached_lookup

        try:
            lookup = replace(
                resolve_release_metadata(
                    release_id,
                    get_json=get_json,
                ),
                looked_up_at=utc_timestamp(),
            )
        except Exception as error:  # noqa: BLE001 - keep batch runs alive and report uncertainty.
            lookup = ReleaseMetadataLookup(
                release_id=release_id,
                looked_up_at=utc_timestamp(),
                master_id=0,
                style=MetadataFieldLookup(
                    values=(),
                    source="error",
                    status="error",
                    notes=f"{type(error).__name__}: {error}",
                ),
                genre=MetadataFieldLookup(
                    values=(),
                    source="error",
                    status="error",
                    notes=f"{type(error).__name__}: {error}",
                ),
            )

        with cache_lock:
            cached_lookup = cache.get(release_id)
            if cached_lookup and not release_metadata_has_error(cached_lookup):
                return cached_lookup
            cache[release_id] = lookup
            save_lookup_cache(cache_path, cache)
        return lookup

    return lookup_metadata


def release_metadata_has_error(lookup: ReleaseMetadataLookup) -> bool:
    return lookup.style.status == "error" or lookup.genre.status == "error"


def make_http_json_getter(
    user_agent: str,
    token: str,
    timeout_seconds: int,
    request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
) -> Callable[[str], Mapping[str, object] | None]:
    rate_limiter = DiscogsRateLimiter(
        fallback_request_interval_seconds=request_interval_seconds,
        initial_rate_limit=default_discogs_rate_limit(token),
    )

    def get_json(url: str) -> Mapping[str, object] | None:
        body = http_get(
            url,
            user_agent=user_agent,
            token=token,
            timeout_seconds=timeout_seconds,
            accept="application/json",
            rate_limiter=rate_limiter,
        )
        if not body:
            return None
        return json.loads(body)

    return get_json


def http_get(
    url: str,
    user_agent: str,
    token: str,
    timeout_seconds: int,
    accept: str,
    rate_limiter: DiscogsRateLimiter | None = None,
) -> str | None:
    last_error = ""
    for attempt_number in range(1, MAX_RETRIES + 1):
        try:
            if rate_limiter:
                rate_limiter.wait_before_request()
            request = Request(url, headers=build_headers(user_agent=user_agent, token=token, accept=accept))
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
                if rate_limiter:
                    rate_limiter.update_from_headers(response.headers)
                return body
        except HTTPError as error:
            last_error = f"HTTP {error.code}"
            error_headers = getattr(error, "headers", None)
            if rate_limiter and error_headers:
                rate_limiter.update_from_headers(error_headers)
            if error.code == 429:
                retry_after_seconds = parse_retry_after_seconds(error_headers) or 65
                if rate_limiter:
                    rate_limiter.sleep_for_retry_after(retry_after_seconds)
                else:
                    time.sleep(retry_after_seconds)
            elif 500 <= error.code < 600:
                time.sleep(5 * attempt_number)
            else:
                break
        except (TimeoutError, URLError) as error:
            last_error = f"{type(error).__name__}: {error}"
            time.sleep(5 * attempt_number)
    raise RuntimeError(f"request failed for {url}: {last_error or 'unknown error'}")


def build_headers(user_agent: str, token: str, accept: str) -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": user_agent}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    return headers


def run_enrichment(args: argparse.Namespace) -> RunSummary:
    export_rows, export_fieldnames = read_csv_file(args.export)
    validate_discogs_export_fieldnames(export_fieldnames)
    move_processed_export_enabled = getattr(args, "move_processed_export", False)
    processed_directory = getattr(args, "processed_dir", DEFAULT_PROCESSED_DIRECTORY)
    if move_processed_export_enabled:
        ensure_processed_export_target_available(args.export, processed_directory)

    if args.master.exists():
        master_rows, master_fieldnames = read_csv_file(args.master)
    else:
        master_rows, master_fieldnames = [], export_fieldnames

    output_fieldnames = build_output_fieldnames([*master_fieldnames, *export_fieldnames])
    merged_rows, appended_rows = merge_master_and_export_rows(
        master_rows=master_rows,
        export_rows=export_rows,
        output_fieldnames=output_fieldnames,
    )
    cache = load_lookup_cache(args.cache)
    lookup_metadata = make_cached_lookup(
        cache=cache,
        cache_path=args.cache,
        get_json=make_http_json_getter(
            args.user_agent,
            args.discogs_token,
            args.timeout_seconds,
            args.request_interval_seconds,
        ),
    )
    updated_at = utc_timestamp()
    enrichment_summary = update_missing_metadata(
        rows=merged_rows,
        lookup_metadata=lookup_metadata,
        updated_at=updated_at,
        refresh_existing=args.refresh_existing,
        progress=ProgressReporter() if getattr(args, "progress", False) else None,
        max_workers=getattr(args, "max_workers", DEFAULT_MAX_WORKERS),
    )
    write_csv_file(args.output, output_fieldnames, merged_rows)
    run_summary = RunSummary(
        input_rows=len(export_rows),
        master_rows_before=len(master_rows),
        output_rows=len(merged_rows),
        appended_rows=appended_rows,
        filled_style_count=enrichment_summary.filled_style_count,
        filled_genre_count=enrichment_summary.filled_genre_count,
        preserved_style_count=enrichment_summary.preserved_style_count,
        preserved_genre_count=enrichment_summary.preserved_genre_count,
        blank_count=enrichment_summary.blank_count,
        error_count=enrichment_summary.error_count,
        not_sure_release_ids=enrichment_summary.not_sure_release_ids,
        output_path=args.output,
        report_path=args.report,
        cache_path=args.cache,
        processed_export_path=None,
    )
    write_report(args.report, run_summary, merged_rows)
    if move_processed_export_enabled and run_summary.error_count == 0:
        processed_export_path = move_processed_export(args.export, processed_directory)
        run_summary = replace(run_summary, processed_export_path=processed_export_path)
    return run_summary


def utc_timestamp() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def readable_timestamp() -> str:
    return dt.datetime.now().replace(microsecond=0).strftime("%Y-%m-%d_%H-%M-%S")


def default_report_path(output_path: Path) -> Path:
    return DEFAULT_REPORT_DIRECTORY / f"{output_path.stem}_{readable_timestamp()}_report.txt"


def default_cache_path(output_path: Path) -> Path:
    return output_path.parent / DEFAULT_CACHE_FILENAME


def write_report(path: Path, summary: RunSummary, rows: Sequence[Mapping[str, str]]) -> None:
    rows_by_release_id = {str(row.get(RELEASE_ID_COLUMN, "") or ""): row for row in rows}
    lines = [
        "Discogs style and genre enrichment report",
        f"Input export rows: {summary.input_rows}",
        f"Master rows before: {summary.master_rows_before}",
        f"Output rows: {summary.output_rows}",
        f"Appended rows: {summary.appended_rows}",
        f"Filled missing styles: {summary.filled_style_count}",
        f"Filled missing genres: {summary.filled_genre_count}",
        f"Preserved existing styles: {summary.preserved_style_count}",
        f"Preserved existing genres: {summary.preserved_genre_count}",
        f"Left blank / not sure: {summary.blank_count}",
        f"Lookup errors: {summary.error_count}",
        f"Output: {summary.output_path}",
        f"Cache: {summary.cache_path}",
        "",
        "Items left blank / not sure:",
    ]
    if summary.not_sure_release_ids:
        lines.extend(format_not_sure_line(release_id, rows_by_release_id.get(release_id, {})) for release_id in summary.not_sure_release_ids)
    else:
        lines.append("- None")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def format_not_sure_line(release_id: str, row: Mapping[str, str]) -> str:
    artist = row.get("Artist", "")
    title = row.get("Title", "")
    notes = join_notes(
        [
            format_missing_metadata_fields(row),
            format_field_note("style", row.get(STYLE_NOTES_COLUMN, "")),
            format_field_note("genre", row.get(GENRE_NOTES_COLUMN, "")),
        ]
    )
    return f"- {release_id}: {artist} - {title} ({notes})"


def format_missing_metadata_fields(row: Mapping[str, str]) -> str:
    missing_fields = [
        field_name
        for field_name, column_name in (("Style", STYLE_COLUMN), ("Genre", GENRE_COLUMN))
        if not str(row.get(column_name, "") or "").strip()
    ]
    if not missing_fields:
        return ""
    return f"missing: {', '.join(missing_fields)}"


def format_field_note(field_name: str, note: str | None) -> str:
    clean_note = str(note or "").strip()
    if not clean_note:
        return ""
    return f"{field_name}: {clean_note}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, help="Specific Discogs collection export CSV. If omitted, exactly one CSV is read from --input-dir.")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIRECTORY, help="Folder containing one new Discogs export CSV. Defaults to export.")
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED_DIRECTORY, help="Folder where default-folder exports are moved after successful runs. Defaults to processed.")
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER_PATH, help="Existing enriched master CSV. Created if missing. Defaults to collection/enriched-collection.csv.")
    parser.add_argument("--output", type=Path, help="Output enriched master CSV. Defaults to --master.")
    parser.add_argument("--report", type=Path, help="Text report path. Defaults to reports/<output-name>_<timestamp>_report.txt.")
    parser.add_argument("--cache", type=Path, help="Lookup cache JSON path. Defaults to processing.cache.json beside output CSV.")
    parser.add_argument("--discogs-token", default=os.environ.get("DISCOGS_TOKEN", ""), help="Optional Discogs personal access token. Defaults to DISCOGS_TOKEN.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent sent to Discogs.")
    parser.add_argument("--timeout-seconds", type=int, default=30, help="HTTP timeout per request.")
    parser.add_argument("--request-interval-seconds", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS, help="Minimum delay between Discogs requests. Defaults to header-aware throttling.")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="Maximum concurrent uncached Discogs lookups. Defaults to 3.")
    parser.add_argument("--refresh-existing", action="store_true", help="Replace existing Style and Genre values instead of preserving them.")
    parser.add_argument("--no-progress", action="store_false", dest="progress", help="Disable terminal progress output.")
    parsed_args = parser.parse_args(argv)
    if parsed_args.max_workers < 1:
        parser.error("--max-workers must be at least 1")
    if parsed_args.request_interval_seconds < 0:
        parser.error("--request-interval-seconds must be non-negative")
    parsed_args.move_processed_export = parsed_args.export is None
    parsed_args.export = parsed_args.export or find_single_csv_export(parsed_args.input_dir)
    parsed_args.output = parsed_args.output or parsed_args.master
    parsed_args.report = parsed_args.report or default_report_path(parsed_args.output)
    parsed_args.cache = parsed_args.cache or default_cache_path(parsed_args.output)
    return parsed_args


def print_summary(summary: RunSummary) -> None:
    print(f"Output: {summary.output_path}")
    print(f"Report: {summary.report_path}")
    print(f"Cache: {summary.cache_path}")
    if summary.processed_export_path:
        print(f"Processed export: {summary.processed_export_path}")
    print(f"Input export rows: {summary.input_rows}")
    print(f"Master rows before: {summary.master_rows_before}")
    print(f"Output rows: {summary.output_rows}")
    print(f"Appended rows: {summary.appended_rows}")
    print(f"Filled missing styles: {summary.filled_style_count}")
    print(f"Filled missing genres: {summary.filled_genre_count}")
    print(f"Preserved existing styles: {summary.preserved_style_count}")
    print(f"Preserved existing genres: {summary.preserved_genre_count}")
    print(f"Left blank / not sure: {summary.blank_count}")
    print(f"Lookup errors: {summary.error_count}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        summary = run_enrichment(args)
    except (FileNotFoundError, NotADirectoryError, FileExistsError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print_summary(summary)
    return 0 if summary.error_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
