#!/usr/bin/env python3
"""Add curated playlist labels to an enriched Discogs collection CSV."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from shared.files import read_csv_file, write_csv_file
from shared.playlist_config import (
    DEFAULT_CONFIG_PATH,
    PlaylistConfig,
    ensure_playlist_config_file,
    format_playlist_config_overview,
    load_playlist_config,
    normalize_playlist_config,
    normalize_term,
)
from shared.reports import readable_timestamp
from shared.text import split_unique_comma_separated as split_discogs_terms


STYLE_COLUMN = "Style"
GENRE_COLUMN = "Genre"
RELEASE_ID_COLUMN = "release_id"
PLAYLISTS_COLUMN = "Playlists"
DEFAULT_INPUT_PATH = Path("collection/enriched-collection.csv")
DEFAULT_REPORT_DIRECTORY = Path("reports")


@dataclass(frozen=True)
class PlaylistMappingSummary:
    input_rows: int
    output_rows: int
    input_path: Path
    output_path: Path
    config_path: Path
    report_path: Path


def map_row_playlists(row: Mapping[str, str], config: PlaylistConfig) -> str:
    style_playlists = map_terms_to_playlists(
        split_discogs_terms(str(row.get(STYLE_COLUMN, "") or "")),
        config,
    )
    if style_playlists:
        return ", ".join(style_playlists)

    genre_playlists = map_terms_to_playlists(
        split_discogs_terms(str(row.get(GENRE_COLUMN, "") or "")),
        config,
    )
    return ", ".join(genre_playlists)


def map_terms_to_playlists(terms: Sequence[str], config: PlaylistConfig) -> tuple[str, ...]:
    term_keys = {
        normalize_term(term)
        for term in terms
        if normalize_term(term) and normalize_term(term) not in config.excluded_term_keys
    }
    playlist_names: list[str] = []
    for playlist_label in config.playlist_labels:
        alias_keys = config.alias_keys_by_label[playlist_label]
        if any(alias_key in term_keys for alias_key in alias_keys):
            playlist_names.append(f"{config.playlist_prefix}{playlist_label}")
    return tuple(playlist_names)


def add_playlist_mappings(
    fieldnames: Sequence[str],
    rows: Sequence[Mapping[str, str]],
    config: PlaylistConfig,
) -> tuple[list[str], list[dict[str, str]]]:
    validate_input_fieldnames(fieldnames)
    output_fieldnames = build_playlist_output_fieldnames(fieldnames)
    output_rows: list[dict[str, str]] = []
    for row in rows:
        output_row = {fieldname: str(row.get(fieldname, "") or "") for fieldname in output_fieldnames}
        output_row[PLAYLISTS_COLUMN] = map_row_playlists(row, config)
        output_rows.append(output_row)
    return output_fieldnames, output_rows


def validate_input_fieldnames(fieldnames: Sequence[str]) -> None:
    if STYLE_COLUMN not in fieldnames and GENRE_COLUMN not in fieldnames:
        raise ValueError("input CSV must contain at least one of Style or Genre")


def build_playlist_output_fieldnames(fieldnames: Sequence[str]) -> list[str]:
    output_fieldnames = list(dict.fromkeys(fieldnames))
    if PLAYLISTS_COLUMN in output_fieldnames:
        return output_fieldnames
    if GENRE_COLUMN in output_fieldnames:
        output_fieldnames.insert(output_fieldnames.index(GENRE_COLUMN) + 1, PLAYLISTS_COLUMN)
    elif STYLE_COLUMN in output_fieldnames:
        output_fieldnames.insert(output_fieldnames.index(STYLE_COLUMN) + 1, PLAYLISTS_COLUMN)
    else:
        output_fieldnames.append(PLAYLISTS_COLUMN)
    return output_fieldnames


def default_report_path(output_path: Path) -> Path:
    return DEFAULT_REPORT_DIRECTORY / f"{output_path.stem}_{readable_timestamp()}_playlist_report.txt"


def ensure_config_file(path: Path) -> None:
    created = ensure_playlist_config_file(path)
    if not created:
        return
    print(format_playlist_config_overview(path, load_playlist_config(path), created=True))
    try:
        input("Fill the config, then press Enter to continue.")
    except EOFError as error:
        raise ValueError(f"created playlist map config at {path}; fill the playlist map config and rerun") from error


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH, help="Enriched Discogs master CSV. Defaults to collection/enriched-collection.csv.")
    parser.add_argument("--output", type=Path, help="Output CSV. Defaults to --input.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Playlist map JSON. Defaults to config/playlist-map.json.")
    parser.add_argument("--report", type=Path, help="Text report path. Defaults to reports/<output-name>_<timestamp>_playlist_report.txt.")
    args = parser.parse_args(argv)
    args.output = args.output or args.input
    args.report = args.report or default_report_path(args.output)
    return args


def run_playlist_mapping(args: argparse.Namespace) -> PlaylistMappingSummary:
    ensure_config_file(args.config)
    config = load_playlist_config(args.config)
    rows, fieldnames = read_csv_file(args.input)
    output_fieldnames, output_rows = add_playlist_mappings(fieldnames, rows, config)
    write_csv_file(args.output, output_fieldnames, output_rows)
    summary = PlaylistMappingSummary(
        input_rows=len(rows),
        output_rows=len(output_rows),
        input_path=args.input,
        output_path=args.output,
        config_path=args.config,
        report_path=args.report,
    )
    write_report(args.report, summary, output_rows)
    return summary


def write_report(
    path: Path,
    summary: PlaylistMappingSummary,
    rows: Sequence[Mapping[str, str]],
) -> None:
    lines = [
        "Discogs playlist mapping report",
        f"Input rows: {summary.input_rows}",
        f"Output rows: {summary.output_rows}",
        f"Input: {summary.input_path}",
        f"Output: {summary.output_path}",
        f"Config: {summary.config_path}",
        "",
        "Release to playlist associations:",
    ]
    lines.extend(format_playlist_association_line(index, row) for index, row in enumerate(rows, start=1))
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def format_playlist_association_line(row_number: int, row: Mapping[str, str]) -> str:
    release_id = str(row.get(RELEASE_ID_COLUMN, "") or "").strip()
    release_label = release_id or f"row {row_number}"
    artist = str(row.get("Artist", "") or "")
    title = str(row.get("Title", "") or "")
    playlists = str(row.get(PLAYLISTS_COLUMN, "") or "").strip() or "None"
    return f"- {release_label}: {artist} - {title} -> {playlists}"


def print_summary(summary: PlaylistMappingSummary) -> None:
    print(f"Output: {summary.output_path}")
    print(f"Report: {summary.report_path}")
    print(f"Input rows: {summary.input_rows}")
    print(f"Output rows: {summary.output_rows}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        summary = run_playlist_mapping(args)
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
