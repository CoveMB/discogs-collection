#!/usr/bin/env python3
"""Print the Discogs playlist map config in a readable form."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

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


def print_playlist_config(path: Path) -> None:
    created = ensure_playlist_config_file(path)
    config = load_playlist_config(path)
    print(format_playlist_config_overview(path, config, created=created), end="")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        print_playlist_config(args.config)
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
