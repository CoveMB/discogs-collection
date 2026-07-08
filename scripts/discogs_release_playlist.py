#!/usr/bin/env python3
"""Create an isolated publisher playlist from explicit Discogs release IDs."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import discogs_playlist_exporter as exporter
import discogs_tracklists as tracklists
from publishers.spotify import publish_playlist as spotify_publisher
from publishers.spotify.env import DEFAULT_ENV_PATH, DEFAULT_TOKEN_CACHE_PATH
from shared.cli import EXPECTED_CLI_ERRORS, run_cli
from shared.debug_log import DebugLog, build_debug_logger
from shared.discogs_columns import RELEASE_ID_COLUMN
from shared.files import write_json_file
from shared.playlist_selection import (
    ensure_path_inside_output_directory,
    playlist_master_path,
    safe_playlist_filename,
)
from shared.progress import ProgressReporter
from shared.publisher_config import (
    DEFAULT_PUBLISHER_CONFIG_PATH,
    NO_PUBLISHER,
    PUBLISHER_CHOICES,
    SPOTIFY_PUBLISHER,
    PublisherConfig,
    load_or_create_publisher_config,
)
from shared.cli import print_cli_summary
from shared.reports import format_report_section, format_report_title, script_report_path, write_text_report
from shared.workflow_paths import (
    DEFAULT_ON_THE_FLY_PLAYLIST_DIRECTORY,
    DEFAULT_SPOTIFY_MATCH_CACHE_PATH,
    DEFAULT_TRACKLIST_CACHE_PATH,
)


PLAYLISTS_COLUMN = exporter.PLAYLISTS_COLUMN
DEFAULT_OUTPUT_DIRECTORY = DEFAULT_ON_THE_FLY_PLAYLIST_DIRECTORY
DEFAULT_MATCH_CACHE_PATH = DEFAULT_SPOTIFY_MATCH_CACHE_PATH
DEFAULT_USER_AGENT = "DiscogsReleasePlaylist/1.0 +https://www.discogs.com"
SUPPORTED_PUBLISHERS = PUBLISHER_CHOICES
RELEASE_PLAYLIST_METADATA_FILENAME = ".release-playlist.json"
RELEASE_PLAYLIST_METADATA_SCHEMA_VERSION = 1
RELEASE_PLAYLIST_METADATA_RECORD_TYPE = "discogs_release_playlist"
RELEASE_PLAYLIST_FIELDNAMES = (
    RELEASE_ID_COLUMN,
    "Artist",
    "Title",
    "Released",
    PLAYLISTS_COLUMN,
)


@dataclass(frozen=True)
class ReleasePlaylistSummary:
    playlist_name: str
    release_ids: tuple[str, ...]
    duplicate_release_ids: tuple[str, ...]
    output_directory: Path
    master_path: Path
    report_path: Path
    publisher: str
    export_summary: exporter.PlaylistExportSummary
    publisher_summary: spotify_publisher.SpotifyPublishSummary | None = None


def create_release_playlist(
    playlist_name: str,
    release_ids: Sequence[str],
    output_directory: Path,
    report_path: Path,
    lookup_tracklist: Callable[[Mapping[str, str]], tracklists.ReleaseTracklistLookup],
    progress: ProgressReporter | None = None,
) -> ReleasePlaylistSummary:
    clean_playlist_name = normalize_playlist_name(playlist_name)
    master_path, metadata_path = resolve_release_playlist_target(clean_playlist_name, output_directory)
    ensure_release_playlist_target_available(clean_playlist_name, master_path, metadata_path)
    unique_release_ids, duplicate_release_ids = dedupe_release_ids(release_ids)
    if not unique_release_ids:
        raise ValueError("at least one release_id is required")

    rows = [
        {
            RELEASE_ID_COLUMN: release_id,
            "Artist": "",
            "Title": "",
            "Released": "",
            PLAYLISTS_COLUMN: clean_playlist_name,
        }
        for release_id in unique_release_ids
    ]
    export_summary = exporter.export_playlist_rows(
        rows=rows,
        fieldnames=RELEASE_PLAYLIST_FIELDNAMES,
        input_path=Path("release-ids"),
        output_directory=output_directory,
        report_path=report_path,
        lookup_tracklist=lookup_tracklist,
        progress=progress,
        include_stale_playlists=False,
    )
    if len(export_summary.playlist_files) != 1:
        raise ValueError(f"expected one generated playlist, got {len(export_summary.playlist_files)}")
    if export_summary.playlist_files[0].path != master_path:
        raise ValueError(f"generated playlist path changed unexpectedly: {export_summary.playlist_files[0].path}")

    summary = ReleasePlaylistSummary(
        playlist_name=clean_playlist_name,
        release_ids=unique_release_ids,
        duplicate_release_ids=duplicate_release_ids,
        output_directory=output_directory,
        master_path=export_summary.playlist_files[0].path,
        report_path=report_path,
        publisher="none",
        export_summary=export_summary,
    )
    write_release_playlist_metadata(metadata_path, clean_playlist_name)
    write_release_playlist_report(summary)
    return summary


def dedupe_release_ids(release_ids: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    unique_release_ids: list[str] = []
    duplicate_release_ids: list[str] = []
    seen_release_ids: set[str] = set()
    for release_id in release_ids:
        normalized_release_id = normalize_release_id(release_id)
        if normalized_release_id in seen_release_ids:
            duplicate_release_ids.append(normalized_release_id)
            continue
        seen_release_ids.add(normalized_release_id)
        unique_release_ids.append(normalized_release_id)
    return tuple(unique_release_ids), tuple(duplicate_release_ids)


def normalize_release_id(release_id: object) -> str:
    text = str(release_id or "").strip()
    if not text:
        raise ValueError("release_id values cannot be blank")
    if not re.fullmatch(r"\d+", text):
        raise ValueError(f"invalid release_id {text}: must be a positive integer")
    normalized = str(int(text))
    if normalized == "0":
        raise ValueError("release_id values must be positive integers")
    return normalized


def normalize_playlist_name(playlist_name: str) -> str:
    clean_playlist_name = str(playlist_name or "").strip()
    if not clean_playlist_name:
        raise ValueError("playlist name cannot be blank")
    return clean_playlist_name


def resolve_release_playlist_target(playlist_name: str, output_directory: Path) -> tuple[Path, Path]:
    safe_folder_name = safe_playlist_filename(playlist_name)
    if safe_folder_name in {".", ".."}:
        raise ValueError(f"{playlist_name}: playlist name resolves outside output directory {output_directory}")
    existing_folder_paths = exporter.existing_playlist_folder_paths_by_safe_name(output_directory)
    folder_path = existing_folder_paths.get(safe_folder_name.casefold(), output_directory / safe_folder_name)
    if folder_path.is_symlink():
        raise ValueError(f"{folder_path}: release playlist folder symlinks are not supported")
    if folder_path.exists() and not folder_path.is_dir():
        raise ValueError(f"{folder_path}: release playlist path exists and is not a directory")
    master_path = playlist_master_path(folder_path)
    ensure_path_inside_output_directory(output_directory, master_path)
    return master_path, folder_path / RELEASE_PLAYLIST_METADATA_FILENAME


def ensure_release_playlist_target_available(
    playlist_name: str,
    master_path: Path,
    metadata_path: Path,
) -> None:
    if metadata_path.is_symlink():
        raise ValueError(f"{metadata_path}: release playlist metadata symlinks are not supported")
    if metadata_path.exists():
        existing_playlist_name = read_release_playlist_metadata_name(metadata_path)
        if existing_playlist_name != playlist_name:
            raise ValueError(
                f"{master_path.parent}: already used by playlist name {existing_playlist_name!r}; "
                f"choose a name with a distinct safe folder path"
            )
        return

    if master_path.exists() and master_path.parent.name != playlist_name:
        raise ValueError(
            f"{master_path.parent}: existing playlist folder has no release-playlist metadata; "
            "choose a name with a distinct safe folder path or remove the old folder"
        )


def read_release_playlist_metadata_name(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: malformed release playlist metadata") from error
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != RELEASE_PLAYLIST_METADATA_SCHEMA_VERSION
        or payload.get("record_type") != RELEASE_PLAYLIST_METADATA_RECORD_TYPE
    ):
        raise ValueError(f"{path}: unsupported release playlist metadata")
    playlist_name = payload.get("playlist_name")
    if not isinstance(playlist_name, str) or not playlist_name.strip():
        raise ValueError(f"{path}: release playlist metadata is missing playlist_name")
    return playlist_name.strip()


def write_release_playlist_metadata(path: Path, playlist_name: str) -> None:
    write_json_file(
        path,
        {
            "schema_version": RELEASE_PLAYLIST_METADATA_SCHEMA_VERSION,
            "record_type": RELEASE_PLAYLIST_METADATA_RECORD_TYPE,
            "playlist_name": playlist_name,
        },
    )


def release_ids_from_file(path: Path) -> tuple[str, ...]:
    return tuple(value for value in re.split(r"[\s,]+", path.read_text(encoding="utf-8")) if value)


def release_ids_from_args(args: argparse.Namespace) -> tuple[str, ...]:
    release_ids: list[str] = list(args.release_ids)
    if args.release_ids_file is not None:
        release_ids.extend(release_ids_from_file(args.release_ids_file))
    return tuple(release_ids)


def resolve_publisher(args: argparse.Namespace) -> str:
    if args.publisher:
        return args.publisher
    publisher_config = load_or_create_publisher_config(args.publisher_config)
    return publisher_config.default_publisher


def ad_hoc_publisher_config() -> PublisherConfig:
    return PublisherConfig(default_publisher=SPOTIFY_PUBLISHER, playlist_prefix="", playlist_suffix="")


def build_spotify_publisher_args(args: argparse.Namespace) -> argparse.Namespace:
    return spotify_publisher.build_spotify_publisher_namespace(
        env_file=args.env_file,
        playlist_output_dir=args.output_dir,
        report=args.publisher_report,
        token_cache=args.token_cache,
        match_cache=args.match_cache,
        publisher_config=args.publisher_config,
        debug_log=args.debug_log,
        reauthorize=args.reauthorize,
        access_token=args.access_token,
        playlists=None,
        search_limit=args.search_limit,
        max_new_searches_per_run=args.max_new_searches_per_run,
        publisher_sync_mode=args.publisher_sync_mode,
        refresh_match_cache=args.refresh_match_cache,
        dry_run=args.publishing_dry_run,
        progress=args.progress,
    )


def publish_release_playlist(
    args: argparse.Namespace,
    master_path: Path,
    playlist_name: str,
    debug_log: DebugLog | None = None,
) -> spotify_publisher.SpotifyPublishSummary:
    publisher_args = build_spotify_publisher_args(args)
    return spotify_publisher.run_spotify_publish_from_args(
        publisher_args,
        playlist_master_paths=(master_path,),
        playlist_names_by_master_path={master_path: playlist_name},
        publisher_config=ad_hoc_publisher_config(),
        debug_log=debug_log,
    )


def run_release_playlist(args: argparse.Namespace) -> ReleasePlaylistSummary:
    debug_log = build_debug_logger(args.debug_log)
    if debug_log:
        debug_log("start discogs_release_playlist")
        debug_log(
            "options "
            f"release_ids={len(args.release_ids)} release_ids_file={format_debug_value(args.release_ids_file)} "
            f"output_dir={format_debug_value(args.output_dir)} publisher_override={format_debug_value(args.publisher)} "
            f"publishing_dry_run={args.publishing_dry_run} progress={args.progress}"
        )
    release_ids = release_ids_from_args(args)
    publisher = resolve_publisher(args)
    if debug_log:
        debug_log(f"resolved_publisher value={publisher}")
    lookup_tracklist = tracklists.make_cached_tracklist_lookup(
        cache_path=args.tracklist_cache,
        token=args.discogs_token,
        user_agent=args.user_agent,
        timeout_seconds=args.timeout_seconds,
        request_interval_seconds=args.request_interval_seconds,
    )
    progress = ProgressReporter(label="Exporting release playlist rows") if args.progress else None
    summary = create_release_playlist(
        playlist_name=args.name,
        release_ids=release_ids,
        output_directory=args.output_dir,
        report_path=args.report,
        lookup_tracklist=lookup_tracklist,
        progress=progress,
    )
    if publisher == NO_PUBLISHER:
        summary = replace(summary, publisher=publisher)
        write_release_playlist_report(summary)
        return summary
    if publisher != SPOTIFY_PUBLISHER:
        raise ValueError(f"unsupported publisher: {publisher}")

    summary = replace(summary, publisher=publisher)
    write_release_playlist_report(summary)
    publisher_summary = publish_release_playlist(args, summary.master_path, summary.playlist_name, debug_log=debug_log)
    summary = replace(summary, publisher_summary=publisher_summary)
    write_release_playlist_report(summary)
    return summary


def format_debug_value(value: object | None) -> str:
    if value is None:
        return "(default)"
    return str(value)


def write_release_playlist_report(summary: ReleasePlaylistSummary) -> None:
    lines = format_report_title("Discogs release playlist report")
    lines.extend(
        format_report_section(
            "Summary",
            [
                f"- Playlist: {summary.playlist_name}",
                f"- Unique release IDs: {len(summary.release_ids)}",
                f"- Duplicate release IDs skipped: {len(summary.duplicate_release_ids)}",
                f"- Publisher: {summary.publisher}",
                f"- Output track rows: {summary.export_summary.track_row_count}",
                f"- Release-level fallback rows: {summary.export_summary.fallback_row_count}",
            ],
        )
    )
    file_lines = [
        f"- Output directory: {summary.output_directory}",
        f"- Master CSV: {summary.master_path}",
        f"- Report: {summary.report_path}",
        "- Collection master: not read or written",
    ]
    if summary.publisher_summary is not None:
        file_lines.append(f"- Publisher report: {summary.publisher_summary.report_path}")
    lines.extend(format_report_section("Files", file_lines))
    duplicate_lines = [f"- {release_id}" for release_id in summary.duplicate_release_ids] or ["- None"]
    lines.extend(format_report_section("Duplicate release IDs", duplicate_lines))
    lines.extend(format_report_section("Review notes", list(summary.export_summary.review_notes) or ["- None"]))
    write_text_report(summary.report_path, lines)


def print_summary(summary: ReleasePlaylistSummary) -> None:
    print_cli_summary(
        files=[
            f"Master CSV: {summary.master_path}",
            f"Report: {summary.report_path}",
            *(
                [f"Publisher report: {summary.publisher_summary.report_path}"]
                if summary.publisher_summary is not None
                else []
            ),
        ],
        processed=[
            f"Playlist: {summary.playlist_name}",
            f"Unique release IDs: {len(summary.release_ids)}",
            f"Duplicate release IDs skipped: {len(summary.duplicate_release_ids)}",
            f"Output track rows: {summary.export_summary.track_row_count}",
            f"Publisher: {summary.publisher}",
        ],
    )


def default_report_path() -> Path:
    return script_report_path(__file__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_ids", nargs="*", help="Discogs release IDs to export into the playlist.")
    parser.add_argument("--name", required=True, help="Playlist name to create or update under the on-the-fly playlist folder.")
    parser.add_argument("--release-ids-file", type=Path, help="Text file containing release IDs separated by whitespace or commas.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY, help="Directory for on-the-fly playlist folders. Defaults to collection/playlists/on-the-fly.")
    parser.add_argument("--report", type=Path, help="Release playlist report path. Defaults to reports/<timestamp>_discogs_release_playlist.txt.")
    parser.add_argument("--tracklist-cache", type=Path, default=DEFAULT_TRACKLIST_CACHE_PATH, help="Discogs tracklist cache JSON. Defaults to collection/cache/playlist-tracks.cache.json.")
    parser.add_argument("--discogs-token", default=os.environ.get("DISCOGS_TOKEN", ""), help="Optional Discogs personal access token. Defaults to DISCOGS_TOKEN.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent sent to Discogs.")
    parser.add_argument("--timeout-seconds", type=int, default=30, help="HTTP timeout per Discogs request.")
    parser.add_argument("--request-interval-seconds", type=float, default=exporter.DEFAULT_REQUEST_INTERVAL_SECONDS, help="Minimum delay between Discogs requests. Defaults to header-aware throttling.")
    parser.add_argument("--publisher-config", type=Path, default=DEFAULT_PUBLISHER_CONFIG_PATH, help="Publisher JSON config. Defaults to config/publisher.json.")
    parser.add_argument("--publisher", choices=SUPPORTED_PUBLISHERS, help="Publisher override. Omit to use default_publisher from the publisher config.")
    parser.add_argument("--publisher-report", type=Path, help="Spotify publisher report path. Defaults to reports/<timestamp>_publish_playlist.txt.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH, help="Local env file containing Spotify app settings. Defaults to .env.")
    parser.add_argument("--token-cache", type=Path, default=DEFAULT_TOKEN_CACHE_PATH, help="Spotify token cache path.")
    parser.add_argument("--match-cache", type=Path, default=DEFAULT_MATCH_CACHE_PATH, help="Spotify track match cache path.")
    parser.add_argument("--reauthorize", action="store_true", help="Force a fresh Spotify login before running the publisher.")
    parser.add_argument("--access-token", help=argparse.SUPPRESS)
    parser.add_argument("--search-limit", type=int, default=10, help="Spotify search result limit per track. Defaults to 10.")
    parser.add_argument(
        "--max-new-searches-per-run",
        type=int,
        default=spotify_publisher.DEFAULT_MAX_NEW_SEARCHES_PER_RUN,
        help=(
            "Maximum uncached Spotify searches per publisher run. "
            "Defaults to 500. Use 0 for unlimited."
        ),
    )
    parser.add_argument("--publisher-sync-mode", choices=spotify_publisher.PUBLISHER_SYNC_MODES, default=spotify_publisher.APPEND_SYNC_MODE, help="Publisher sync mode. append adds missing tracks; replace replaces playlist contents. Defaults to append.")
    parser.add_argument("--refresh-match-cache", action="store_true", help="Recheck every generated playlist row with Spotify and update the local track match cache.")
    parser.add_argument("--publishing-dry-run", action="store_true", help="Preview Spotify playlist changes without creating or updating playlists.")
    parser.add_argument("--debug-log", type=Path, help="Write sanitized release playlist debug logs to this path.")
    parser.add_argument("--no-progress", action="store_false", dest="progress", help="Disable terminal progress output.")
    args = parser.parse_args(argv)
    validate_args(parser, args)
    args.report = args.report or default_report_path()
    args.publisher_report = args.publisher_report or spotify_publisher.default_report_path()
    return args


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")
    if args.request_interval_seconds < 0:
        parser.error("--request-interval-seconds must be non-negative")
    if args.search_limit < 1 or args.search_limit > 10:
        parser.error("--search-limit must be between 1 and 10")
    if args.max_new_searches_per_run < 0:
        parser.error("--max-new-searches-per-run must be non-negative")
    if not args.release_ids and args.release_ids_file is None:
        parser.error("at least one release_id or --release-ids-file is required")


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(
        parse_args,
        run_release_playlist,
        print_summary,
        argv,
        expected_errors=(*EXPECTED_CLI_ERRORS, spotify_publisher.SpotifyApiError),
    )


if __name__ == "__main__":
    raise SystemExit(main())
