#!/usr/bin/env python3
"""Run Discogs enrichment, playlist mapping, and playlist export in order."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import discogs_playlist_exporter as exporter
import discogs_playlist_mapper as mapper
import discogs_playlist_splitter as splitter
import discogs_style_enricher as enricher
from publishers.spotify import publish_playlist as spotify_publisher
from shared.cli import console_section, print_console_sections, print_step_header
from shared.cli_args import append_cli_option as append_option
from shared.debug_log import DebugLog, build_debug_logger
from shared.publisher_config import (
    DEFAULT_PUBLISHER_CONFIG_PATH,
    NO_PUBLISHER,
    PUBLISHER_CHOICES,
    SPOTIFY_PUBLISHER,
    load_or_create_publisher_config,
    publishing_publishers,
)


StepMain = Callable[[Sequence[str] | None], int]
PipelineStep = tuple[str, StepMain, list[str]]
SUPPORTED_MAIN_PUBLISHERS = PUBLISHER_CHOICES
DEBUG_PATH_FIELDS = (
    "export",
    "input_dir",
    "processed_dir",
    "master",
    "config",
    "workflow_config",
    "playlist_output_dir",
    "enrichment_cache",
    "tracklist_cache",
    "enrichment_report",
    "mapping_report",
    "playlist_report",
    "split_report",
    "publisher_config",
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, help="Specific Discogs collection export CSV passed to the enricher.")
    parser.add_argument("--input-dir", type=Path, help="Folder containing one Discogs export CSV passed to the enricher.")
    parser.add_argument("--processed-dir", type=Path, help="Folder where default-folder exports are moved after enrichment.")
    parser.add_argument("--master", type=Path, help="Enriched master CSV used by all three steps.")
    parser.add_argument("--config", type=Path, help="Playlist map JSON passed to the mapper.")
    parser.add_argument("--workflow-config", type=Path, help="Workflow JSON config passed to the splitter.")
    parser.add_argument("--playlist-output-dir", type=Path, help="Directory for per-playlist TuneMyMusic CSV files.")
    parser.add_argument("--enrichment-cache", type=Path, help="Discogs style and genre lookup cache JSON.")
    parser.add_argument("--tracklist-cache", type=Path, help="Discogs tracklist lookup cache JSON.")
    parser.add_argument("--enrichment-report", type=Path, help="Enrichment report path.")
    parser.add_argument("--mapping-report", type=Path, help="Playlist mapping report path.")
    parser.add_argument("--playlist-report", type=Path, help="Playlist export report path.")
    parser.add_argument("--split-report", type=Path, help="Playlist split report path.")
    parser.add_argument("--debug-log", type=Path, help="Write sanitized make-playlists pipeline debug logs to this path.")
    parser.add_argument("--regenerate-splits", help="Playlist folder/display name/path to regenerate, or all, passed to the splitter.")
    parser.add_argument("--publisher-config", type=Path, default=DEFAULT_PUBLISHER_CONFIG_PATH, help="Publisher JSON config. Defaults to config/publisher.json.")
    parser.add_argument("--publisher", choices=SUPPORTED_MAIN_PUBLISHERS, help="Publisher override for the workflow. Omit to use default_publisher from the publisher config.")
    parser.add_argument("--publishing-dry-run", action="store_true", help="Preview playlist publishing without creating or updating Spotify playlists.")
    parser.add_argument("--refresh-existing", action="store_true", help="Ask the enricher to replace existing Style and Genre values.")
    parser.add_argument("--no-seen-terms", action="store_true", help="Disable seen Discogs terms tracking in the enricher.")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress output in enrichment, playlist export, and Spotify publishing.")
    parser.add_argument("--timeout-seconds", type=int, help="HTTP timeout per Discogs request for enrichment and playlist export.")
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        help="Minimum delay between Discogs requests for enrichment and playlist export.",
    )
    parser.add_argument("--max-workers", type=int, help="Maximum concurrent uncached enrichment lookups.")
    parser.add_argument("--max-rows", type=int, help="Maximum rows per split CSV, overriding workflow config.")
    parser.add_argument(
        "--max-new-searches-per-run",
        type=int,
        help=(
            "Maximum uncached Spotify searches per publisher run. "
            "Passed to the Spotify publisher; use 0 for unlimited."
        ),
    )
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
    if args.max_rows is not None and args.max_rows < 1:
        parser.error("--max-rows must be at least 1")
    if args.max_new_searches_per_run is not None and args.max_new_searches_per_run < 0:
        parser.error("--max-new-searches-per-run must be non-negative")


def log_pipeline_context(args: argparse.Namespace, debug_log: DebugLog) -> None:
    for field_name in DEBUG_PATH_FIELDS:
        debug_log(f"path {field_name}={format_debug_value(getattr(args, field_name))}")
    debug_log(
        "options "
        f"refresh_existing={args.refresh_existing} no_seen_terms={args.no_seen_terms} "
        f"publishing_dry_run={args.publishing_dry_run} "
        f"no_progress={args.no_progress} timeout_seconds={format_debug_value(args.timeout_seconds)} "
        f"request_interval_seconds={format_debug_value(args.request_interval_seconds)} "
        f"max_workers={format_debug_value(args.max_workers)} max_rows={format_debug_value(args.max_rows)} "
        f"max_new_searches_per_run={format_debug_value(args.max_new_searches_per_run)} "
        f"regenerate_splits={format_debug_value(args.regenerate_splits)}"
    )


def format_debug_value(value: object | None) -> str:
    if value is None:
        return "(default)"
    return str(value)


def step_option_names(step_args: Sequence[str]) -> str:
    option_names = [argument for argument in step_args if argument.startswith("--")]
    return ",".join(option_names) if option_names else "(none)"


def available_playlist_publishers(supported_publishers: Sequence[str] | None = None) -> tuple[str, ...]:
    return publishing_publishers(SUPPORTED_MAIN_PUBLISHERS if supported_publishers is None else supported_publishers)


def publisher_disabled_message() -> str:
    return (
        "Playlist publishing skipped because the resolved publisher is none. "
        f"Run with --publisher {', '.join(available_playlist_publishers())} to publish the playlist."
    )


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


def build_splitter_args(args: argparse.Namespace) -> list[str]:
    arguments: list[str] = []
    append_option(arguments, "--output-dir", args.playlist_output_dir)
    append_option(arguments, "--report", args.split_report)
    append_option(arguments, "--workflow-config", args.workflow_config)
    append_option(arguments, "--regenerate", args.regenerate_splits)
    append_option(arguments, "--max-rows", args.max_rows)
    return arguments


def build_spotify_publisher_args(args: argparse.Namespace) -> list[str]:
    return spotify_publisher.build_spotify_publisher_argv(
        playlist_output_dir=args.playlist_output_dir,
        publisher_config=args.publisher_config,
        no_progress=args.no_progress,
        dry_run=args.publishing_dry_run,
        max_new_searches_per_run=args.max_new_searches_per_run,
    )


def resolve_publisher(args: argparse.Namespace) -> str:
    if args.publisher:
        return args.publisher
    publisher_config = load_or_create_publisher_config(args.publisher_config)
    return publisher_config.default_publisher


def skip_publisher(_argv: Sequence[str] | None = None) -> int:
    print_console_sections([console_section("Publisher", [publisher_disabled_message()])])
    return 0


def build_publisher_step(args: argparse.Namespace) -> PipelineStep:
    if args.publisher == NO_PUBLISHER:
        return ("Playlist publisher", skip_publisher, [])
    if args.publisher == SPOTIFY_PUBLISHER:
        return ("Spotify playlist publisher", spotify_publisher.main, build_spotify_publisher_args(args))
    raise ValueError(f"unsupported publisher: {args.publisher}")


def build_pipeline_steps(args: argparse.Namespace) -> tuple[PipelineStep, ...]:
    return (
        ("Discogs style enricher", enricher.main, build_enricher_args(args)),
        ("Discogs playlist mapper", mapper.main, build_mapper_args(args)),
        ("Discogs playlist exporter", exporter.main, build_exporter_args(args)),
        ("Discogs playlist splitter", splitter.main, build_splitter_args(args)),
        build_publisher_step(args),
    )


def run_step(
    label: str,
    step_main: StepMain,
    step_args: Sequence[str],
    step_index: int = 1,
    total_steps: int = 1,
    debug_log: DebugLog | None = None,
) -> int:
    if debug_log:
        debug_log(
            f"step_start index={step_index} total={total_steps} label={label} "
            f"arg_count={len(step_args)} options={step_option_names(step_args)}"
        )
    print_step_header(label, step_index=step_index, total_steps=total_steps)
    exit_code = step_main(step_args)
    normalized_exit_code = 0 if exit_code is None else int(exit_code)
    if debug_log:
        debug_log(f"step_end index={step_index} total={total_steps} label={label} exit_code={normalized_exit_code}")
    return normalized_exit_code


def run_pipeline(
    args: argparse.Namespace,
    debug_log: DebugLog | None = None,
) -> int:
    steps = build_pipeline_steps(args)
    if debug_log:
        debug_log(f"pipeline_steps count={len(steps)}")
    for index, (label, step_main, step_args) in enumerate(steps):
        exit_code = run_step(
            label=label,
            step_main=step_main,
            step_args=step_args,
            step_index=index + 1,
            total_steps=len(steps),
            debug_log=debug_log,
        )
        if exit_code == 0:
            continue
        if index + 1 < len(steps):
            next_label = steps[index + 1][0]
            if debug_log:
                debug_log(f"stopping failed_step={label} next_step={next_label} exit_code={exit_code}")
            print(
                f"Stopping before {next_label} because {label} exited with code {exit_code}.",
                file=sys.stderr,
            )
        elif debug_log:
            debug_log(f"stopping failed_step={label} next_step=(none) exit_code={exit_code}")
        return exit_code
    if debug_log:
        debug_log("pipeline_completed exit_code=0")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    debug_log: DebugLog | None = None
    try:
        args = parse_args(argv)
        debug_log = build_debug_logger(args.debug_log)
        if debug_log:
            debug_log("start discogs_make_playlists")
            log_pipeline_context(args, debug_log)
        args.publisher = resolve_publisher(args)
        if debug_log:
            debug_log(f"resolved_publisher value={args.publisher}")
    except ValueError as error:
        if debug_log:
            debug_log(f"error type={type(error).__name__}")
        print(f"Error: {error}", file=sys.stderr)
        return 1
    exit_code = run_pipeline(args, debug_log)
    if debug_log:
        debug_log(f"completed exit_code={exit_code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
