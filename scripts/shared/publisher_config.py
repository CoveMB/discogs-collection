"""Publisher workflow config helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from shared.files import write_json_file


DEFAULT_PUBLISHER_CONFIG_PATH = Path("config/publisher.json")
SUPPORTED_PUBLISHERS = frozenset({"spotify", "none"})
PUBLISHER_CONFIG_KEYS = frozenset({"default_publisher", "playlist_prefix", "playlist_suffix"})
DEFAULT_PUBLISHER = "none"
DEFAULT_PLAYLIST_PREFIX = "Discogs - "
DEFAULT_PLAYLIST_SUFFIX = ""


@dataclass(frozen=True)
class PublisherConfig:
    default_publisher: str
    playlist_prefix: str
    playlist_suffix: str


def default_publisher_config_payload() -> dict[str, object]:
    return {
        "default_publisher": DEFAULT_PUBLISHER,
        "playlist_prefix": DEFAULT_PLAYLIST_PREFIX,
        "playlist_suffix": DEFAULT_PLAYLIST_SUFFIX,
    }


def load_or_create_publisher_config(path: Path = DEFAULT_PUBLISHER_CONFIG_PATH) -> PublisherConfig:
    if not path.exists():
        write_json_file(path, default_publisher_config_payload())
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"malformed publisher config JSON: {path}") from error
    return normalize_publisher_config(payload)


def normalize_publisher_config(payload: object) -> PublisherConfig:
    if not isinstance(payload, Mapping):
        raise ValueError("publisher config must be a JSON object")

    unknown_keys = sorted(str(key) for key in payload if key not in PUBLISHER_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown publisher config key: {', '.join(unknown_keys)}")

    default_publisher = payload.get("default_publisher", DEFAULT_PUBLISHER)
    if not isinstance(default_publisher, str) or default_publisher not in SUPPORTED_PUBLISHERS:
        raise ValueError("default_publisher must be one of: none, spotify")

    playlist_prefix = payload.get("playlist_prefix", DEFAULT_PLAYLIST_PREFIX)
    if not isinstance(playlist_prefix, str):
        raise ValueError("playlist_prefix must be a string")

    playlist_suffix = payload.get("playlist_suffix", DEFAULT_PLAYLIST_SUFFIX)
    if not isinstance(playlist_suffix, str):
        raise ValueError("playlist_suffix must be a string")

    return PublisherConfig(
        default_publisher=default_publisher,
        playlist_prefix=playlist_prefix,
        playlist_suffix=playlist_suffix,
    )
