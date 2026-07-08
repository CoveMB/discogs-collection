#!/usr/bin/env python3
"""Print the Discogs playlist map config in a readable form."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from shared.cli import run_cli
from shared.playlist_config import (
    DEFAULT_CONFIG_PATH,
    ensure_playlist_config_file,
    format_playlist_config_overview,
    load_playlist_config,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Playlist map JSON. Defaults to config/playlist-map.json.",
    )
    return parser.parse_args(argv)


def render_playlist_config(path: Path) -> str:
    created = ensure_playlist_config_file(path)
    config = load_playlist_config(path)
    return format_playlist_config_overview(path, config, created=created)


def print_playlist_config(path: Path) -> None:
    print(render_playlist_config(path), end="")


def run_playlist_config_printer(args: argparse.Namespace) -> str:
    return render_playlist_config(args.config)


def print_summary(output: str) -> None:
    print(output, end="")


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(parse_args, run_playlist_config_printer, print_summary, argv)


if __name__ == "__main__":
    raise SystemExit(main())
