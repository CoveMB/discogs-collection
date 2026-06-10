#!/usr/bin/env python3
"""Export TuneMyMusic-ready playlist CSVs from a playlist-mapped Discogs master."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from discogs_style_enricher import (
    DEFAULT_REQUEST_INTERVAL_SECONDS,
    DiscogsRateLimiter,
    http_get,
)
from shared.discogs_columns import RELEASE_ID_COLUMN
from shared.files import read_csv_file, write_csv_file, write_json_file
from shared.progress import ProgressReporter
from shared.reports import (
    format_report_section,
    format_report_title,
    print_report_section,
    timestamped_report_path,
    write_text_report,
)
from shared.text import split_unique_comma_separated


PLAYLISTS_COLUMN = "Playlists"
DEFAULT_INPUT_PATH = Path("collection/enriched-collection.csv")
DEFAULT_OUTPUT_DIRECTORY = Path("collection/playlists")
DEFAULT_CACHE_PATH = Path("collection/cache/playlist-tracks.cache.json")
DEFAULT_USER_AGENT = "DiscogsPlaylistExporter/1.0 +https://www.discogs.com"
DISCOGS_API_ROOT = "https://api.discogs.com"
TRACKLIST_CACHE_SCHEMA_VERSION = 1
TRACKLIST_CACHE_RECORD_TYPE = "discogs_release_tracklist"
TUNEMYMUSIC_COLUMNS = (
    "Release Id",
    "Album Name",
    "Track Number",
    "Track Name",
    "Artist Name",
    "Spotify Search Query",
)


@dataclass(frozen=True)
class DiscogsTrack:
    position: str
    title: str
    artist_name: str


@dataclass(frozen=True)
class ReleaseTracklistLookup:
    release_id: str
    artist_name: str
    album_name: str
    record_year: str
    tracks: tuple[DiscogsTrack, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class PlaylistExportFile:
    playlist_name: str
    path: Path
    row_count: int


ReleaseReportKey = tuple[str, str, str]
TrackReportKey = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class ReleaseReportEntry:
    key: ReleaseReportKey
    release_id: str
    artist_name: str
    album_name: str
    track_row_count: int


@dataclass(frozen=True)
class TrackReportEntry:
    key: TrackReportKey
    release_id: str
    artist_name: str
    album_name: str
    track_number: str
    track_name: str


@dataclass(frozen=True)
class PlaylistReleaseChange:
    playlist_name: str
    path: Path
    added_releases: tuple[ReleaseReportEntry, ...]
    removed_releases: tuple[ReleaseReportEntry, ...]
    added_tracks: tuple[TrackReportEntry, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlaylistExportSummary:
    input_rows: int
    playlist_count: int
    track_row_count: int
    fallback_row_count: int
    skipped_unassigned_count: int
    input_path: Path
    output_directory: Path
    report_path: Path
    playlist_files: tuple[PlaylistExportFile, ...]
    playlist_release_changes: tuple[PlaylistReleaseChange, ...]
    review_notes: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseExportRecord:
    row_number: int
    row: Mapping[str, str]
    lookup: ReleaseTracklistLookup | None


def release_api_url(release_id: str) -> str:
    return f"{DISCOGS_API_ROOT}/releases/{release_id}"


def export_playlist_csvs(
    input_path: Path,
    output_directory: Path,
    report_path: Path,
    lookup_tracklist: Callable[[Mapping[str, str]], ReleaseTracklistLookup],
    progress: ProgressReporter | None = None,
) -> PlaylistExportSummary:
    rows, fieldnames = read_csv_file(input_path)
    validate_input_fieldnames(fieldnames)
    records_by_playlist: dict[str, list[ReleaseExportRecord]] = {}
    review_notes: list[str] = []
    skipped_unassigned_count = 0

    if progress:
        progress.start(len(rows))
    try:
        for row_number, row in enumerate(rows, start=1):
            try:
                playlists = split_unique_comma_separated(row.get(PLAYLISTS_COLUMN, ""))
                if not playlists:
                    skipped_unassigned_count += 1
                    continue

                record = build_release_export_record(row_number, row, lookup_tracklist, review_notes)
                for playlist_name in playlists:
                    records_by_playlist.setdefault(playlist_name, []).append(record)
            finally:
                if progress:
                    progress.update(row_number)
    finally:
        if progress:
            progress.finish()

    playlist_files: list[PlaylistExportFile] = []
    playlist_release_changes: list[PlaylistReleaseChange] = []
    written_playlist_paths: set[Path] = set()
    track_row_count = 0
    fallback_row_count = 0
    playlist_paths = build_playlist_paths(records_by_playlist.keys(), output_directory)
    for playlist_name, records in records_by_playlist.items():
        output_rows, playlist_fallback_count = build_playlist_output_rows(records)
        output_path = playlist_paths[playlist_name]
        previous_rows, previous_notes = read_existing_playlist_rows_for_report(output_path, review_notes)
        playlist_release_changes.append(
            build_playlist_release_change(
                playlist_name=playlist_name,
                path=output_path,
                previous_rows=previous_rows,
                current_rows=output_rows,
                notes=previous_notes,
            )
        )
        write_csv_file(output_path, TUNEMYMUSIC_COLUMNS, output_rows)
        written_playlist_paths.add(output_path)
        playlist_files.append(
            PlaylistExportFile(
                playlist_name=playlist_name,
                path=output_path,
                row_count=len(output_rows),
            )
        )
        track_row_count += len(output_rows)
        fallback_row_count += playlist_fallback_count

    playlist_release_changes.extend(
        build_stale_playlist_release_changes(
            output_directory=output_directory,
            written_playlist_paths=written_playlist_paths,
            review_notes=review_notes,
        )
    )

    summary = PlaylistExportSummary(
        input_rows=len(rows),
        playlist_count=len(playlist_files),
        track_row_count=track_row_count,
        fallback_row_count=fallback_row_count,
        skipped_unassigned_count=skipped_unassigned_count,
        input_path=input_path,
        output_directory=output_directory,
        report_path=report_path,
        playlist_files=tuple(playlist_files),
        playlist_release_changes=tuple(playlist_release_changes),
        review_notes=tuple(review_notes),
    )
    write_report(report_path, summary)
    return summary


def validate_input_fieldnames(fieldnames: Sequence[str]) -> None:
    if PLAYLISTS_COLUMN not in fieldnames:
        raise ValueError("input CSV must contain a Playlists column; run scripts/discogs_playlist_mapper.py first")


def build_release_export_record(
    row_number: int,
    row: Mapping[str, str],
    lookup_tracklist: Callable[[Mapping[str, str]], ReleaseTracklistLookup],
    review_notes: list[str],
) -> ReleaseExportRecord:
    release_id = clean_cell(row.get(RELEASE_ID_COLUMN, ""))
    if not release_id:
        reason = "release_id is missing"
        review_notes.append(f"Row {row_number}: {reason}")
        return ReleaseExportRecord(row_number=row_number, row=row, lookup=None)

    try:
        lookup = lookup_tracklist(row)
    except Exception as error:  # noqa: BLE001 - export a reviewable fallback instead of dropping the row.
        reason = f"tracklist lookup failed: {type(error).__name__}: {error}"
        review_notes.append(f"Release ID {release_id}: {reason}")
        return ReleaseExportRecord(row_number=row_number, row=row, lookup=None)

    if not lookup.tracks:
        reason = first_note_or_default(lookup.notes, "no Discogs tracklist found")
        review_notes.append(f"Release ID {release_id}: {reason}")
        return ReleaseExportRecord(row_number=row_number, row=row, lookup=lookup)

    return ReleaseExportRecord(row_number=row_number, row=row, lookup=lookup)


def first_note_or_default(notes: Sequence[str], default: str) -> str:
    for note in notes:
        clean_note = clean_cell(note)
        if clean_note:
            return clean_note
    return default


def build_playlist_output_rows(
    records: Sequence[ReleaseExportRecord],
) -> tuple[list[dict[str, str]], int]:
    output_rows: list[dict[str, str]] = []
    fallback_count = 0
    for record in records:
        release_rows, release_fallback_count = build_release_output_rows(record)
        output_rows.extend(release_rows)
        fallback_count += release_fallback_count
    return output_rows, fallback_count


def build_release_output_rows(record: ReleaseExportRecord) -> tuple[list[dict[str, str]], int]:
    lookup = record.lookup
    if lookup and lookup.tracks:
        return (
            [
                build_tunemymusic_row(
                    track_number=track_index,
                    track=track,
                    row=record.row,
                    lookup=lookup,
                )
                for track_index, track in enumerate(lookup.tracks, start=1)
            ],
            0,
        )

    fallback_lookup = lookup or fallback_lookup_from_row(record.row)
    fallback_track = DiscogsTrack(
        position="",
        title=clean_cell(record.row.get("Title", "")) or fallback_lookup.album_name,
        artist_name=fallback_lookup.artist_name,
    )
    return (
        [
            build_tunemymusic_row(
                track_number=1,
                track=fallback_track,
                row=record.row,
                lookup=fallback_lookup,
            )
        ],
        1,
    )


def build_tunemymusic_row(
    track_number: int,
    track: DiscogsTrack,
    row: Mapping[str, str],
    lookup: ReleaseTracklistLookup,
) -> dict[str, str]:
    artist_name = track.artist_name or lookup.artist_name
    album_name = lookup.album_name or clean_cell(row.get("Title", ""))
    release_id = clean_cell(row.get(RELEASE_ID_COLUMN, ""))
    return {
        "Release Id": release_id,
        "Album Name": album_name,
        "Track Number": str(track_number),
        "Track Name": track.title,
        "Artist Name": artist_name,
        "Spotify Search Query": build_search_query(track.title, artist_name, album_name),
    }


def release_report_key(row: Mapping[str, str]) -> ReleaseReportKey:
    release_id = clean_cell(row.get("Release Id", ""))
    if release_id:
        return ("release_id", release_id, "")
    return (
        "missing_release_id",
        clean_cell(row.get("Artist Name", "")).casefold(),
        clean_cell(row.get("Album Name", "")).casefold(),
    )


def release_report_entry_from_rows(key: ReleaseReportKey, rows: Sequence[Mapping[str, str]]) -> ReleaseReportEntry:
    first_row = rows[0]
    return ReleaseReportEntry(
        key=key,
        release_id=clean_cell(first_row.get("Release Id", "")),
        artist_name=clean_cell(first_row.get("Artist Name", "")),
        album_name=clean_cell(first_row.get("Album Name", "")),
        track_row_count=len(rows),
    )


def release_report_entries_from_rows(rows: Sequence[Mapping[str, str]]) -> tuple[ReleaseReportEntry, ...]:
    rows_by_key: dict[ReleaseReportKey, list[Mapping[str, str]]] = {}
    key_order: list[ReleaseReportKey] = []
    for row in rows:
        key = release_report_key(row)
        if key not in rows_by_key:
            rows_by_key[key] = []
            key_order.append(key)
        rows_by_key[key].append(row)
    return tuple(release_report_entry_from_rows(key, rows_by_key[key]) for key in key_order)


def compare_release_entries(
    previous_entries: Sequence[ReleaseReportEntry],
    current_entries: Sequence[ReleaseReportEntry],
) -> tuple[tuple[ReleaseReportEntry, ...], tuple[ReleaseReportEntry, ...]]:
    previous_by_key = {entry.key: entry for entry in previous_entries}
    current_by_key = {entry.key: entry for entry in current_entries}
    added = tuple(entry for entry in current_entries if entry.key not in previous_by_key)
    removed = tuple(entry for entry in previous_entries if entry.key not in current_by_key)
    return added, removed


def track_report_key(row: Mapping[str, str]) -> TrackReportKey:
    return (
        clean_cell(row.get("Release Id", "")),
        clean_cell(row.get("Album Name", "")).casefold(),
        clean_cell(row.get("Artist Name", "")).casefold(),
        clean_cell(row.get("Track Number", "")),
        clean_cell(row.get("Track Name", "")).casefold(),
    )


def track_report_entry_from_row(key: TrackReportKey, row: Mapping[str, str]) -> TrackReportEntry:
    return TrackReportEntry(
        key=key,
        release_id=clean_cell(row.get("Release Id", "")),
        artist_name=clean_cell(row.get("Artist Name", "")),
        album_name=clean_cell(row.get("Album Name", "")),
        track_number=clean_cell(row.get("Track Number", "")),
        track_name=clean_cell(row.get("Track Name", "")),
    )


def track_report_entries_from_rows(rows: Sequence[Mapping[str, str]]) -> tuple[TrackReportEntry, ...]:
    entries: list[TrackReportEntry] = []
    seen_keys: set[TrackReportKey] = set()
    for row in rows:
        key = track_report_key(row)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        entries.append(track_report_entry_from_row(key, row))
    return tuple(entries)


def compare_track_entries(
    previous_entries: Sequence[TrackReportEntry],
    current_entries: Sequence[TrackReportEntry],
) -> tuple[TrackReportEntry, ...]:
    previous_keys = {entry.key for entry in previous_entries}
    return tuple(entry for entry in current_entries if entry.key not in previous_keys)


def read_existing_playlist_rows_for_report(path: Path, review_notes: list[str]) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    if not path.exists():
        return [], ()
    try:
        rows, fieldnames = read_csv_file(path)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        note = f"{path}: previous playlist CSV could not be read; release change report skipped: {error}"
        review_notes.append(note)
        return [], (note,)

    missing_columns = [column for column in TUNEMYMUSIC_COLUMNS if column not in fieldnames]
    if missing_columns:
        note = (
            f"{path}: previous playlist CSV is missing TuneMyMusic columns; "
            f"release change report skipped: {', '.join(missing_columns)}"
        )
        review_notes.append(note)
        return [], (note,)
    return rows, ()


def build_playlist_release_change(
    playlist_name: str,
    path: Path,
    previous_rows: Sequence[Mapping[str, str]],
    current_rows: Sequence[Mapping[str, str]],
    notes: Sequence[str] = (),
) -> PlaylistReleaseChange:
    previous_entries = release_report_entries_from_rows(previous_rows)
    current_entries = release_report_entries_from_rows(current_rows)
    if notes:
        added_releases: tuple[ReleaseReportEntry, ...] = ()
        removed_releases: tuple[ReleaseReportEntry, ...] = ()
        added_tracks: tuple[TrackReportEntry, ...] = ()
    else:
        added_releases, removed_releases = compare_release_entries(previous_entries, current_entries)
        added_tracks = compare_track_entries(
            previous_entries=track_report_entries_from_rows(previous_rows),
            current_entries=track_report_entries_from_rows(current_rows),
        )
    return PlaylistReleaseChange(
        playlist_name=playlist_name,
        path=path,
        added_releases=added_releases,
        removed_releases=removed_releases,
        added_tracks=added_tracks,
        notes=tuple(notes),
    )


def build_stale_playlist_release_changes(
    output_directory: Path,
    written_playlist_paths: set[Path],
    review_notes: list[str],
) -> tuple[PlaylistReleaseChange, ...]:
    if not output_directory.exists():
        return ()

    changes: list[PlaylistReleaseChange] = []
    for folder_path in sorted(path for path in output_directory.iterdir() if path.is_dir()):
        path = playlist_master_path(folder_path)
        if path in written_playlist_paths:
            continue
        previous_rows, previous_notes = read_existing_playlist_rows_for_report(path, review_notes)
        previous_entries = release_report_entries_from_rows(previous_rows)
        if not previous_entries and not previous_notes:
            continue
        stale_note = "previous playlist file was not regenerated in this run; file left unchanged"
        changes.append(
            PlaylistReleaseChange(
                playlist_name=folder_path.name,
                path=path,
                added_releases=(),
                removed_releases=previous_entries,
                notes=tuple(previous_notes) + (stale_note,),
            )
        )
    return tuple(changes)


def fallback_lookup_from_row(row: Mapping[str, str]) -> ReleaseTracklistLookup:
    release_id = clean_cell(row.get(RELEASE_ID_COLUMN, ""))
    return ReleaseTracklistLookup(
        release_id=release_id,
        artist_name=clean_cell(row.get("Artist", "")),
        album_name=clean_cell(row.get("Title", "")),
        record_year=record_year_from_row(row),
        tracks=(),
        notes=(),
    )


def build_search_query(track_name: str, artist_name: str, album_name: str) -> str:
    return " ".join(value for value in (artist_name.strip(), track_name.strip(), album_name.strip()) if value)


def build_playlist_paths(playlist_names: Sequence[str], output_directory: Path) -> dict[str, Path]:
    return {
        playlist_name: playlist_master_path(folder_path)
        for playlist_name, folder_path in build_playlist_folder_paths(playlist_names, output_directory).items()
    }


def build_playlist_folder_paths(playlist_names: Sequence[str], output_directory: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    used_folder_names: set[str] = set()
    existing_folder_paths = existing_playlist_folder_paths_by_safe_name(output_directory)
    for playlist_name in playlist_names:
        base_name = safe_playlist_filename(playlist_name)
        folder_path = existing_folder_paths.get(base_name.casefold())
        folder_name = folder_path.name if folder_path else base_name
        suffix = 2
        while folder_name.casefold() in used_folder_names:
            folder_name = f"{base_name} ({suffix})"
            folder_path = None
            suffix += 1
        used_folder_names.add(folder_name.casefold())
        paths[playlist_name] = folder_path or output_directory / folder_name
    return paths


def existing_playlist_folder_paths_by_safe_name(output_directory: Path) -> dict[str, Path]:
    if not output_directory.exists():
        return {}
    folder_paths: dict[str, Path] = {}
    for folder_path in sorted(path for path in output_directory.iterdir() if path.is_dir()):
        folder_paths.setdefault(folder_path.name.casefold(), folder_path)
    return folder_paths


def playlist_master_path(folder_path: Path) -> Path:
    return folder_path / f"{folder_path.name}.csv"


def safe_playlist_filename(playlist_name: str) -> str:
    clean_name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", playlist_name).strip()
    clean_name = re.sub(r"\s+", " ", clean_name)
    return clean_name or "playlist"


def release_tracklist_from_payload(
    release_id: str,
    payload: Mapping[str, object],
    row: Mapping[str, str],
) -> ReleaseTracklistLookup:
    artist_name = parse_artists(payload.get("artists")) or clean_cell(row.get("Artist", ""))
    album_name = clean_cell(payload.get("title")) or clean_cell(row.get("Title", ""))
    record_year = record_year_from_payload(payload) or record_year_from_row(row)
    tracks = tuple(flatten_tracklist(payload.get("tracklist"), artist_name))
    notes = () if tracks else ("no Discogs tracklist found",)
    return ReleaseTracklistLookup(
        release_id=release_id,
        artist_name=artist_name,
        album_name=album_name,
        record_year=record_year,
        tracks=tracks,
        notes=notes,
    )


def flatten_tracklist(tracklist: object, default_artist_name: str) -> list[DiscogsTrack]:
    if not isinstance(tracklist, list):
        return []
    tracks: list[DiscogsTrack] = []
    for item in tracklist:
        if not isinstance(item, Mapping):
            continue
        sub_tracks = flatten_tracklist(item.get("sub_tracks"), default_artist_name)
        if sub_tracks:
            tracks.extend(sub_tracks)
            continue

        track_type = clean_cell(item.get("type_", "track")).casefold() or "track"
        if track_type != "track":
            continue

        title = clean_cell(item.get("title", ""))
        if not title:
            continue
        tracks.append(
            DiscogsTrack(
                position=clean_cell(item.get("position", "")),
                title=title,
                artist_name=parse_artists(item.get("artists")) or default_artist_name,
            )
        )
    return tracks


def parse_artists(value: object) -> str:
    if not isinstance(value, list):
        return ""
    names: list[str] = []
    for artist in value:
        if not isinstance(artist, Mapping):
            continue
        name = clean_discogs_artist_name(clean_cell(artist.get("anv")) or clean_cell(artist.get("name")))
        if name:
            names.append(name)
    return ", ".join(names)


def clean_discogs_artist_name(name: str) -> str:
    return re.sub(r"\s+\(\d+\)$", "", name).strip()


def record_year_from_payload(payload: Mapping[str, object]) -> str:
    return first_year(clean_cell(payload.get("year", "")))


def record_year_from_row(row: Mapping[str, str]) -> str:
    return first_year(clean_cell(row.get("Released", "")))


def first_year(value: str) -> str:
    match = re.search(r"\b(\d{4})\b", value)
    return match.group(1) if match else ""


def fetch_release_tracklist(
    row: Mapping[str, str],
    token: str,
    user_agent: str,
    timeout_seconds: int,
    rate_limiter: DiscogsRateLimiter,
) -> ReleaseTracklistLookup:
    release_id = clean_cell(row.get(RELEASE_ID_COLUMN, ""))
    body = http_get(
        release_api_url(release_id),
        user_agent=user_agent,
        token=token,
        timeout_seconds=timeout_seconds,
        accept="application/json",
        rate_limiter=rate_limiter,
    )
    payload = json.loads(body or "{}")
    if not isinstance(payload, Mapping):
        payload = {}
    return release_tracklist_from_payload(release_id, payload, row)


def make_cached_tracklist_lookup(
    cache_path: Path,
    token: str,
    user_agent: str,
    timeout_seconds: int,
    request_interval_seconds: float,
) -> Callable[[Mapping[str, str]], ReleaseTracklistLookup]:
    cache = load_tracklist_cache(cache_path)
    rate_limiter = DiscogsRateLimiter(
        fallback_request_interval_seconds=request_interval_seconds,
        initial_rate_limit=60 if token else 25,
    )

    def lookup(row: Mapping[str, str]) -> ReleaseTracklistLookup:
        release_id = clean_cell(row.get(RELEASE_ID_COLUMN, ""))
        cached_lookup = cache.get(release_id)
        if cached_lookup:
            return cached_lookup
        lookup_result = fetch_release_tracklist(
            row=row,
            token=token,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            rate_limiter=rate_limiter,
        )
        cache[release_id] = lookup_result
        save_tracklist_cache(cache_path, cache)
        return lookup_result

    return lookup


def load_tracklist_cache(path: Path) -> dict[str, ReleaseTracklistLookup]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != TRACKLIST_CACHE_SCHEMA_VERSION
        or payload.get("record_type") != TRACKLIST_CACHE_RECORD_TYPE
        or not isinstance(payload.get("records"), Mapping)
    ):
        raise ValueError("unsupported playlist track cache format; delete the old cache or choose a new --cache path")
    records = payload["records"]
    return {
        str(release_id): tracklist_lookup_from_cache_record(str(release_id), record)
        for release_id, record in records.items()
        if isinstance(record, Mapping)
    }


def tracklist_lookup_from_cache_record(release_id: str, record: Mapping[str, object]) -> ReleaseTracklistLookup:
    return ReleaseTracklistLookup(
        release_id=clean_cell(record.get("release_id")) or release_id,
        artist_name=clean_cell(record.get("artist_name")),
        album_name=clean_cell(record.get("album_name")),
        record_year=clean_cell(record.get("record_year")),
        tracks=tuple(track_from_cache_record(track) for track in record.get("tracks", []) if isinstance(track, Mapping)),
        notes=tuple(clean_cell(note) for note in record.get("notes", []) if clean_cell(note)),
    )


def track_from_cache_record(record: Mapping[str, object]) -> DiscogsTrack:
    return DiscogsTrack(
        position=clean_cell(record.get("position")),
        title=clean_cell(record.get("title")),
        artist_name=clean_cell(record.get("artist_name")),
    )


def save_tracklist_cache(path: Path, cache: Mapping[str, ReleaseTracklistLookup]) -> None:
    payload = {
        "schema_version": TRACKLIST_CACHE_SCHEMA_VERSION,
        "record_type": TRACKLIST_CACHE_RECORD_TYPE,
        "records": {
            release_id: tracklist_lookup_to_cache_record(lookup)
            for release_id, lookup in sorted(cache.items())
        },
    }
    write_json_file(path, payload)


def tracklist_lookup_to_cache_record(lookup: ReleaseTracklistLookup) -> dict[str, object]:
    return {
        "release_id": lookup.release_id,
        "artist_name": lookup.artist_name,
        "album_name": lookup.album_name,
        "record_year": lookup.record_year,
        "tracks": [
            {
                "position": track.position,
                "title": track.title,
                "artist_name": track.artist_name,
            }
            for track in lookup.tracks
        ],
        "notes": list(lookup.notes),
    }


def default_report_path(output_directory: Path) -> Path:
    return timestamped_report_path(output_directory, "playlist_export_report")


def run_playlist_export(args: argparse.Namespace) -> PlaylistExportSummary:
    lookup_tracklist = make_cached_tracklist_lookup(
        cache_path=args.cache,
        token=args.discogs_token,
        user_agent=args.user_agent,
        timeout_seconds=args.timeout_seconds,
        request_interval_seconds=args.request_interval_seconds,
    )
    return export_playlist_csvs(
        input_path=args.input,
        output_directory=args.output_dir,
        report_path=args.report,
        lookup_tracklist=lookup_tracklist,
        progress=ProgressReporter(label="Exporting playlist rows") if getattr(args, "progress", False) else None,
    )


def write_report(path: Path, summary: PlaylistExportSummary) -> None:
    lines = format_report_title("Discogs TuneMyMusic playlist export report")
    lines.extend(
        format_report_section(
            "Summary",
            [
                f"- Input rows: {summary.input_rows}",
                f"- Exported playlists: {summary.playlist_count}",
                f"- Output track rows: {summary.track_row_count}",
                f"- Release-level fallback rows: {summary.fallback_row_count}",
                f"- Skipped rows without playlists: {summary.skipped_unassigned_count}",
            ],
        )
    )
    lines.extend(
        format_report_section(
            "Files",
            [
                f"- Input: {summary.input_path}",
                f"- Output directory: {summary.output_directory}",
            ],
        )
    )
    playlist_lines = [
        f"- {playlist_file.playlist_name}: {playlist_file.path} ({playlist_file.row_count} rows)"
        for playlist_file in summary.playlist_files
    ] or ["- None"]
    lines.extend(format_report_section("Playlist CSVs", playlist_lines))
    lines.extend(
        format_report_section(
            "Playlist release changes",
            format_playlist_release_change_lines(summary.playlist_release_changes),
        )
    )
    lines.extend(format_report_section("Review notes", list(summary.review_notes) or ["- None"]))
    write_text_report(path, lines)


def format_release_change_entry(entry: ReleaseReportEntry) -> str:
    release_id = entry.release_id or "missing release_id"
    track_word = "track row" if entry.track_row_count == 1 else "track rows"
    return f"{release_id} | {entry.artist_name} | {entry.album_name} | {entry.track_row_count} {track_word}"


def format_playlist_release_change_lines(changes: Sequence[PlaylistReleaseChange]) -> list[str]:
    lines: list[str] = []
    reportable_changes = [
        change
        for change in changes
        if change.added_releases or change.removed_releases or change.notes
    ]
    for change in reportable_changes:
        lines.append(f"- {change.playlist_name}:")
        lines.append(f"  File: {change.path}")
        for note in change.notes:
            lines.append(f"  Note: {note}")
        lines.append("  Added releases:")
        if change.added_releases:
            lines.extend(f"    - {format_release_change_entry(entry)}" for entry in change.added_releases)
        else:
            lines.append("    - None")
        lines.append("  Removed releases:")
        if change.removed_releases:
            lines.extend(f"    - {format_release_change_entry(entry)}" for entry in change.removed_releases)
        else:
            lines.append("    - None")
    return lines or ["- None"]


def print_summary(summary: PlaylistExportSummary) -> None:
    print_report_section(
        "Files",
        [
            f"Output directory: {summary.output_directory}",
            f"Report: {summary.report_path}",
        ],
    )
    print_report_section(
        "Processed",
        [
            f"Input rows: {summary.input_rows}",
            f"Exported playlists: {summary.playlist_count}",
            f"Output track rows: {summary.track_row_count}",
            f"Release-level fallback rows: {summary.fallback_row_count}",
            f"Skipped rows without playlists: {summary.skipped_unassigned_count}",
        ],
    )
    print_report_section(
        "Playlist Release Changes",
        format_playlist_release_change_lines(summary.playlist_release_changes),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH, help="Playlist-mapped enriched master CSV. Defaults to collection/enriched-collection.csv.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY, help="Directory for per-playlist CSVs. Defaults to collection/playlists.")
    parser.add_argument("--report", type=Path, help="Text report path. Defaults to reports/playlists_<timestamp>_playlist_export_report.txt.")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH, help="Discogs tracklist cache JSON. Defaults to collection/cache/playlist-tracks.cache.json.")
    parser.add_argument("--discogs-token", default=os.environ.get("DISCOGS_TOKEN", ""), help="Optional Discogs personal access token. Defaults to DISCOGS_TOKEN.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent sent to Discogs.")
    parser.add_argument("--timeout-seconds", type=int, default=30, help="HTTP timeout per request.")
    parser.add_argument("--request-interval-seconds", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS, help="Minimum delay between Discogs requests. Defaults to header-aware throttling.")
    parser.add_argument("--no-progress", action="store_false", dest="progress", help="Disable terminal progress output.")
    args = parser.parse_args(argv)
    if args.request_interval_seconds < 0:
        parser.error("--request-interval-seconds must be non-negative")
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")
    args.report = args.report or default_report_path(args.output_dir)
    return args


def clean_cell(value: object) -> str:
    return str(value or "").strip()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        summary = run_playlist_export(args)
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
