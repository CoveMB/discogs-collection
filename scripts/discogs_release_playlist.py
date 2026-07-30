#!/usr/bin/env python3
"""Create isolated ad-hoc or configured publisher playlists from Discogs release IDs."""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import configured_release_playlists
import discogs_playlist_exporter as exporter
import discogs_tracklists as tracklists
from publishers.spotify import publish_playlist as spotify_publisher
from publishers.spotify.env import DEFAULT_ENV_PATH, DEFAULT_TOKEN_CACHE_PATH
from shared.cli import EXPECTED_CLI_ERRORS, run_cli
from shared.debug_log import DebugLog, build_debug_logger
from shared.discogs_columns import RELEASE_ID_COLUMN
from shared.playlist_selection import (
    ensure_path_inside_output_directory,
    playlist_master_path,
    safe_playlist_filename,
)
from shared import playlist_config, workflow_config
from shared.playlist_config import DEFAULT_CONFIG_PATH, PlaylistConfig
from shared.progress import ProgressReporter
from shared.release_playlist_metadata import (
    AD_HOC_RELEASE_PLAYLIST_RECORD_TYPE,
    RELEASE_PLAYLIST_METADATA_FILENAME,
    RELEASE_PLAYLIST_METADATA_SCHEMA_VERSION,
    ReleasePlaylistMetadata,
    read_release_playlist_metadata as load_release_playlist_metadata,
    write_release_playlist_metadata as save_release_playlist_metadata,
)
from shared.publisher_config import (
    DEFAULT_PUBLISHER_CONFIG_PATH,
    NO_PUBLISHER,
    PUBLISHER_CHOICES,
    SPOTIFY_PUBLISHER,
    PublisherConfig,
    configured_release_publisher_config,
    load_or_create_publisher_config,
    publisher_playlist_name,
)
from shared.cli import print_cli_summary
from shared.reports import format_report_section, format_report_title, script_report_path, write_text_report
from shared.workflow_paths import (
    DEFAULT_CONFIGURED_RELEASE_PLAYLIST_DIRECTORY,
    DEFAULT_ON_THE_FLY_PLAYLIST_DIRECTORY,
    DEFAULT_PLAYLIST_OUTPUT_DIRECTORY,
    DEFAULT_SPOTIFY_MATCH_CACHE_PATH,
    DEFAULT_TRACKLIST_CACHE_PATH,
)
from shared.workflow_config import DEFAULT_WORKFLOW_CONFIG_PATH


PLAYLISTS_COLUMN = exporter.PLAYLISTS_COLUMN
DEFAULT_OUTPUT_DIRECTORY = DEFAULT_ON_THE_FLY_PLAYLIST_DIRECTORY
DEFAULT_MATCH_CACHE_PATH = DEFAULT_SPOTIFY_MATCH_CACHE_PATH
DEFAULT_USER_AGENT = "DiscogsReleasePlaylist/1.0 +https://www.discogs.com"
SUPPORTED_PUBLISHERS = PUBLISHER_CHOICES
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


@dataclass(frozen=True)
class ConfiguredReleasePlaylistRunSummary:
    local_summary: configured_release_playlists.ConfiguredReleasePlaylistsSummary
    publisher: str
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
    return load_release_playlist_metadata(
        path,
        AD_HOC_RELEASE_PLAYLIST_RECORD_TYPE,
    ).playlist_name


def write_release_playlist_metadata(path: Path, playlist_name: str) -> None:
    save_release_playlist_metadata(
        path,
        ReleasePlaylistMetadata(
            schema_version=RELEASE_PLAYLIST_METADATA_SCHEMA_VERSION,
            record_type=AD_HOC_RELEASE_PLAYLIST_RECORD_TYPE,
            playlist_name=playlist_name,
        ),
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


def build_spotify_publisher_args(
    args: argparse.Namespace,
    *,
    publisher_sync_mode: str | None = None,
) -> argparse.Namespace:
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
        publisher_sync_mode=publisher_sync_mode or args.publisher_sync_mode,
        refresh_match_cache=args.refresh_match_cache,
        dry_run=args.publishing_dry_run,
        progress=args.progress,
    )


