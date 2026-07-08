#!/usr/bin/env python3
"""Dedupe repo-managed provider playlists."""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Sequence
from pathlib import Path


SCRIPTS_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.client import SpotifyApiError, SpotifyClient  # noqa: E402
from publishers.spotify.dedupe import (  # noqa: E402
    SpotifyDedupeSummary,
    dedupe_spotify_managed_playlists,
    default_report_path,
)
from publishers.spotify.env import DEFAULT_ENV_PATH, DEFAULT_TOKEN_CACHE_PATH, load_spotify_settings  # noqa: E402
from publishers.spotify.publish_playlist import get_access_token_for_run  # noqa: E402
from shared.debug_log import build_debug_logger  # noqa: E402
from shared.progress import ProgressReporter  # noqa: E402
from shared.publisher_config import DEFAULT_PUBLISHER_CONFIG_PATH, load_or_create_publisher_config  # noqa: E402


SUPPORTED_PROVIDERS = ("spotify",)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, default="spotify", help="Playlist provider to dedupe. Defaults to spotify.")
    parser.add_argument("--apply", action="store_true", help="Remove duplicate tracks from eligible provider playlists. Omit for dry-run.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH, help="Local env file containing Spotify app settings. Defaults to .env.")
    parser.add_argument("--report", type=Path, help="Dedupe report path. Defaults to reports/<timestamp>_dedupe.txt.")
    parser.add_argument("--token-cache", type=Path, default=DEFAULT_TOKEN_CACHE_PATH, help="Spotify token cache path.")
    parser.add_argument("--publisher-config", type=Path, default=DEFAULT_PUBLISHER_CONFIG_PATH, help="Publisher JSON config. Defaults to config/publisher.json.")
    parser.add_argument("--playlists", nargs="+", help="Provider playlist names or IDs to dedupe. Omit to process every eligible playlist.")
    parser.add_argument("--playlist", dest="legacy_playlist", help=argparse.SUPPRESS)
    parser.add_argument("--debug-log", type=Path, help="Write sanitized playlist dedupe debug logs to this path.")
    parser.add_argument("--reauthorize", action="store_true", help="Force a fresh Spotify login before running dedupe.")
    parser.add_argument("--access-token", help=argparse.SUPPRESS)
    parser.add_argument("--no-progress", action="store_false", dest="progress", help="Disable terminal progress output.")
    args = parser.parse_args(argv)
    if args.legacy_playlist and args.playlists:
        parser.error("--playlist cannot be used with --playlists")
    if args.legacy_playlist is not None:
        args.playlists = [args.legacy_playlist]
    args.playlists = normalize_playlist_selectors_from_args(parser, args.playlists)
    delattr(args, "legacy_playlist")
    args.report = args.report or default_report_path()
    return args


def normalize_playlist_selectors_from_args(
    parser: argparse.ArgumentParser,
    playlists: Sequence[str] | None,
) -> list[str] | None:
    if playlists is None:
        return None
    selectors = [selector.strip() for selector in playlists]
    if any(not selector for selector in selectors):
        parser.error("--playlists cannot contain blank selectors")
    if any(selector.casefold() == "all" for selector in selectors):
        parser.error("--playlists all is not allowed; omit --playlists to process every eligible playlist")
    return selectors


def run_spotify_dedupe_from_args(args: argparse.Namespace) -> SpotifyDedupeSummary:
    debug_log = build_debug_logger(args.debug_log)
    if debug_log:
        debug_log(f"start playlist_dedupe provider=spotify apply={args.apply} progress={args.progress}")
    settings = load_spotify_settings(args.env_file, token_cache_path=args.token_cache)
    publisher_config = load_or_create_publisher_config(args.publisher_config)
    access_token = args.access_token or get_access_token_for_run(settings, force_reauthorize=args.reauthorize)
    progress = ProgressReporter(label="Checking Spotify playlists") if args.progress else None
    summary = dedupe_spotify_managed_playlists(
        spotify_client=SpotifyClient(debug_log=debug_log),
        access_token=access_token,
        report_path=args.report,
        publisher_config=publisher_config,
        apply=args.apply,
        progress=progress,
        info_log=print,
        playlist_selectors=args.playlists,
    )
    if debug_log:
        debug_log(
            "completed "
            f"eligible={summary.eligible_playlist_count} skipped={summary.skipped_playlist_count} "
            f"tracks={summary.track_count} duplicates={summary.duplicate_count} removed={summary.removed_count}"
        )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.provider == "spotify":
            summary = run_spotify_dedupe_from_args(args)
        else:
            raise ValueError(f"unsupported provider: {args.provider}")
    except (FileNotFoundError, NotADirectoryError, ValueError, csv.Error, SpotifyApiError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"Playlist dedupe report: {summary.report_path}")
    print(f"Provider playlists fetched: {summary.provider_playlist_count}")
    print(f"Eligible playlists: {summary.eligible_playlist_count}")
    print(f"Skipped playlists: {summary.skipped_playlist_count}")
    print(f"Tracks checked: {summary.track_count}")
    print(f"Duplicates planned: {summary.duplicate_count}")
    print(f"Duplicates removed: {summary.removed_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
