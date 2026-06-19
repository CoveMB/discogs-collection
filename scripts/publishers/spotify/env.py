"""Spotify environment file loading."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ENV_PATH = Path(".env")
DEFAULT_TOKEN_CACHE_PATH = Path("config/cache/spotify-token.cache.json")
SPOTIFY_CLIENT_ID_KEY = "SPOTIFY_CLIENT_ID"
SPOTIFY_REDIRECT_URI_KEY = "SPOTIFY_REDIRECT_URI"


@dataclass(frozen=True)
class SpotifySettings:
    client_id: str
    redirect_uri: str
    token_cache_path: Path = DEFAULT_TOKEN_CACHE_PATH


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"malformed env line {line_number}: expected KEY=value")
        key, raw_value = line.split("=", 1)
        clean_key = key.strip()
        if not clean_key:
            raise ValueError(f"malformed env line {line_number}: missing key")
        values[clean_key] = parse_env_value(raw_value.strip())
    return values


def parse_env_value(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = shlex.split(value, comments=False, posix=True)
    except ValueError:
        return value.strip('"').strip("'")
    if not parsed:
        return ""
    return parsed[0]


def load_spotify_settings(env_path: Path = DEFAULT_ENV_PATH, token_cache_path: Path = DEFAULT_TOKEN_CACHE_PATH) -> SpotifySettings:
    file_values = load_env_file(env_path)
    client_id = clean_value(file_values.get(SPOTIFY_CLIENT_ID_KEY) or os.environ.get(SPOTIFY_CLIENT_ID_KEY))
    redirect_uri = clean_value(file_values.get(SPOTIFY_REDIRECT_URI_KEY) or os.environ.get(SPOTIFY_REDIRECT_URI_KEY))

    missing_keys = []
    if not client_id:
        missing_keys.append(SPOTIFY_CLIENT_ID_KEY)
    if not redirect_uri:
        missing_keys.append(SPOTIFY_REDIRECT_URI_KEY)
    if missing_keys:
        raise ValueError(f"missing required Spotify env value: {', '.join(missing_keys)}")

    return SpotifySettings(
        client_id=client_id,
        redirect_uri=redirect_uri,
        token_cache_path=token_cache_path,
    )


def clean_value(value: object) -> str:
    return str(value or "").strip()