def validate_configured_output_directory(output_directory: Path) -> None:
    output_parts = casefolded_resolved_path_parts(output_directory)
    normal_output_parts = casefolded_resolved_path_parts(
        DEFAULT_PLAYLIST_OUTPUT_DIRECTORY
    )
    if output_parts == normal_output_parts:
        raise ValueError(
            f"configured release playlist output {output_directory} cannot equal "
            f"the normal playlist output root {DEFAULT_PLAYLIST_OUTPUT_DIRECTORY}"
        )

    on_the_fly_parts = casefolded_resolved_path_parts(
        DEFAULT_ON_THE_FLY_PLAYLIST_DIRECTORY
    )
    if output_parts[: len(on_the_fly_parts)] == on_the_fly_parts:
        raise ValueError(
            f"configured release playlist output {output_directory} cannot be inside "
            f"the on-the-fly directory {DEFAULT_ON_THE_FLY_PLAYLIST_DIRECTORY}"
        )


def casefolded_resolved_path_parts(path: Path) -> tuple[str, ...]:
    return tuple(part.casefold() for part in path.resolve().parts)


def validate_configured_publisher_targets(
    config: PlaylistConfig,
    publisher_config: PublisherConfig,
) -> None:
    normal_targets: dict[str, tuple[str, str]] = {}
    for label in config.playlist_labels:
        target_name = publisher_playlist_name(label, publisher_config)
        normal_targets[target_name.casefold()] = (label, target_name)
    release_publisher_config = configured_release_publisher_config(publisher_config)
    release_targets: dict[str, tuple[str, str]] = {}
    for definition in config.release_playlists:
        target_name = publisher_playlist_name(definition.name, release_publisher_config)
        target_key = target_name.casefold()
        normal_target = normal_targets.get(target_key)
        if normal_target is not None:
            normal_label, normal_target_name = normal_target
            raise ValueError(
                f"configured release playlist {definition.name!r} Spotify target {target_name!r} "
                f"collides with normal playlist {normal_label!r} target {normal_target_name!r}"
            )
        release_target = release_targets.get(target_key)
        if release_target is not None:
            release_name, release_target_name = release_target
            raise ValueError(
                f"configured release playlists {release_name!r} and {definition.name!r} have "
                f"the same Spotify target {release_target_name!r}"
            )
        release_targets[target_key] = (definition.name, target_name)


def reject_unexpected_configured_lookup(
    _row: Mapping[str, str],
) -> tracklists.ReleaseTracklistLookup:
    raise AssertionError("configured tracklist lookup was called for an empty release set")


def run_configured_release_playlist(
    args: argparse.Namespace,
) -> ConfiguredReleasePlaylistRunSummary:
    debug_log = build_debug_logger(args.debug_log)
    if debug_log:
        debug_log("start discogs_release_playlist configured_mode=true")

    try:
        validate_configured_output_directory(args.output_dir)
        config = playlist_config.load_playlist_config(args.config)
        resolved_workflow_config = workflow_config.load_or_create_workflow_config(
            args.workflow_config
        )
        if args.max_rows is not None:
            resolved_workflow_config = replace(
                resolved_workflow_config,
                max_rows_per_split=args.max_rows,
            )
        publisher_config = load_or_create_publisher_config(args.publisher_config)
        validate_configured_publisher_targets(config, publisher_config)
    except Exception as error:
        try:
            configured_release_playlists.write_configured_release_playlist_failure_report(
                args.report,
                config_path=args.config,
                output_directory=args.output_dir,
                error=error,
            )
        except Exception:
            pass
        raise

    publisher = args.publisher or publisher_config.default_publisher
    has_release_ids = any(
        definition.release_ids for definition in config.release_playlists
    )
    lookup_tracklist = (
        tracklists.make_cached_tracklist_lookup(
            cache_path=args.tracklist_cache,
            token=args.discogs_token,
            user_agent=args.user_agent,
            timeout_seconds=args.timeout_seconds,
            request_interval_seconds=args.request_interval_seconds,
        )
        if has_release_ids
        else reject_unexpected_configured_lookup
    )
    local_summary = configured_release_playlists.create_configured_release_playlists(
        config=config,
        config_path=args.config,
        workflow_config=resolved_workflow_config,
        output_directory=args.output_dir,
        report_path=args.report,
        split_report_path=args.split_report,
        lookup_tracklist=lookup_tracklist,
    )
    summary = ConfiguredReleasePlaylistRunSummary(
        local_summary=local_summary,
        publisher=publisher,
    )
    if publisher == NO_PUBLISHER:
        return summary
    if publisher != SPOTIFY_PUBLISHER:
        raise ValueError(f"unsupported publisher: {publisher}")
    if not local_summary.master_paths:
        return summary

    publisher_args = build_spotify_publisher_args(
        args,
        publisher_sync_mode=spotify_publisher.REPLACE_SYNC_MODE,
    )
    publisher_summary = spotify_publisher.run_spotify_publish_from_args(
        publisher_args,
        playlist_master_paths=local_summary.master_paths,
        playlist_names_by_master_path=local_summary.playlist_names_by_master_path,
        publisher_config=configured_release_publisher_config(publisher_config),
        debug_log=debug_log,
    )
    return replace(summary, publisher_summary=publisher_summary)


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


