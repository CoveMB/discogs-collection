#!/usr/bin/env python3
"""Add curated playlist labels to an enriched Discogs collection CSV."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from shared.cli import console_section, print_cli_summary, run_cli
from shared.discogs_columns import GENRE_COLUMN, RELEASE_ID_COLUMN, STYLE_COLUMN, move_release_id_to_front
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
from shared.reports import (
    format_report_section,
    format_report_title,
    script_report_path,
    write_text_report,
)
from shared.text import split_unique_comma_separated as split_discogs_terms
from shared.workflow_paths import DEFAULT_ENRICHED_MASTER_PATH


PLAYLISTS_COLUMN = "Playlists"
DEFAULT_INPUT_PATH = DEFAULT_ENRICHED_MASTER_PATH


@dataclass(frozen=True)
class PlaylistMappingSummary:
    input_rows: int
    output_rows: int
    input_path: Path
    output_path: Path
    config_path: Path
    report_path: Path
    playlist_association_lines: tuple[str, ...] = ()


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
    term_keys = normalized_playlist_term_keys(terms, config.excluded_term_keys)
    playlist_names: list[str] = []
    for playlist_label in config.playlist_labels:
        alias_keys = config.alias_keys_by_label[playlist_label]
        if any(alias_key in term_keys for alias_key in alias_keys):
            playlist_names.append(playlist_label)
    return tuple(playlist_names)


def normalized_playlist_term_keys(
    terms: Sequence[str],
    excluded_term_keys: frozenset[str],
) -> frozenset[str]:
    term_keys: set[str] = set()
    for term in terms:
        term_key = normalize_term(term)
        if term_key and term_key not in excluded_term_keys:
            term_keys.add(term_key)
    return frozenset(term_keys)


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
    output_fieldnames = move_release_id_to_front(list(dict.fromkeys(fieldnames)))
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
    return script_report_path(__file__)


def ensure_config_file(path: Path) -> None:
    created = ensure_playlist_config_file(path)
    if not created:
        return
    print(format_playlist_config_overview(path, load_playlist_config(path), created=True))
    try:
        input("Fill the config, then press Enter to continue.")
    except EOFError as error:
        raise ValueError(f"created playlist map config at {path}; fill the playlist map config and rerun") from error


def ensure_enriched_collection_exists(path: Path) -> None:
    if path.exists():
        return
    collection_directory = path.parent
    raise FileNotFoundError(
        f"No enriched collection found {collection_directory} process your collection first with "
        "python3 scripts/discogs_style_enricher.py"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH, help="Enriched Discogs master CSV. Defaults to collection/enriched-collection.csv.")
    parser.add_argument("--output", type=Path, help="Output CSV. Defaults to --input.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Playlist map JSON. Defaults to config/playlist-map.json.")
    parser.add_argument("--report", type=Path, help="Text report path. Defaults to reports/<timestamp>_discogs_playlist_mapper.txt.")
    args = parser.parse_args(argv)
    args.output = args.output or args.input
    args.report = args.report or default_report_path(args.output)
    return args


def run_playlist_mapping(args: argparse.Namespace) -> PlaylistMappingSummary:
    ensure_enriched_collection_exists(args.input)
    ensure_config_file(args.config)
    config = load_playlist_config(args.config)
    rows, fieldnames = read_csv_file(args.input)
    output_fieldnames, output_rows = add_playlist_mappings(fieldnames, rows, config)
    write_csv_file(args.output, output_fieldnames, output_rows)
    playlist_association_lines = tuple(format_playlist_association_section_lines(output_rows))
    summary = PlaylistMappingSummary(
        input_rows=len(rows),
        output_rows=len(output_rows),
        input_path=args.input,
        output_path=args.output,
        config_path=args.config,
        report_path=args.report,
        playlist_association_lines=playlist_association_lines,
    )
    write_report(args.report, summary, output_rows)
    return summary


def write_report(
    path: Path,
    summary: PlaylistMappingSummary,
    rows: Sequence[Mapping[str, str]],
) -> None:
    lines = format_report_title("Discogs playlist mapping report")
    summary_lines = [
        f"- Input rows: {summary.input_rows}",
        f"- Output rows: {summary.output_rows}",
    ]
    file_lines = [
        f"- Input: {summary.input_path}",
        f"- Output: {summary.output_path}",
        f"- Config: {summary.config_path}",
    ]
    lines.extend(format_report_section("Summary", summary_lines))
    lines.extend(format_report_section("Files", file_lines))
    association_lines = summary.playlist_association_lines or tuple(format_playlist_association_section_lines(rows))
    lines.extend(format_report_section("Release to playlist associations", association_lines))
    write_text_report(path, lines)


def format_playlist_association_section_lines(rows: Sequence[Mapping[str, str]]) -> list[str]:
    association_lines: list[str] = []
    if rows:
        for index, row in enumerate(rows, start=1):
            if association_lines:
                association_lines.append("")
            association_lines.extend(format_playlist_association_lines(index, row))
    else:
        association_lines.append("- None")
    return association_lines


def format_playlist_association_lines(row_number: int, row: Mapping[str, str]) -> list[str]:
    release_id = str(row.get(RELEASE_ID_COLUMN, "") or "").strip()
    release_label_name = "Release ID" if release_id else "Row"
    release_label = release_id or str(row_number)
    artist = str(row.get("Artist", "") or "")
    title = str(row.get("Title", "") or "")
    playlists = str(row.get(PLAYLISTS_COLUMN, "") or "").strip() or "None"
    return [
        f"- {release_label_name}: {release_label}",
        f"  Artist: {artist}",
        f"  Title: {title}",
        f"  Playlists: {playlists}",
    ]


def print_summary(summary: PlaylistMappingSummary) -> None:
    extra_sections = []
    if summary.playlist_association_lines:
        extra_sections.append(
            console_section("Release to playlist associations", summary.playlist_association_lines)
        )
    print_cli_summary(
        files=[
            f"Output: {summary.output_path}",
            f"Report: {summary.report_path}",
        ],
        processed=[
            f"Input rows: {summary.input_rows}",
            f"Output rows: {summary.output_rows}",
        ],
        extra_sections=extra_sections,
    )


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(parse_args, run_playlist_mapping, print_summary, argv)


if __name__ == "__main__":
    raise SystemExit(main())
