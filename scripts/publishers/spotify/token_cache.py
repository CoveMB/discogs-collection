"""Local Spotify token cache."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from shared.files import write_json_file


TOKEN_CACHE_SCHEMA_VERSION = 1
TOKEN_CACHE_RECORD_TYPE = "spotify_oauth_token"


@dataclass(frozen=True)
class SpotifyToken:
    access_token: str
    refresh_token: str
    expires_at: int
    scope: str
    token_type: str

    def is_expired(self, now: int, leeway_seconds: int = 60) -> bool:
        return self.expires_at <= now + leeway_seconds


def load_spotify_token(path: Path) -> SpotifyToken | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Spotify token cache must be a JSON object")
    if (
        payload.get("schema_version") != TOKEN_CACHE_SCHEMA_VERSION
        or payload.get("record_type") != TOKEN_CACHE_RECORD_TYPE
    ):
        raise ValueError("unsupported Spotify token cache format; delete the old cache or choose a new --token-cache path")
    return SpotifyToken(
        access_token=clean_cell(payload.get("access_token")),
        refresh_token=clean_cell(payload.get("refresh_token")),
        expires_at=parse_int(payload.get("expires_at"), "expires_at"),
        scope=clean_cell(payload.get("scope")),
        token_type=clean_cell(payload.get("token_type")) or "Bearer",
    )


def save_spotify_token(path: Path, token: SpotifyToken) -> None:
    write_json_file(
        path,
        {
            "schema_version": TOKEN_CACHE_SCHEMA_VERSION,
            "record_type": TOKEN_CACHE_RECORD_TYPE,
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at,
            "scope": token.scope,
            "token_type": token.token_type,
        },
    )


def discard_spotify_token(path: Path) -> None:
    path.unlink(missing_ok=True)


def parse_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Spotify token cache field must be an integer: {field_name}")
    return value


def clean_cell(value: object) -> str:
    return str(value or "").strip()
