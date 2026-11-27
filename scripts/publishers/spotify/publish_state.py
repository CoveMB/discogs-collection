"""Local Spotify publish state cache."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from publishers.spotify.match_cache import compare_cached_manual_source
from publishers.spotify.matching import PlaylistTrack
from shared.files import write_json_file
from shared.text import clean_cell


PUBLISH_STATE_SCHEMA_VERSION = 1
PUBLISH_STATE_RECORD_TYPE = "spotify_publish_state_cache"
PUBLISH_IDENTITY_VERSION = 2


SpotifyPublishState = dict[str, dict[str, dict[str, object]]]


def load_spotify_publish_state(path: Path) -> SpotifyPublishState:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Spotify publish state cache must be a JSON object")
    if (
        payload.get("schema_version") != PUBLISH_STATE_SCHEMA_VERSION
        or payload.get("record_type") != PUBLISH_STATE_RECORD_TYPE
    ):
        raise ValueError("unsupported Spotify publish state cache format; delete the old cache or choose a new --publish-state-cache path")
    playlists = payload.get("playlists", {})
    if not isinstance(playlists, Mapping):
        raise ValueError("Spotify publish state cache field must be an object: playlists")
    state: SpotifyPublishState = {}
    for playlist_name, playlist_payload in playlists.items():
        clean_playlist_name = clean_cell(playlist_name)
        if not clean_playlist_name or not isinstance(playlist_payload, Mapping):
            continue
        tracks = playlist_payload.get("tracks", {})
        if not isinstance(tracks, Mapping):
            continue
        state[clean_playlist_name] = {
            clean_identity_key: dict(record)
            for identity_key, record in tracks.items()
            if (clean_identity_key := clean_cell(identity_key)) and isinstance(record, Mapping)
        }
    return state


def save_spotify_publish_state(path: Path, state: Mapping[str, Mapping[str, Mapping[str, object]]]) -> None:
    write_json_file(
        path,
        {
            "schema_version": PUBLISH_STATE_SCHEMA_VERSION,
            "record_type": PUBLISH_STATE_RECORD_TYPE,
            "playlists": {
                playlist_name: {
                    "tracks": dict(sorted((identity_key, dict(record)) for identity_key, record in tracks.items())),
                }
                for playlist_name, tracks in sorted((clean_cell(name), tracks) for name, tracks in state.items())
                if playlist_name
            },
        },
    )


def spotify_publish_state_has_track(
    state: Mapping[str, Mapping[str, Mapping[str, object]]],
    playlist_name: str,
    identity_key: str,
    *,
    track: PlaylistTrack | None = None,
    spotify_uri: str = "",
    legacy_identity_key: str = "",
) -> bool:
    clean_playlist_name = clean_cell(playlist_name)
    clean_identity_key = clean_cell(identity_key)
    history = state.get(clean_playlist_name, {})
    current = history.get(clean_identity_key)
    if current is not None and (track is None or current.get("identity_version") == PUBLISH_IDENTITY_VERSION):
        return True
    legacy = history.get(legacy_identity_key)
    return track is not None and any(
        record is not None and (
            (spotify_uri and record.get("spotify_uri") == spotify_uri)
            or compare_cached_manual_source(track, record) is True
        )
        for record in (current, legacy)
    )


def record_spotify_publish_state_track(
    state: SpotifyPublishState,
    playlist_name: str,
    identity_key: str,
    spotify_uri: str,
    source_position: int | None,
    track: PlaylistTrack | None,
    timestamp: str,
    event: str,
) -> None:
    clean_playlist_name = clean_cell(playlist_name)
    clean_identity_key = clean_cell(identity_key)
    if not clean_playlist_name or not clean_identity_key:
        return
    playlist_state = state.setdefault(clean_playlist_name, {})
    record = dict(playlist_state.get(clean_identity_key, {}))
    record["identity_key"] = clean_identity_key
    record["identity_version"] = PUBLISH_IDENTITY_VERSION
    record["spotify_uri"] = clean_cell(spotify_uri)
    if source_position is not None and source_position > 0:
        if "first_source_position" not in record:
            record["first_source_position"] = source_position
        record["last_source_position"] = source_position
    if track:
        record.update(
            {
                "release_id": track.release_id,
                "track_number": track.track_number,
                "artist_name": track.artist_name,
                "album_name": track.album_name,
                "track_name": track.track_name,
            }
        )
    if event == "observed":
        if "first_observed_at" not in record:
            record["first_observed_at"] = timestamp
        record["last_observed_at"] = timestamp
    elif event == "published":
        if "first_published_at" not in record:
            record["first_published_at"] = timestamp
        record["last_published_at"] = timestamp
    record["last_seen_at"] = timestamp
    playlist_state[clean_identity_key] = record
