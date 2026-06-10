#!/usr/bin/env python3
"""Regenerate split CSV files from TuneMyMusic playlist master CSVs."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from discogs_playlist_exporter import (
    TUNEMYMUSIC_COLUMNS,
    playlist_master_path,
    safe_playlist_filename,
)
from shared.files import read_csv_file, write_csv_file
from shared.reports import (
    DEFAULT_REPORT_DIRECTORY,
    format_report_section,
    format_report_title,
    print_report_section,
    readable_timestamp,
    write_text_report,
)


PLAYLIST_SPLITS_DIRECTORY_NAME = "splits"
DEFAULT_OUTPUT_DIRECTORY = Path("collection/playlists")
DEFAULT_MAX_ROWS_PER_SPLIT = 500
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


def plan_split_chunks(rows: Sequence[Mapping[str, str]], max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT) -> SplitPlan:
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


def write_regenerated_splits(master_path: Path, max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT) -> PlaylistSplitSummary:
    rows, fieldnames = read_csv_file(master_path)
    validate_master_fieldnames(master_path, fieldnames)
    plan = plan_split_chunks(rows, max_rows=max_rows)
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


def write_stable_splits(master_path: Path, max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT) -> PlaylistSplitSummary:
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
            return write_initial_stable_splits(master_path, master_rows, tuple(read_warnings), max_rows=max_rows)
        return write_regenerated_splits(master_path, max_rows=max_rows)

    warnings = list(read_warnings)
    preserved_split_paths: list[Path] = []
    for existing_split in existing_splits:
        expected_rows = master_rows[existing_split.start_row_number - 1 : existing_split.end_row_number]
        if existing_split.rows != expected_rows:
            warnings.append(
                f"{existing_split.path}: existing split content differs from current master rows "
                f"{existing_split.start_row_number}-{existing_split.end_row_number}; preserved without rewrite."
            )
        preserved_split_paths.append(existing_split.path)

    highest_existing_end_row_number = max(split.end_row_number for split in existing_splits)
    new_rows = master_rows[highest_existing_end_row_number:]
    plan = plan_split_chunks(new_rows, max_rows=max_rows)
    boundary_warning = release_boundary_warning(existing_splits[-1], new_rows, highest_existing_end_row_number)
    if boundary_warning:
        warnings.append(boundary_warning)
    warnings.extend(plan.warnings)

    written_split_paths: list[Path] = []
    for chunk in plan.chunks:
        start_row_number = highest_existing_end_row_number + chunk.start_row_number
        end_row_number = highest_existing_end_row_number + chunk.end_row_number
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


def write_initial_stable_splits(
    master_path: Path,
    master_rows: Sequence[Mapping[str, str]],
    read_warnings: tuple[str, ...],
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
) -> PlaylistSplitSummary:
    plan = plan_split_chunks(master_rows, max_rows=max_rows)
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


def normalize_tunemymusic_rows(rows: Sequence[Mapping[str, str]]) -> tuple[dict[str, str], ...]:
    return tuple({column: str(row.get(column, "") or "") for column in TUNEMYMUSIC_COLUMNS} for row in rows)


def validate_master_fieldnames(master_path: Path, fieldnames: Sequence[str]) -> None:
    missing_columns = [column for column in TUNEMYMUSIC_COLUMNS if column not in fieldnames]
    if missing_columns:
        raise ValueError(f"{master_path}: missing TuneMyMusic columns: {', '.join(missing_columns)}")


def resolve_master_paths(output_directory: Path, target: str) -> tuple[Path, ...]:
    if not output_directory.exists():
        raise FileNotFoundError(output_directory)
    if not output_directory.is_dir():
        raise NotADirectoryError(output_directory)
    if target == "all":
        master_paths: list[Path] = []
        for folder_path in sorted(output_directory.iterdir()):
            if folder_path.is_symlink():
                raise ValueError(f"{folder_path}: playlist folder symlinks are not supported")
            if not folder_path.is_dir():
                continue
            master_path = playlist_master_path(folder_path)
            if master_path.exists():
                master_paths.append(master_path)
        return tuple(master_paths)

    validate_regenerate_target(target)

    exact_folder_path = output_directory / target
    if exact_folder_path.is_symlink():
        raise ValueError(f"{exact_folder_path}: playlist folder symlinks are not supported")
    exact_master_path = playlist_master_path(exact_folder_path)
    if exact_master_path.exists():
        return (exact_master_path,)

    safe_folder_path = output_directory / safe_playlist_filename(target)
    if safe_folder_path.is_symlink():
        raise ValueError(f"{safe_folder_path}: playlist folder symlinks are not supported")
    safe_master_path = playlist_master_path(safe_folder_path)
    if safe_master_path.exists():
        return (safe_master_path,)

    raise FileNotFoundError(f"playlist master not found for target: {target}")


def validate_regenerate_target(target: str) -> None:
    target_path = Path(target)
    if target in {".", ".."} or target_path.is_absolute() or "/" in target or "\\" in target:
        raise ValueError(f"invalid playlist target: {target}")


def regenerate_playlist_splits(
    output_directory: Path,
    report_path: Path,
    target: str,
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
) -> tuple[PlaylistSplitSummary, ...]:
    master_paths = resolve_master_paths(output_directory, target)
    summaries = tuple(write_regenerated_splits(master_path, max_rows=max_rows) for master_path in master_paths)
    write_report(report_path, output_directory, target, summaries)
    return summaries


def update_playlist_splits(
    output_directory: Path,
    target: str,
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
) -> tuple[PlaylistSplitSummary, ...]:
    master_paths = resolve_master_paths(output_directory, target)
    return tuple(write_stable_splits(master_path, max_rows=max_rows) for master_path in master_paths)


def update_playlist_splits_with_report(
    output_directory: Path,
    report_path: Path,
    target: str,
    max_rows: int = DEFAULT_MAX_ROWS_PER_SPLIT,
) -> tuple[PlaylistSplitSummary, ...]:
    summaries = update_playlist_splits(output_directory, target, max_rows=max_rows)
    write_report(report_path, output_directory, target, summaries)
    return summaries


def default_report_path() -> Path:
    return DEFAULT_REPORT_DIRECTORY / f"playlist_splits_{readable_timestamp()}_report.txt"


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
        if summary.preserved_split_paths:
            playlist_lines.append("  Preserved split CSVs:")
            playlist_lines.extend(f"    - {path}" for path in summary.preserved_split_paths)
        if summary.warnings:
            playlist_lines.append("  Warnings:")
            playlist_lines.extend(f"    - {warning}" for warning in summary.warnings)
    lines.extend(format_report_section("Playlists", playlist_lines or ["- None"]))
    write_text_report(path, lines)


def print_summary(report_path: Path, summaries: Sequence[PlaylistSplitSummary]) -> None:
    print_report_section(
        "Files",
        [
            f"Report: {report_path}",
        ],
    )
    print_report_section(
        "Processed",
        [
            f"Playlists: {len(summaries)}",
            f"Split CSVs written: {sum(len(summary.written_split_paths) for summary in summaries)}",
        ],
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY, help="Directory containing playlist folders. Defaults to collection/playlists.")
    parser.add_argument("--report", type=Path, help="Text report path. Defaults to reports/playlist_splits_<timestamp>_report.txt.")
    parser.add_argument("--regenerate", nargs="?", const="all", help="Playlist folder/display name to regenerate, or all. Defaults to stable update for all playlists.")
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS_PER_SPLIT, help="Maximum rows per split CSV. Defaults to 500.")
    args = parser.parse_args(argv)
    args.report = args.report or default_report_path()
    return args


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        target = args.regenerate or "all"
        if args.regenerate is None:
            summaries = update_playlist_splits_with_report(
                output_directory=args.output_dir,
                report_path=args.report,
                target=target,
                max_rows=args.max_rows,
            )
        else:
            summaries = regenerate_playlist_splits(
                output_directory=args.output_dir,
                report_path=args.report,
                target=target,
                max_rows=args.max_rows,
            )
    except (FileNotFoundError, NotADirectoryError, ValueError, csv.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print_summary(args.report, summaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
