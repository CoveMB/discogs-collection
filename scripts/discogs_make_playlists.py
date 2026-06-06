#!/usr/bin/env python3
"""Run Discogs enrichment, playlist mapping, and playlist export in order."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import discogs_playlist_exporter as exporter
import discogs_playlist_mapper as mapper
import discogs_style_enricher as enricher


StepMain = Callable[[Sequence[str] | None], int]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, help="Specific Discogs collection export CSV passed to the enricher.")
    parser.add_argument("--input-dir", type=Path, help="Folder containing one Discogs export CSV passed to the enricher.")
    parser.add_argument("--processed-dir", type=Path, help="Folder where default-folder exports are moved after enrichment.")
    parser.add_argument("--master", type=Path, help="Enriched master CSV used by all three steps.")
    parser.add_argument("--config", type=Path, help="Playlist map JSON passed to the mapper.")
    parser.add_argument("--playlist-output-dir", type=Path, help="Directory for per-playlist TuneMyMusic CSV files.")
    parser.add_argument("--enrichment-cache", type=Path, help="Discogs style and genre lookup cache JSON.")
    parser.add_argument("--tracklist-cache", type=Path, help="Discogs tracklist lookup cache JSON.")
    parser.add_argument("--enrichment-report", type=Path, help="Enrichment report path.")
    parser.add_argument("--mapping-report", type=Path, help="Playlist mapping report path.")
    parser.add_argument("--playlist-report", type=Path, help="Playlist export report path.")
    parser.add_argument("--refresh-existing", action="store_true", help="Ask the enricher to replace existing Style and Genre values.")
    parser.add_argument("--no-seen-terms", action="store_true", help="Disable seen Discogs terms tracking in the enricher.")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress output in enrichment and playlist export.")
    parser.add_argument("--timeout-seconds", type=int, help="HTTP timeout per Discogs request for enrichment and playlist export.")
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        help="Minimum delay between Discogs requests for enrichment and playlist export.",
    )
    parser.add_argument("--max-workers", type=int, help="Maximum concurrent uncached enrichment lookups.")
    args = parser.parse_args(argv)
    validate_args(parser, args)
    return args


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.timeout_seconds is not None and args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")
    if args.request_interval_seconds is not None and args.request_interval_seconds < 0:
        parser.error("--request-interval-seconds must be non-negative")
    if args.max_workers is not None and args.max_workers < 1:
        parser.error("--max-workers must be at least 1")


def append_option(arguments: list[str], option: str, value: object | None) -> None:
    if value is None:
        return
    arguments.extend([option, str(value)])


def build_enricher_args(args: argparse.Namespace) -> list[str]:
    arguments: list[str] = []
    append_option(arguments, "--export", args.export)
    append_option(arguments, "--input-dir", args.input_dir)
    append_option(arguments, "--processed-dir", args.processed_dir)
    append_option(arguments, "--master", args.master)
    append_option(arguments, "--cache", args.enrichment_cache)
    append_option(arguments, "--report", args.enrichment_report)
    if args.refresh_existing:
        arguments.append("--refresh-existing")
    if args.no_seen_terms:
        arguments.append("--no-seen-terms")
    if args.no_progress:
        arguments.append("--no-progress")
    append_option(arguments, "--timeout-seconds", args.timeout_seconds)
    append_option(arguments, "--request-interval-seconds", args.request_interval_seconds)
    append_option(arguments, "--max-workers", args.max_workers)
    return arguments


def build_mapper_args(args: argparse.Namespace) -> list[str]:
    arguments: list[str] = []
    if args.master is not None:
        append_option(arguments, "--input", args.master)
        append_option(arguments, "--output", args.master)
    append_option(arguments, "--config", args.config)
    append_option(arguments, "--report", args.mapping_report)
    return arguments


def build_exporter_args(args: argparse.Namespace) -> list[str]:
    arguments: list[str] = []
    append_option(arguments, "--input", args.master)
    append_option(arguments, "--output-dir", args.playlist_output_dir)
    append_option(arguments, "--cache", args.tracklist_cache)
    append_option(arguments, "--report", args.playlist_report)
    if args.no_progress:
        arguments.append("--no-progress")
    append_option(arguments, "--timeout-seconds", args.timeout_seconds)
    append_option(arguments, "--request-interval-seconds", args.request_interval_seconds)
    return arguments


def run_step(label: str, step_main: StepMain, step_args: Sequence[str]) -> int:
    print(f"Running {label}...")
    exit_code = step_main(step_args)
    return 0 if exit_code is None else int(exit_code)


def run_pipeline(args: argparse.Namespace) -> int:
    steps: tuple[tuple[str, StepMain, list[str]], ...] = (
        ("Discogs style enricher", enricher.main, build_enricher_args(args)),
        ("Discogs playlist mapper", mapper.main, build_mapper_args(args)),
        ("Discogs playlist exporter", exporter.main, build_exporter_args(args)),
    )
    for index, (label, step_main, step_args) in enumerate(steps):
        exit_code = run_step(label, step_main, step_args)
        if exit_code == 0:
            continue
        if index + 1 < len(steps):
            next_label = steps[index + 1][0]
            print(
                f"Stopping before {next_label} because {label} exited with code {exit_code}.",
                file=sys.stderr,
            )
        return exit_code
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
