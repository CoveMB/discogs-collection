"""Publisher workflow config helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from shared.config_files import load_or_create_json_file, reject_unknown_keys


DEFAULT_PUBLISHER_CONFIG_PATH = Path("config/publisher.json")
SPOTIFY_PUBLISHER = "spotify"
NO_PUBLISHER = "none"
PUBLISHER_CHOICES = (SPOTIFY_PUBLISHER, NO_PUBLISHER)
SUPPORTED_PUBLISHERS = frozenset(PUBLISHER_CHOICES)
NON_PUBLISHING_PUBLISHERS = frozenset({NO_PUBLISHER})
PUBLISHER_CONFIG_KEYS = frozenset(
    {
        "default_publisher",
        "playlist_prefix",
        "playlist_suffix",
        "release_playlists_prefix",
        "release_playlists_suffix",
    }
)
DEFAULT_PUBLISHER = NO_PUBLISHER
DEFAULT_PLAYLIST_PREFIX = ""
DEFAULT_PLAYLIST_SUFFIX = ""
DEFAULT_RELEASE_PLAYLISTS_PREFIX = ""
DEFAULT_RELEASE_PLAYLISTS_SUFFIX = ""


@dataclass(frozen=True)
class PublisherConfig:
    default_publisher: str
    playlist_prefix: str
    playlist_suffix: str
    release_playlists_prefix: str = DEFAULT_RELEASE_PLAYLISTS_PREFIX
    release_playlists_suffix: str = DEFAULT_RELEASE_PLAYLISTS_SUFFIX


def default_publisher_config_payload() -> dict[str, object]:
    return {
        "default_publisher": DEFAULT_PUBLISHER,
        "playlist_prefix": DEFAULT_PLAYLIST_PREFIX,
        "playlist_suffix": DEFAULT_PLAYLIST_SUFFIX,
        "release_playlists_prefix": DEFAULT_RELEASE_PLAYLISTS_PREFIX,
        "release_playlists_suffix": DEFAULT_RELEASE_PLAYLISTS_SUFFIX,
    }


def load_or_create_publisher_config(path: Path = DEFAULT_PUBLISHER_CONFIG_PATH) -> PublisherConfig:
    payload = load_or_create_json_file(
        path,
        default_payload=default_publisher_config_payload(),
        malformed_label="publisher config",
    )
    return normalize_publisher_config(payload)


def normalize_publisher_config(payload: object) -> PublisherConfig:
    if not isinstance(payload, Mapping):
        raise ValueError("publisher config must be a JSON object")

    reject_unknown_keys(payload, allowed_keys=PUBLISHER_CONFIG_KEYS, config_label="publisher config")

    default_publisher = payload.get("default_publisher", DEFAULT_PUBLISHER)
    if not isinstance(default_publisher, str) or default_publisher not in SUPPORTED_PUBLISHERS:
        raise ValueError("default_publisher must be one of: none, spotify")

    playlist_prefix = payload.get("playlist_prefix", DEFAULT_PLAYLIST_PREFIX)
    if not isinstance(playlist_prefix, str):
        raise ValueError("playlist_prefix must be a string")

    playlist_suffix = payload.get("playlist_suffix", DEFAULT_PLAYLIST_SUFFIX)
    if not isinstance(playlist_suffix, str):
        raise ValueError("playlist_suffix must be a string")

    release_playlists_prefix = payload.get(
        "release_playlists_prefix", DEFAULT_RELEASE_PLAYLISTS_PREFIX
    )
    if not isinstance(release_playlists_prefix, str):
        raise ValueError("release_playlists_prefix must be a string")

    release_playlists_suffix = payload.get(
        "release_playlists_suffix", DEFAULT_RELEASE_PLAYLISTS_SUFFIX
    )
    if not isinstance(release_playlists_suffix, str):
        raise ValueError("release_playlists_suffix must be a string")

    return PublisherConfig(
        default_publisher=default_publisher,
        playlist_prefix=playlist_prefix,
        playlist_suffix=playlist_suffix,
        release_playlists_prefix=release_playlists_prefix,
        release_playlists_suffix=release_playlists_suffix,
    )


def publishing_publishers(supported_publishers: Sequence[str] | None = None) -> tuple[str, ...]:
    publishers = PUBLISHER_CHOICES if supported_publishers is None else supported_publishers
    return tuple(publisher for publisher in publishers if publisher not in NON_PUBLISHING_PUBLISHERS)


def publisher_playlist_name(playlist_name: str, config: PublisherConfig) -> str:
    return f"{config.playlist_prefix}{playlist_name}{config.playlist_suffix}"


def configured_release_publisher_config(config: PublisherConfig) -> PublisherConfig:
    return replace(
        config,
        playlist_prefix=config.release_playlists_prefix,
        playlist_suffix=config.release_playlists_suffix,
    )


def publisher_local_name_from_target(playlist_name: object, config: PublisherConfig) -> str | None:
    name = clean_text(playlist_name)
    prefix = config.playlist_prefix
    suffix = config.playlist_suffix
    if prefix and not name.startswith(prefix):
        return None
    if suffix and not name.endswith(suffix):
        return None
    without_prefix = name[len(prefix) :] if prefix else name
    without_suffix = without_prefix[: -len(suffix)] if suffix else without_prefix
    local_name = clean_text(without_suffix)
    return local_name or None


def validate_publisher_naming_is_safe(config: PublisherConfig) -> None:
    if not config.playlist_prefix and not config.playlist_suffix:
        raise ValueError(
            "playlist_prefix or playlist_suffix must be configured before deduping provider playlists"
        )


def clean_text(value: object) -> str:
    return str(value or "").strip()