def print_configured_summary(summary: ConfiguredReleasePlaylistRunSummary) -> None:
    local_summary = summary.local_summary
    print_cli_summary(
        files=[
            f"Config: {local_summary.config_path}",
            f"Output directory: {local_summary.output_directory}",
            f"Report: {local_summary.report_path}",
            f"Split report: {local_summary.split_report_path}",
            *(
                [f"Publisher report: {summary.publisher_summary.report_path}"]
                if summary.publisher_summary is not None
                else []
            ),
        ],
        processed=[
            f"Current playlists: {len(local_summary.playlists)}",
            f"Deleted folders: {len(local_summary.deleted_folder_paths)}",
            f"Output track rows: {sum(playlist.track_row_count for playlist in local_summary.playlists)}",
            f"Publisher: {summary.publisher}",
        ],
    )


def run_release_playlist_mode(
    args: argparse.Namespace,
) -> ReleasePlaylistSummary | ConfiguredReleasePlaylistRunSummary:
    if args.from_config:
        return run_configured_release_playlist(args)
    return run_release_playlist(args)


def print_release_playlist_mode_summary(
    summary: ReleasePlaylistSummary | ConfiguredReleasePlaylistRunSummary,
) -> None:
    if isinstance(summary, ConfiguredReleasePlaylistRunSummary):
        print_configured_summary(summary)
        return
    print_summary(summary)


def default_report_path() -> Path:
    return script_report_path(__file__)


def default_configured_split_report_path() -> Path:
    return script_report_path("configured_release_playlist_splitter.py")


