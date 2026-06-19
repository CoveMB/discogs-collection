"""Local Spotify track match cache."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from publishers.spotify.matching import (
    MATCHED,
    PlaylistTrack,
    SpotifyTrackCandidate,
    TrackMatchDecision,
    build_spotify_track_search_query,
    normalize_music_text,
)
from shared.files import write_json_file


MATCH_CACHE_SCHEMA_VERSION = 1
MATCH_CACHE_RECORD_TYPE = "spotify_track_match_cache"
MATCHER_VERSION = 1


@dataclass(frozen=True)
class CachedSpotifyTrackMatch:
    cache_key: str
    decision: TrackMatchDecision


def load_spotify_track_match_cache(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Spotify track match cache must be a JSON object")
    if (
        payload.get("schema_version") != MATCH_CACHE_SCHEMA_VERSION
        or payload.get("record_type") != MATCH_CACHE_RECORD_TYPE
    ):
        raise ValueError("unsupported Spotify track match cache format; delete the old cache or choose a new --match-cache path")
    matches = payload.get("matches", {})
    if not isinstance(matches, Mapping):
        raise ValueError("Spotify track match cache field must be an object: matches")
    return {
        clean_cell(cache_key): dict(record)
        for cache_key, record in matches.items()
        if clean_cell(cache_key) and isinstance(record, Mapping)
    }


def save_spotify_track_match_cache(path: Path, matches: Mapping[str, Mapping[str, object]]) -> None:
    write_json_file(
        path,
        {
            "schema_version": MATCH_CACHE_SCHEMA_VERSION,
            "record_type": MATCH_CACHE_RECORD_TYPE,
            "matches": dict(sorted((cache_key, dict(record)) for cache_key, record in matches.items())),
        },
    )


def cached_track_match(
    track: PlaylistTrack,
    matches: Mapping[str, Mapping[str, object]],
    seen_at: str | None = None,
) -> CachedSpotifyTrackMatch | None:
    cache_key = spotify_track_match_key(track)
    record = matches.get(cache_key)
    if not record:
        return None
    if clean_cell(record.get("match_status")) not in {"matched", "manual"}:
        return None
    spotify_uri = clean_cell(record.get("spotify_uri"))
    if not spotify_uri.startswith("spotify:track:"):
        return None

    artist_names = tuple(
        clean_cell(artist_name)
        for artist_name in record.get("spotify_artist_names", ())
        if clean_cell(artist_name)
    )
    candidate = SpotifyTrackCandidate(
        uri=spotify_uri,
        name=clean_cell(record.get("spotify_track_name")) or track.track_name,
        artists=artist_names or (track.artist_name,),
        album_name=clean_cell(record.get("spotify_album_name")) or track.album_name,
    )
    if seen_at is not None and isinstance(record, dict):
        record["last_seen_at"] = seen_at
    return CachedSpotifyTrackMatch(
        cache_key=cache_key,
        decision=TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=spotify_uri,
            reason=clean_cell(record.get("match_reason")) or "cached Spotify match",
            candidate=candidate,
            review_candidates=(candidate,),
        ),
    )


def cache_track_match(
    matches: dict[str, dict[str, object]],
    decision: TrackMatchDecision,
    matched_at: str | None = None,
) -> None:
    if decision.status != MATCHED or not decision.spotify_uri:
        return
    timestamp = matched_at or utc_timestamp()
    track = decision.track
    candidate = decision.candidate
    matches[spotify_track_match_key(track)] = {
        "release_id": track.release_id,
        "track_number": track.track_number,
        "artist_name": track.artist_name,
        "album_name": track.album_name,
        "track_name": track.track_name,
        "search_query": build_spotify_track_search_query(track),
        "spotify_uri": decision.spotify_uri,
        "spotify_url": spotify_url_from_uri(decision.spotify_uri),
        "spotify_track_name": candidate.name if candidate else track.track_name,
        "spotify_artist_names": list(candidate.artists if candidate else (track.artist_name,)),
        "spotify_album_name": candidate.album_name if candidate else track.album_name,
        "match_status": "matched",
        "match_reason": decision.reason,
        "matcher_version": MATCHER_VERSION,
        "matched_at": timestamp,
        "last_seen_at": timestamp,
    }


def spotify_track_match_key(track: PlaylistTrack) -> str:
    return "|".join(
        (
            clean_cell(track.release_id),
            clean_cell(track.track_number),
            normalize_music_text(track.artist_name),
            normalize_music_text(track.album_name),
            normalize_music_text(track.track_name),
        )
    )


def spotify_url_from_uri(uri: str) -> str:
    prefix = "spotify:track:"
    if not uri.startswith(prefix):
        return ""
    return f"https://open.spotify.com/track/{uri.removeprefix(prefix)}"


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_cell(value: object) -> str:
    return str(value or "").strip()
