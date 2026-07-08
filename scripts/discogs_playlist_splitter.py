#!/usr/bin/env python3
"""Regenerate split CSV files from TuneMyMusic playlist master CSVs."""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from shared.cli import print_cli_summary, run_cli
from shared.files import read_csv_file, write_csv_file
from shared.playlist_selection import resolve_playlist_master_paths
from shared.reports import (
    format_report_section,
    format_report_title,
    script_report_path,
    write_text_report,
)
from shared.tunemymusic import TUNEMYMUSIC_COLUMNS, missing_tunemymusic_columns, normalize_tunemymusic_rows
from shared.workflow_config import (
    DEFAULT_MAX_ROWS_PER_SPLIT,
    DEFAULT_WORKFLOW_CONFIG_PATH,
    WorkflowConfig,
    load_or_create_workflow_config,
)
from shared.workflow_paths import DEFAULT_PLAYLIST_OUTPUT_DIRECTORY


PLAYLIST_SPLITS_DIRECTORY_NAME = "splits"
DEFAULT_OUTPUT_DIRECTORY = DEFAULT_PLAYLIST_OUTPUT_DIRECTORY
RANGE_FILENAME_PATTERN = re.compile(r"^(\d+)-(\d+)\.csv$")


@dataclass(frozen=True)
class SplitChunk:
    start_row_number: int
    end_row_number: int
    rows: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class SplitPlan:
    chunks: tuple[SplitChunk, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PlaylistSplitSummary:
    playlist_folder_path: Path
    master_path: Path
    written_split_paths: tuple[Path, ...]
    regenerated_split_paths: tuple[Path, ...]
    preserved_split_paths: tuple[Path, ...]
    warnings: tuple[str, ...]
    updated_split_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ExistingSplitFile:
    path: Path
    start_row_number: int
    end_row_number: int
    rows: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class SplitGroup:
    release_id: str
    start_row_number: int
    rows: tuple[dict[str, str], ...]

    @property
    def end_row_number(self) -> int:
        return self.start_row_number + len(self.rows) - 1


def plan_split_chunks(
    rows: Sequence[Mapping[str, str]],
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
    keep_release_tracks_together: bool = True,
) -> SplitPlan:
    if keep_release_tracks_together:
        return plan_release_grouped_split_chunks(rows, max_rows=max_rows)
    return plan_row_count_split_chunks(rows, max_rows=max_rows)


def plan_row_count_split_chunks(rows: Sequence[Mapping[str, str]], max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT) -> SplitPlan:
    if max_rows < 1:
        raise ValueError("max_rows must be at least 1")

    clean_rows = tuple({str(key): str(value or "") for key, value in row.items()} for row in rows)
    chunks: list[SplitChunk] = []
    for offset in range(0, len(clean_rows), max_rows):
        split_rows = clean_rows[offset : offset + max_rows]
        start_row_number = offset + 1
        chunks.append(
            SplitChunk(
                start_row_number=start_row_number,
                end_row_number=start_row_number + len(split_rows) - 1,
                rows=tuple(split_rows),
            )
        )
    return SplitPlan(chunks=tuple(chunks), warnings=())


def plan_release_grouped_split_chunks(
    rows: Sequence[Mapping[str, str]],
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
) -> SplitPlan:
    if max_rows < 1:
        raise ValueError("max_rows must be at least 1")

    groups, warnings = build_release_groups(rows)
    chunks: list[SplitChunk] = []
    current_rows: list[dict[str, str]] = []
    current_start_row_number: int | None = None

    for group in groups:
        if len(group.rows) > max_rows:
            if current_rows:
                chunks.append(
                    SplitChunk(
                        start_row_number=current_start_row_number or 1,
                        end_row_number=(current_start_row_number or 1) + len(current_rows) - 1,
                        rows=tuple(current_rows),
                    )
                )
                current_rows = []
                current_start_row_number = None

            warnings.append(
                f"Release Id {group.release_id} has {len(group.rows)} rows, exceeding max_rows {max_rows}; "
                "split across multiple files."
            )
            for offset in range(0, len(group.rows), max_rows):
                split_rows = group.rows[offset : offset + max_rows]
                start_row_number = group.start_row_number + offset
                chunks.append(
                    SplitChunk(
                        start_row_number=start_row_number,
                        end_row_number=start_row_number + len(split_rows) - 1,
                        rows=tuple(split_rows),
                    )
                )
            continue

        if current_rows and len(current_rows) + len(group.rows) > max_rows:
            chunks.append(
                SplitChunk(
                    start_row_number=current_start_row_number or 1,
                    end_row_number=(current_start_row_number or 1) + len(current_rows) - 1,
                    rows=tuple(current_rows),
                )
            )
            current_rows = []
            current_start_row_number = None

        if current_start_row_number is None:
            current_start_row_number = group.start_row_number
        current_rows.extend(group.rows)

    if current_rows:
        chunks.append(
            SplitChunk(
                start_row_number=current_start_row_number or 1,
                end_row_number=(current_start_row_number or 1) + len(current_rows) - 1,
                rows=tuple(current_rows),
            )
        )

    return SplitPlan(chunks=tuple(chunks), warnings=tuple(warnings))


def build_release_groups(rows: Sequence[Mapping[str, str]]) -> tuple[list[SplitGroup], list[str]]:
    groups: list[SplitGroup] = []
    warnings: list[str] = []
    current_release_id = ""
    current_start_row_number = 0
    current_rows: list[dict[str, str]] = []

    def append_current_group() -> None:
        nonlocal current_release_id, current_start_row_number, current_rows
        if current_rows:
            groups.append(
                SplitGroup(
                    release_id=current_release_id,
                    start_row_number=current_start_row_number,
                    rows=tuple(current_rows),
                )
            )
            current_release_id = ""
            current_start_row_number = 0
            current_rows = []

    for row_number, row in enumerate(rows, start=1):
        clean_row = {str(key): str(value or "") for key, value in row.items()}
        release_id = clean_row.get("Release Id", "").strip()
        if not release_id:
            append_current_group()
            groups.append(SplitGroup(release_id="", start_row_number=row_number, rows=(clean_row,)))
            warnings.append(
                f"Row {row_number} has blank Release Id; treated as one-row group because release boundaries cannot be proven."
            )
            continue

        if current_rows and release_id != current_release_id:
            append_current_group()
        if not current_rows:
            current_release_id = release_id
            current_start_row_number = row_number
        current_rows.append(clean_row)

    append_current_group()
    return groups, warnings


def write_regenerated_splits(
    master_path: Path,
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
    keep_release_tracks_together: bool = True,
) -> PlaylistSplitSummary:
    rows, fieldnames = read_csv_file(master_path)
    validate_master_fieldnames(master_path, fieldnames)
    plan = plan_split_chunks(rows, max_rows=max_rows, keep_release_tracks_together=keep_release_tracks_together)
    playlist_folder_path = master_path.parent
    if playlist_folder_path.is_symlink():
        raise ValueError(f"{playlist_folder_path}: playlist folder symlinks are not supported")
    splits_directory = playlist_folder_path / PLAYLIST_SPLITS_DIRECTORY_NAME
    if splits_directory.is_symlink():
        raise ValueError(f"{splits_directory}: splits directory symlinks are not supported")
    preserved_split_paths = tuple(sorted(path for path in splits_directory.iterdir() if not path.is_file() or path.suffix.lower() != ".csv")) if splits_directory.exists() else ()
    regenerated_split_paths: list[Path] = []
    if splits_directory.exists():
        for path in sorted(splits_directory.glob("*.csv")):
            if path.parent == splits_directory and path.is_file():
                path.unlink()
                regenerated_split_paths.append(path)

    written_split_paths: list[Path] = []
    for chunk in plan.chunks:
        split_path = splits_directory / f"{chunk.start_row_number}-{chunk.end_row_number}.csv"
        write_csv_file(split_path, TUNEMYMUSIC_COLUMNS, chunk.rows)
        written_split_paths.append(split_path)

    return PlaylistSplitSummary(
        playlist_folder_path=playlist_folder_path,
        master_path=master_path,
        written_split_paths=tuple(written_split_paths),
        regenerated_split_paths=tuple(regenerated_split_paths),
        preserved_split_paths=preserved_split_paths,
        warnings=plan.warnings,
    )


def read_existing_split_files(splits_directory: Path) -> tuple[tuple[ExistingSplitFile, ...], tuple[str, ...]]:
    if splits_directory.is_symlink():
        raise ValueError(f"{splits_directory}: splits directory symlinks are not supported")
    if not splits_directory.exists():
        return (), ()
    if not splits_directory.is_dir():
        raise NotADirectoryError(splits_directory)

    existing_splits: list[ExistingSplitFile] = []
    warnings: list[str] = []
    for path in sorted(splits_directory.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".csv":
            continue
        match = RANGE_FILENAME_PATTERN.match(path.name)
        if not match:
            warnings.append(f"{path}: non-range CSV file ignored; review manually.")
            continue

        start_row_number = int(match.group(1))
        end_row_number = int(match.group(2))
        if start_row_number < 1 or end_row_number < start_row_number:
            raise ValueError(f"{path}: invalid split row range")

        rows, fieldnames = read_csv_file(path)
        validate_master_fieldnames(path, fieldnames)
        expected_row_count = end_row_number - start_row_number + 1
        if len(rows) != expected_row_count:
            raise ValueError(
                f"{path}: row count {len(rows)} does not match advertised range "
                f"{start_row_number}-{end_row_number} ({expected_row_count} rows)"
            )
        existing_splits.append(
            ExistingSplitFile(
                path=path,
                start_row_number=start_row_number,
                end_row_number=end_row_number,
                rows=tuple(normalize_tunemymusic_rows(rows)),
            )
        )

    sorted_splits = sorted(existing_splits, key=lambda split: (split.start_row_number, split.end_row_number))
    expected_start_row_number = 1
    for split in sorted_splits:
        if split.start_row_number < expected_start_row_number:
            raise ValueError(f"{split.path}: split row ranges overlap or are out of order")
        if split.start_row_number > expected_start_row_number:
            raise ValueError(
                f"{split.path}: split row ranges contain a gap before row {split.start_row_number}; "
                f"expected row {expected_start_row_number}"
            )
        expected_start_row_number = split.end_row_number + 1

    return tuple(sorted_splits), tuple(warnings)


def write_stable_splits(
    master_path: Path,
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
    keep_release_tracks_together: bool = True,
    create_new_split_files_for_new_releases: bool = True,
) -> PlaylistSplitSummary:
    rows, fieldnames = read_csv_file(master_path)
    validate_master_fieldnames(master_path, fieldnames)
    master_rows = tuple(normalize_tunemymusic_rows(rows))
    playlist_folder_path = master_path.parent
    if playlist_folder_path.is_symlink():
        raise ValueError(f"{playlist_folder_path}: playlist folder symlinks are not supported")
    splits_directory = playlist_folder_path / PLAYLIST_SPLITS_DIRECTORY_NAME
    existing_splits, read_warnings = read_existing_split_files(splits_directory)
    if not existing_splits:
        if read_warnings:
            return write_initial_stable_splits(
                master_path,
                master_rows,
                tuple(read_warnings),
                max_rows=max_rows,
                keep_release_tracks_together=keep_release_tracks_together,
            )
        return write_regenerated_splits(
            master_path,
            max_rows=max_rows,
            keep_release_tracks_together=keep_release_tracks_together,
        )

    warnings = list(read_warnings)
    preserved_split_paths: list[Path] = []
    latest_existing_split = existing_splits[-1]
    for existing_split in existing_splits:
        expected_rows = master_rows[existing_split.start_row_number - 1 : existing_split.end_row_number]
        if existing_split.rows != expected_rows:
            if existing_split is latest_existing_split and not create_new_split_files_for_new_releases:
                raise ValueError(
                    f"{existing_split.path}: latest split content differs from current master rows "
                    f"{existing_split.start_row_number}-{existing_split.end_row_number}; "
                    "append mode would rewrite it. Run --regenerate for this playlist after reviewing the mismatch."
                )
            warnings.append(
                f"{existing_split.path}: existing split content differs from current master rows "
                f"{existing_split.start_row_number}-{existing_split.end_row_number}; preserved without rewrite."
            )
        if create_new_split_files_for_new_releases or existing_split is not latest_existing_split:
            preserved_split_paths.append(existing_split.path)

    highest_existing_end_row_number = max(split.end_row_number for split in existing_splits)
    new_rows = master_rows[highest_existing_end_row_number:]
    written_split_paths: list[Path] = []
    updated_split_paths: list[Path] = []
    remaining_new_rows: Sequence[Mapping[str, str]] = new_rows
    new_row_offset = highest_existing_end_row_number

    if create_new_split_files_for_new_releases:
        boundary_warning = release_boundary_warning(latest_existing_split, new_rows, highest_existing_end_row_number)
        if boundary_warning:
            warnings.append(boundary_warning)
    else:
        append_rows, remaining_new_rows, append_warnings = split_rows_for_latest_append(
            latest_existing_split,
            new_rows,
            max_rows=max_rows,
            keep_release_tracks_together=keep_release_tracks_together,
        )
        warnings.extend(append_warnings)
        if append_rows:
            updated_rows = (*latest_existing_split.rows, *append_rows)
            updated_end_row_number = latest_existing_split.end_row_number + len(append_rows)
            updated_split_path = splits_directory / f"{latest_existing_split.start_row_number}-{updated_end_row_number}.csv"
            write_csv_file(updated_split_path, TUNEMYMUSIC_COLUMNS, updated_rows)
            if updated_split_path != latest_existing_split.path:
                latest_existing_split.path.unlink()
            updated_split_paths.append(updated_split_path)
            new_row_offset = updated_end_row_number
        else:
            preserved_split_paths.append(latest_existing_split.path)

    plan = plan_split_chunks(
        remaining_new_rows,
        max_rows=max_rows,
        keep_release_tracks_together=keep_release_tracks_together,
    )
    warnings.extend(plan.warnings)
    for chunk in plan.chunks:
        start_row_number = new_row_offset + chunk.start_row_number
        end_row_number = new_row_offset + chunk.end_row_number
        split_path = splits_directory / f"{start_row_number}-{end_row_number}.csv"
        write_csv_file(split_path, TUNEMYMUSIC_COLUMNS, chunk.rows)
        written_split_paths.append(split_path)

    return PlaylistSplitSummary(
        playlist_folder_path=playlist_folder_path,
        master_path=master_path,
        written_split_paths=tuple(written_split_paths),
        regenerated_split_paths=(),
        preserved_split_paths=tuple(preserved_split_paths),
        warnings=tuple(warnings),
        updated_split_paths=tuple(updated_split_paths),
    )


def release_boundary_warning(
    last_existing_split: ExistingSplitFile,
    new_rows: Sequence[Mapping[str, str]],
    highest_existing_end_row_number: int,
) -> str:
    if not new_rows or not last_existing_split.rows:
        return ""

    trailing_release_id = last_existing_split.rows[-1].get("Release Id", "").strip()
    first_new_release_id = str(new_rows[0].get("Release Id", "") or "").strip()
    if not trailing_release_id or trailing_release_id != first_new_release_id:
        return ""

    first_new_group_end_row_number = highest_existing_end_row_number
    for row in new_rows:
        release_id = str(row.get("Release Id", "") or "").strip()
        if release_id != first_new_release_id:
            break
        first_new_group_end_row_number += 1

    return (
        f"Release Id {first_new_release_id} continues from preserved split "
        f"{last_existing_split.path.name} rows {last_existing_split.start_row_number}-{last_existing_split.end_row_number} "
        f"into new rows {highest_existing_end_row_number + 1}-{first_new_group_end_row_number}; "
        "frozen splits prevent preserving that release boundary. "
        "Run --regenerate for this playlist to rebuild split boundaries."
    )


def split_rows_for_latest_append(
    latest_existing_split: ExistingSplitFile,
    new_rows: Sequence[Mapping[str, str]],
    max_rows: int,
    keep_release_tracks_together: bool,
) -> tuple[tuple[dict[str, str], ...], tuple[Mapping[str, str], ...], tuple[str, ...]]:
    if not new_rows:
        return (), (), ()

    capacity = max_rows - len(latest_existing_split.rows)
    if capacity <= 0:
        return (), tuple(new_rows), ()

    if not keep_release_tracks_together:
        append_rows = tuple(dict(row) for row in new_rows[:capacity])
        return append_rows, tuple(new_rows[capacity:]), ()

    groups, warnings = build_release_groups(new_rows)
    append_count = 0
    for group in groups:
        remaining_capacity = capacity - append_count
        if remaining_capacity <= 0:
            break
        if len(group.rows) <= remaining_capacity:
            append_count += len(group.rows)
            continue
        if len(group.rows) > max_rows and append_count == 0:
            append_count += remaining_capacity
            warnings.append(
                f"Release Id {group.release_id} has {len(group.rows)} new rows, exceeding max_rows {max_rows}; "
                "split across the latest split file and a new split file."
            )
        break

    if append_count == 0:
        trailing_release_id = latest_existing_split.rows[-1].get("Release Id", "").strip()
        first_new_release_id = str(new_rows[0].get("Release Id", "") or "").strip()
        if trailing_release_id and trailing_release_id == first_new_release_id:
            warnings.append(
                f"Release Id {first_new_release_id} continues from latest split {latest_existing_split.path.name}, "
                f"but appending it would exceed max_rows {max_rows}; write remaining tracks to a new split file."
            )

    append_rows = tuple(dict(row) for row in new_rows[:append_count])
    return append_rows, tuple(new_rows[append_count:]), tuple(warnings)


def write_initial_stable_splits(
    master_path: Path,
    master_rows: Sequence[Mapping[str, str]],
    read_warnings: tuple[str, ...],
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
    keep_release_tracks_together: bool = True,
) -> PlaylistSplitSummary:
    plan = plan_split_chunks(
        master_rows,
        max_rows=max_rows,
        keep_release_tracks_together=keep_release_tracks_together,
    )
    splits_directory = master_path.parent / PLAYLIST_SPLITS_DIRECTORY_NAME
    written_split_paths: list[Path] = []
    for chunk in plan.chunks:
        split_path = splits_directory / f"{chunk.start_row_number}-{chunk.end_row_number}.csv"
        write_csv_file(split_path, TUNEMYMUSIC_COLUMNS, chunk.rows)
        written_split_paths.append(split_path)

    return PlaylistSplitSummary(
        playlist_folder_path=master_path.parent,
        master_path=master_path,
        written_split_paths=tuple(written_split_paths),
        regenerated_split_paths=(),
        preserved_split_paths=(),
        warnings=(*read_warnings, *plan.warnings),
    )


def validate_master_fieldnames(master_path: Path, fieldnames: Sequence[str]) -> None:
    missing_columns = missing_tunemymusic_columns(fieldnames)
    if missing_columns:
        raise ValueError(f"{master_path}: missing TuneMyMusic columns: {', '.join(missing_columns)}")


def resolve_master_paths(output_directory: Path, target: str) -> tuple[Path, ...]:
    return resolve_playlist_master_paths(output_directory, [target], allow_all_selector=True)


def regenerate_playlist_splits(
    output_directory: Path,
    report_path: Path,
    target: str,
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
    keep_release_tracks_together: bool = True,
) -> tuple[PlaylistSplitSummary, ...]:
    master_paths = resolve_master_paths(output_directory, target)
    summaries = tuple(
        write_regenerated_splits(
            master_path,
            max_rows=max_rows,
            keep_release_tracks_together=keep_release_tracks_together,
        )
        for master_path in master_paths
    )
    write_report(report_path, output_directory, target, summaries)
    return summaries


def update_playlist_splits(
    output_directory: Path,
    target: str,
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
    keep_release_tracks_together: bool = True,
    create_new_split_files_for_new_releases: bool = True,
) -> tuple[PlaylistSplitSummary, ...]:
    master_paths = resolve_master_paths(output_directory, target)
    return tuple(
        write_stable_splits(
            master_path,
            max_rows=max_rows,
            keep_release_tracks_together=keep_release_tracks_together,
            create_new_split_files_for_new_releases=create_new_split_files_for_new_releases,
        )
        for master_path in master_paths
    )


def update_playlist_splits_with_report(
    output_directory: Path,
    report_path: Path,
    target: str,
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
    keep_release_tracks_together: bool = True,
    create_new_split_files_for_new_releases: bool = True,
) -> tuple[PlaylistSplitSummary, ...]:
    summaries = update_playlist_splits(
        output_directory,
        target,
        max_rows=max_rows,
        keep_release_tracks_together=keep_release_tracks_together,
        create_new_split_files_for_new_releases=create_new_split_files_for_new_releases,
    )
    write_report(report_path, output_directory, target, summaries)
    return summaries


def default_report_path() -> Path:
    return script_report_path(__file__)


def write_report(
    path: Path,
    output_directory: Path,
    target: str,
    summaries: Sequence[PlaylistSplitSummary],
) -> None:
    lines = format_report_title("Discogs playlist split report")
    lines.extend(
        format_report_section(
            "Summary",
            [
                f"- Output directory: {output_directory}",
                f"- Target: {target}",
                f"- Playlists processed: {len(summaries)}",
                f"- Split CSVs written: {sum(len(summary.written_split_paths) for summary in summaries)}",
                f"- Split CSVs preserved: {sum(len(summary.preserved_split_paths) for summary in summaries)}",
                f"- Split CSVs regenerated: {sum(len(summary.regenerated_split_paths) for summary in summaries)}",
                f"- Split CSVs updated: {sum(len(summary.updated_split_paths) for summary in summaries)}",
            ],
        )
    )
    playlist_lines: list[str] = []
    for summary in summaries:
        playlist_lines.append(f"- {summary.playlist_folder_path.name}:")
        playlist_lines.append(f"  Playlist folder: {summary.playlist_folder_path}")
        playlist_lines.append(f"  Master CSV: {summary.master_path}")
        playlist_lines.append("  New split CSVs written:")
        if summary.written_split_paths:
            playlist_lines.extend(f"    - {path}" for path in summary.written_split_paths)
        else:
            playlist_lines.append("    - None")
        if summary.regenerated_split_paths:
            playlist_lines.append("  Regenerated split CSVs:")
            playlist_lines.extend(f"    - {path}" for path in summary.regenerated_split_paths)
        if summary.updated_split_paths:
            playlist_lines.append("  Updated split CSVs:")
            playlist_lines.extend(f"    - {path}" for path in summary.updated_split_paths)
        if summary.preserved_split_paths:
            playlist_lines.append("  Preserved split CSVs:")
            playlist_lines.extend(f"    - {path}" for path in summary.preserved_split_paths)
        if summary.warnings:
            playlist_lines.append("  Warnings:")
            playlist_lines.extend(f"    - {warning}" for warning in summary.warnings)
    lines.extend(format_report_section("Playlists", playlist_lines or ["- None"]))
    write_text_report(path, lines)


def print_summary(report_path: Path, summaries: Sequence[PlaylistSplitSummary]) -> None:
    print_cli_summary(
        files=[
            f"Report: {report_path}",
        ],
        processed=[
            f"Playlists: {len(summaries)}",
            f"Split CSVs written: {sum(len(summary.written_split_paths) for summary in summaries)}",
            f"Split CSVs updated: {sum(len(summary.updated_split_paths) for summary in summaries)}",
        ],
    )


def resolve_workflow_config(args: argparse.Namespace) -> WorkflowConfig:
    config = load_or_create_workflow_config(args.workflow_config)
    if args.max_rows is None:
        return config
    if args.max_rows < 1:
        raise ValueError("--max-rows must be at least 1")
    return WorkflowConfig(
        max_rows_per_split=args.max_rows,
        keep_release_tracks_together=config.keep_release_tracks_together,
        create_new_split_files_for_new_releases=config.create_new_split_files_for_new_releases,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY, help="Directory containing playlist folders. Defaults to collection/playlists.")
    parser.add_argument("--report", type=Path, help="Text report path. Defaults to reports/<timestamp>_discogs_playlist_splitter.txt.")
    parser.add_argument("--regenerate", nargs="?", const="all", help="Playlist folder/display name/path to regenerate, or all. Defaults to stable update for all playlists.")
    parser.add_argument("--workflow-config", type=Path, default=DEFAULT_WORKFLOW_CONFIG_PATH, help="Workflow JSON config. Defaults to config/workflow.json.")
    parser.add_argument("--max-rows", type=int, help="Maximum rows per split CSV. Overrides workflow config.")
    args = parser.parse_args(argv)
    args.report = args.report or default_report_path()
    return args


def run_playlist_splitter(args: argparse.Namespace) -> tuple[Path, Sequence[PlaylistSplitSummary]]:
    workflow_config = resolve_workflow_config(args)
    target = args.regenerate or "all"
    if args.regenerate is None:
        summaries = update_playlist_splits_with_report(
            output_directory=args.output_dir,
            report_path=args.report,
            target=target,
            max_rows=workflow_config.max_rows_per_split,
            keep_release_tracks_together=workflow_config.keep_release_tracks_together,
            create_new_split_files_for_new_releases=workflow_config.create_new_split_files_for_new_releases,
        )
    else:
        summaries = regenerate_playlist_splits(
            output_directory=args.output_dir,
            report_path=args.report,
            target=target,
            max_rows=workflow_config.max_rows_per_split,
            keep_release_tracks_together=workflow_config.keep_release_tracks_together,
        )
    return args.report, summaries


def print_splitter_summary(result: tuple[Path, Sequence[PlaylistSplitSummary]]) -> None:
    report_path, summaries = result
    print_summary(report_path, summaries)


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(parse_args, run_playlist_splitter, print_splitter_summary, argv)


if __name__ == "__main__":
    raise SystemExit(main())