def default_configured_publisher_report_path() -> Path:
    return script_report_path("configured_release_playlist_publisher.py")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_ids", nargs="*", help="Discogs release IDs to export into the playlist.")
    parser.add_argument("--from-config", action="store_true", help="Rebuild all configured release playlists instead of creating one ad-hoc playlist.")
    parser.add_argument("--name", help="Ad-hoc playlist name to create or update under the on-the-fly playlist folder.")
    parser.add_argument("--release-ids-file", type=Path, help="Text file containing release IDs separated by whitespace or commas.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Playlist map JSON config. Defaults to config/playlist-map.json.")
    parser.add_argument("--workflow-config", type=Path, default=DEFAULT_WORKFLOW_CONFIG_PATH, help="Workflow JSON config. Defaults to config/workflow.json.")
    parser.add_argument("--output-dir", type=Path, help="Playlist output root. Defaults depend on the selected mode.")
    parser.add_argument("--report", type=Path, help="Release playlist report path. Defaults to reports/<timestamp>_discogs_release_playlist.txt.")
    parser.add_argument("--split-report", type=Path, help="Configured release playlist split report path. Defaults to reports/<timestamp>_configured_release_playlist_splitter.txt.")
    parser.add_argument("--max-rows", type=int, help="Maximum rows per configured split CSV. Overrides workflow config.")
    parser.add_argument("--tracklist-cache", type=Path, default=DEFAULT_TRACKLIST_CACHE_PATH, help="Discogs tracklist cache JSON. Defaults to collection/cache/playlist-tracks.cache.json.")
    parser.add_argument("--discogs-token", default=os.environ.get("DISCOGS_TOKEN", ""), help="Optional Discogs personal access token. Defaults to DISCOGS_TOKEN.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent sent to Discogs.")
    parser.add_argument("--timeout-seconds", type=int, default=30, help="HTTP timeout per Discogs request.")
    parser.add_argument("--request-interval-seconds", type=float, default=exporter.DEFAULT_REQUEST_INTERVAL_SECONDS, help="Minimum delay between Discogs requests. Defaults to header-aware throttling.")
    parser.add_argument("--publisher-config", type=Path, default=DEFAULT_PUBLISHER_CONFIG_PATH, help="Publisher JSON config. Defaults to config/publisher.json.")
    parser.add_argument("--publisher", choices=SUPPORTED_PUBLISHERS, help="Publisher override. Omit to use default_publisher from the publisher config.")
    parser.add_argument("--publisher-report", type=Path, help="Spotify publisher report path. Ad-hoc mode defaults to reports/<timestamp>_publish_playlist.txt; configured mode defaults to reports/<timestamp>_configured_release_playlist_publisher.txt.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH, help="Local env file containing Spotify app settings. Defaults to .env.")
    parser.add_argument("--token-cache", type=Path, default=DEFAULT_TOKEN_CACHE_PATH, help="Spotify token cache path.")
    parser.add_argument("--match-cache", type=Path, default=DEFAULT_MATCH_CACHE_PATH, help="Spotify track match cache path.")
    parser.add_argument("--reauthorize", action="store_true", help="Force a fresh Spotify login before running the publisher.")
    parser.add_argument("--access-token", help=argparse.SUPPRESS)
    parser.add_argument("--search-limit", type=int, default=10, help="Spotify search result limit per album or track query. Defaults to 10.")
    parser.add_argument(
        "--max-new-searches-per-run",
        type=int,
        default=spotify_publisher.DEFAULT_MAX_NEW_SEARCHES_PER_RUN,
        help=(
            "Maximum uncached Spotify searches per publisher run. "
            "Defaults to 500. Use 0 for unlimited."
        ),
    )
    parser.add_argument("--publisher-sync-mode", choices=spotify_publisher.PUBLISHER_SYNC_MODES, help="Publisher sync mode. Ad-hoc mode defaults to append; configured mode requires replace.")
    parser.add_argument("--refresh-match-cache", action="store_true", help="Recheck every generated playlist row with Spotify and update the local track match cache.")
    parser.add_argument("--publishing-dry-run", action="store_true", help="Preview Spotify playlist changes without creating or updating playlists.")
    parser.add_argument("--debug-log", type=Path, help="Write sanitized release playlist debug logs to this path.")
    parser.add_argument("--no-progress", action="store_false", dest="progress", help="Disable terminal progress output.")
    args = parser.parse_args(argv)
    validate_args(parser, args)
    args.report = args.report or default_report_path()
    if args.from_config:
        args.split_report = args.split_report or default_configured_split_report_path()
        args.publisher_report = args.publisher_report or default_configured_publisher_report_path()
    else:
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
    if args.max_rows is not None and args.max_rows < 1:
        parser.error("--max-rows must be at least 1")
    if args.from_config:
        if args.name or args.release_ids or args.release_ids_file is not None:
            parser.error("--from-config cannot be combined with ad-hoc playlist inputs")
        if args.publisher_sync_mode not in {None, spotify_publisher.REPLACE_SYNC_MODE}:
            parser.error("configured release playlists require --publisher-sync-mode replace")
        args.output_dir = args.output_dir or DEFAULT_CONFIGURED_RELEASE_PLAYLIST_DIRECTORY
        args.publisher_sync_mode = spotify_publisher.REPLACE_SYNC_MODE
        return
    if args.max_rows is not None:
        parser.error("--max-rows requires --from-config")
    if not args.name:
        parser.error("--name is required unless --from-config is used")
    if not args.release_ids and args.release_ids_file is None:
        parser.error("at least one release_id or --release-ids-file is required")
    args.output_dir = args.output_dir or DEFAULT_ON_THE_FLY_PLAYLIST_DIRECTORY
    args.publisher_sync_mode = args.publisher_sync_mode or spotify_publisher.APPEND_SYNC_MODE


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(
        parse_args,
        run_release_playlist_mode,
        print_release_playlist_mode_summary,
        argv,
        expected_errors=(*EXPECTED_CLI_ERRORS, spotify_publisher.SpotifyApiError),
    )


if __name__ == "__main__":
    raise SystemExit(main())
