"""Discogs release tracklist lookup, parsing, and cache helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from shared.discogs_api import (
    DEFAULT_REQUEST_INTERVAL_SECONDS,
    DISCOGS_API_ROOT,
    DiscogsRateLimiter,
    default_discogs_rate_limit,
    http_get,
)
from shared.discogs_columns import RELEASE_ID_COLUMN
from shared.files import write_json_file
from shared.text import clean_cell


TRACKLIST_CACHE_SCHEMA_VERSION = 1
TRACKLIST_CACHE_RECORD_TYPE = "discogs_release_tracklist"


@dataclass(frozen=True)
class DiscogsTrack:
    position: str
    title: str
    artist_name: str


@dataclass(frozen=True)
class ReleaseTracklistLookup:
    release_id: str
    artist_name: str
    album_name: str
    record_year: str
    tracks: tuple[DiscogsTrack, ...]
    notes: tuple[str, ...]


def release_api_url(release_id: str) -> str:
    return f"{DISCOGS_API_ROOT}/releases/{release_id}"


def release_tracklist_from_payload(
    release_id: str,
    payload: Mapping[str, object],
    row: Mapping[str, str],
) -> ReleaseTracklistLookup:
    artist_name = parse_artists(payload.get("artists")) or clean_cell(row.get("Artist", ""))
    album_name = clean_cell(payload.get("title")) or clean_cell(row.get("Title", ""))
    record_year = record_year_from_payload(payload) or record_year_from_row(row)
    tracks = tuple(flatten_tracklist(payload.get("tracklist"), artist_name))
    notes = () if tracks else ("no Discogs tracklist found",)
    return ReleaseTracklistLookup(
        release_id=release_id,
        artist_name=artist_name,
        album_name=album_name,
        record_year=record_year,
        tracks=tracks,
        notes=notes,
    )


def flatten_tracklist(tracklist: object, default_artist_name: str) -> list[DiscogsTrack]:
    if not isinstance(tracklist, list):
        return []
    tracks: list[DiscogsTrack] = []
    for item in tracklist:
        if not isinstance(item, Mapping):
            continue
        sub_tracks = flatten_tracklist(item.get("sub_tracks"), default_artist_name)
        if sub_tracks:
            tracks.extend(sub_tracks)
            continue

        track_type = clean_cell(item.get("type_", "track")).casefold() or "track"
        if track_type != "track":
            continue

        title = clean_cell(item.get("title", ""))
        if not title:
            continue
        tracks.append(
            DiscogsTrack(
                position=clean_cell(item.get("position", "")),
                title=title,
                artist_name=parse_artists(item.get("artists")) or default_artist_name,
            )
        )
    return tracks


def parse_artists(value: object) -> str:
    if not isinstance(value, list):
        return ""
    names: list[str] = []
    for artist in value:
        if not isinstance(artist, Mapping):
            continue
        name = clean_discogs_artist_name(clean_cell(artist.get("anv")) or clean_cell(artist.get("name")))
        if name:
            names.append(name)
    return ", ".join(names)


def clean_discogs_artist_name(name: str) -> str:
    return re.sub(r"\s+\(\d+\)$", "", name).strip()


def record_year_from_payload(payload: Mapping[str, object]) -> str:
    return first_year(clean_cell(payload.get("year", "")))


def record_year_from_row(row: Mapping[str, str]) -> str:
    return first_year(clean_cell(row.get("Released", "")))


def first_year(value: str) -> str:
    match = re.search(r"\b(\d{4})\b", value)
    return match.group(1) if match else ""


def fetch_release_tracklist(
    row: Mapping[str, str],
    token: str,
    user_agent: str,
    timeout_seconds: int,
    rate_limiter: DiscogsRateLimiter,
) -> ReleaseTracklistLookup:
    release_id = clean_cell(row.get(RELEASE_ID_COLUMN, ""))
    body = http_get(
        release_api_url(release_id),
        user_agent=user_agent,
        token=token,
        timeout_seconds=timeout_seconds,
        accept="application/json",
        rate_limiter=rate_limiter,
    )
    payload = json.loads(body or "{}")
    if not isinstance(payload, Mapping):
        payload = {}
    return release_tracklist_from_payload(release_id, payload, row)


def make_cached_tracklist_lookup(
    cache_path: Path,
    token: str,
    user_agent: str,
    timeout_seconds: int,
    request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
) -> Callable[[Mapping[str, str]], ReleaseTracklistLookup]:
    cache = load_tracklist_cache(cache_path)
    rate_limiter = DiscogsRateLimiter(
        fallback_request_interval_seconds=request_interval_seconds,
        initial_rate_limit=default_discogs_rate_limit(token),
    )

    def lookup(row: Mapping[str, str]) -> ReleaseTracklistLookup:
        release_id = clean_cell(row.get(RELEASE_ID_COLUMN, ""))
        cached_lookup = cache.get(release_id)
        if cached_lookup:
            return cached_lookup
        lookup_result = fetch_release_tracklist(
            row=row,
            token=token,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            rate_limiter=rate_limiter,
        )
        cache[release_id] = lookup_result
        save_tracklist_cache(cache_path, cache)
        return lookup_result

    return lookup


def load_tracklist_cache(path: Path) -> dict[str, ReleaseTracklistLookup]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != TRACKLIST_CACHE_SCHEMA_VERSION
        or payload.get("record_type") != TRACKLIST_CACHE_RECORD_TYPE
        or not isinstance(payload.get("records"), Mapping)
    ):
        raise ValueError("unsupported playlist track cache format; delete the old cache or choose a new --cache path")
    records = payload["records"]
    return {
        str(release_id): tracklist_lookup_from_cache_record(str(release_id), record)
        for release_id, record in records.items()
        if isinstance(record, Mapping)
    }


def tracklist_lookup_from_cache_record(release_id: str, record: Mapping[str, object]) -> ReleaseTracklistLookup:
    return ReleaseTracklistLookup(
        release_id=clean_cell(record.get("release_id")) or release_id,
        artist_name=clean_cell(record.get("artist_name")),
        album_name=clean_cell(record.get("album_name")),
        record_year=clean_cell(record.get("record_year")),
        tracks=tuple(track_from_cache_record(track) for track in record.get("tracks", []) if isinstance(track, Mapping)),
        notes=tuple(clean_cell(note) for note in record.get("notes", []) if clean_cell(note)),
    )


def track_from_cache_record(record: Mapping[str, object]) -> DiscogsTrack:
    return DiscogsTrack(
        position=clean_cell(record.get("position")),
        title=clean_cell(record.get("title")),
        artist_name=clean_cell(record.get("artist_name")),
    )


def save_tracklist_cache(path: Path, cache: Mapping[str, ReleaseTracklistLookup]) -> None:
    payload = {
        "schema_version": TRACKLIST_CACHE_SCHEMA_VERSION,
        "record_type": TRACKLIST_CACHE_RECORD_TYPE,
        "records": {
            release_id: tracklist_lookup_to_cache_record(lookup)
            for release_id, lookup in sorted(cache.items())
        },
    }
    write_json_file(path, payload)


def tracklist_lookup_to_cache_record(lookup: ReleaseTracklistLookup) -> dict[str, object]:
    return {
        "release_id": lookup.release_id,
        "artist_name": lookup.artist_name,
        "album_name": lookup.album_name,
        "record_year": lookup.record_year,
        "tracks": [
            {
                "position": track.position,
                "title": track.title,
                "artist_name": track.artist_name,
            }
            for track in lookup.tracks
        ],
        "notes": list(lookup.notes),
    }

