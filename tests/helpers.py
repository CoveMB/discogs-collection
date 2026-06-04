"""Shared test helpers for small CSV and JSON fixtures."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path


def read_csv_text(csv_text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(csv_text)))


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def sample_playlist_config() -> dict[str, object]:
    return {
        "playlist_prefix": "Discogs - ",
        "excluded_terms": ["Electronic", "Electro"],
        "playlists": {
            "Bossanova": ["Bossa Nova", "Bossanova"],
            "Breakbeat": ["Breakbeat", "Breaks"],
            "House": ["House", "Deep House", "Acid House"],
        },
    }
