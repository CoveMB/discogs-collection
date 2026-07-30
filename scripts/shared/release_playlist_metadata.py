"""Typed ownership metadata for generated release-playlist folders."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from shared.files import write_json_file


RELEASE_PLAYLIST_METADATA_FILENAME = ".release-playlist.json"
RELEASE_PLAYLIST_METADATA_SCHEMA_VERSION = 1
AD_HOC_RELEASE_PLAYLIST_RECORD_TYPE = "discogs_release_playlist"
CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE = "discogs_configured_release_playlist"


@dataclass(frozen=True)
class ReleasePlaylistMetadata:
    schema_version: int
    record_type: str
    playlist_name: str


def read_release_playlist_metadata(path: Path, expected_record_type: str) -> ReleasePlaylistMetadata:
    if path.is_symlink():
        raise ValueError(f"{path}: release playlist metadata symlinks are not supported")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: malformed release playlist metadata") from error
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path}: unsupported release playlist metadata")
    if payload.get("schema_version") != RELEASE_PLAYLIST_METADATA_SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported release playlist metadata")
    if payload.get("record_type") != expected_record_type:
        raise ValueError(f"{path}: unsupported release playlist metadata record type")
    playlist_name = payload.get("playlist_name")
    if not isinstance(playlist_name, str) or not playlist_name.strip():
        raise ValueError(f"{path}: release playlist metadata is missing playlist_name")
    return ReleasePlaylistMetadata(
        schema_version=RELEASE_PLAYLIST_METADATA_SCHEMA_VERSION,
        record_type=expected_record_type,
        playlist_name=playlist_name.strip(),
    )


def write_release_playlist_metadata(path: Path, metadata: ReleasePlaylistMetadata) -> None:
    write_json_file(
        path,
        {
            "schema_version": metadata.schema_version,
            "record_type": metadata.record_type,
            "playlist_name": metadata.playlist_name,
        },
    )
