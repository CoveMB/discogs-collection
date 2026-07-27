"""Local Spotify track match cache."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from publishers.spotify.matching import (
    ALPHANUMERIC_SPACING_MATCH_STRATEGY,
    AMBIGUOUS,
    CONSTRAINED_TYPO_MATCH_REASON_PREFIX,
    MATCHED,
    UNMATCHED,
    PlaylistTrack,
    SPOTIFY_ORIGINAL_ANNOTATION_MATCH_STRATEGY,
    SpotifyTrackCandidate,
    TrackMatchDecision,
    VERSION_SUBSTITUTE_MATCH_STRATEGY,
    build_spotify_track_search_query,
    music_values_match_only_after_alphanumeric_spacing,
    normalize_music_text,
)
from publishers.spotify.release_matching import (
    ALBUM_ALPHANUMERIC_SPACING_MATCH_STRATEGY,
    ALBUM_EXACT_TRACK_MATCH_STRATEGY,
    ALBUM_POSITION_MATCH_STRATEGY,
    TRACK_CANDIDATE_ALBUM_ALPHANUMERIC_MATCH_STRATEGY,
    TRACK_CANDIDATE_ALBUM_EXACT_MATCH_STRATEGY,
    TRACK_CANDIDATE_ALBUM_POSITION_MATCH_STRATEGY,
)
from shared.files import write_json_file
from shared.text import clean_cell


MATCH_CACHE_SCHEMA_VERSION = 1
MATCH_CACHE_RECORD_TYPE = "spotify_track_match_cache"
MATCHER_VERSION = 13
CACHEABLE_MATCH_STATUSES = {MATCHED, AMBIGUOUS, UNMATCHED}
CONSTRAINED_TYPO_MATCH_STRATEGY = "constrained_title_typo"
ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD = "album_recovery_attempt_matcher_version"
ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD = "album_recovery_attempt_album_ids"


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
    match_status = clean_cell(record.get("match_status"))
    if match_status in {"matched", "manual"}:
        if (
            match_status == MATCHED
            and cache_record_is_version_sensitive(record)
            and cache_record_matcher_version(record) != MATCHER_VERSION
        ):
            return None
        return cached_matched_track(track, cache_key, record, seen_at=seen_at)
    if match_status in {AMBIGUOUS, UNMATCHED}:
        if cache_record_matcher_version(record) != MATCHER_VERSION:
            return None
        return cached_review_decision(track, cache_key, record, match_status, seen_at=seen_at)
    return None


def cache_record_matcher_version(record: Mapping[str, object]) -> int:
    try:
        return int(str(record.get("matcher_version", "")).strip())
    except ValueError:
        return 0


def cache_record_is_version_sensitive(record: Mapping[str, object]) -> bool:
    if record.get("version_sensitive") is True:
        return True
    if clean_cell(record.get("match_strategy")) != ALBUM_EXACT_TRACK_MATCH_STRATEGY:
        return False
    return (
        music_values_match_only_after_alphanumeric_spacing(
            clean_cell(record.get("track_name")),
            clean_cell(record.get("spotify_track_name")),
        )
        or music_values_match_only_after_alphanumeric_spacing(
            clean_cell(record.get("album_name")),
            clean_cell(record.get("spotify_album_name")),
        )
    )


def cached_matched_track(
    track: PlaylistTrack,
    cache_key: str,
    record: Mapping[str, object],
    seen_at: str | None = None,
) -> CachedSpotifyTrackMatch | None:
    candidate = spotify_candidate_from_cache_record(record, default_track=track)
    if not candidate:
        return None
    if seen_at is not None and isinstance(record, dict):
        record["last_seen_at"] = seen_at
    return CachedSpotifyTrackMatch(
        cache_key=cache_key,
        decision=TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=candidate.uri,
            reason=clean_cell(record.get("match_reason")) or "cached Spotify match",
            candidate=candidate,
            review_candidates=(candidate,),
            search_queries=search_queries_from_cache_record(record),
            match_strategy=clean_cell(record.get("match_strategy")),
        ),
    )


def cached_review_decision(
    track: PlaylistTrack,
    cache_key: str,
    record: Mapping[str, object],
    match_status: str,
    seen_at: str | None = None,
) -> CachedSpotifyTrackMatch:
    if seen_at is not None and isinstance(record, dict):
        record["last_seen_at"] = seen_at
    return CachedSpotifyTrackMatch(
        cache_key=cache_key,
        decision=TrackMatchDecision(
            track=track,
            status=match_status,
            spotify_uri="",
            reason=clean_cell(record.get("match_reason")) or f"cached Spotify {match_status} decision",
            candidate=None,
            review_candidates=review_candidates_from_cache_record(record),
            search_queries=search_queries_from_cache_record(record),
        ),
    )


def search_queries_from_cache_record(record: Mapping[str, object]) -> tuple[str, ...]:
    search_queries = clean_string_sequence(record.get("search_queries"))
    if search_queries:
        return search_queries
    search_query = clean_cell(record.get("search_query"))
    return (search_query,) if search_query else ()


def spotify_candidate_from_cache_record(
    record: Mapping[str, object],
    default_track: PlaylistTrack | None = None,
) -> SpotifyTrackCandidate | None:
    spotify_uri = clean_cell(record.get("spotify_uri"))
    if not spotify_uri.startswith("spotify:track:"):
        return None
    artist_names = clean_string_sequence(record.get("spotify_artist_names"))
    return SpotifyTrackCandidate(
        uri=spotify_uri,
        name=clean_cell(record.get("spotify_track_name")) or (default_track.track_name if default_track else ""),
        artists=artist_names or ((default_track.artist_name,) if default_track else ()),
        album_name=clean_cell(record.get("spotify_album_name")) or (default_track.album_name if default_track else ""),
        album_id=clean_cell(record.get("spotify_album_id")),
    )


def review_candidates_from_cache_record(record: Mapping[str, object]) -> tuple[SpotifyTrackCandidate, ...]:
    candidate_records = record.get("review_candidates", ())
    if not isinstance(candidate_records, list):
        return ()
    candidates: list[SpotifyTrackCandidate] = []
    for candidate_record in candidate_records:
        if not isinstance(candidate_record, Mapping):
            continue
        candidate = spotify_candidate_from_cache_record(candidate_record)
        if candidate:
            candidates.append(candidate)
    return tuple(candidates)


def album_lookup_candidates_from_cache_record(
    record: Mapping[str, object],
) -> tuple[SpotifyTrackCandidate, ...]:
    match_status = clean_cell(record.get("match_status"))
    if match_status == MATCHED:
        candidate = spotify_candidate_from_cache_record(record)
        return (candidate,) if candidate else ()
    if match_status in {AMBIGUOUS, UNMATCHED}:
        return review_candidates_from_cache_record(record)
    return ()


def canonical_album_recovery_candidate_ids(
    album_ids: Sequence[str],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                clean_album_id
                for album_id in album_ids
                if (clean_album_id := clean_cell(album_id))
            }
        )
    )


def cache_record_has_current_album_recovery_attempt(
    record: Mapping[str, object],
    album_ids: Sequence[str],
) -> bool:
    if not cache_record_can_store_album_recovery_attempt(record):
        return False
    try:
        attempt_matcher_version = int(
            str(record.get(ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD, "")).strip()
        )
    except ValueError:
        return False
    raw_album_ids = record.get(ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD)
    if not isinstance(raw_album_ids, list) or not all(
        isinstance(album_id, str) and album_id.strip()
        for album_id in raw_album_ids
    ):
        return False
    stored_album_ids = tuple(album_id.strip() for album_id in raw_album_ids)
    expected_album_ids = canonical_album_recovery_candidate_ids(album_ids)
    return bool(
        expected_album_ids
        and attempt_matcher_version == MATCHER_VERSION
        and stored_album_ids == expected_album_ids
    )


def record_album_recovery_attempt(
    record: dict[str, object],
    album_ids: Sequence[str],
) -> None:
    if not cache_record_can_store_album_recovery_attempt(record):
        return
    canonical_album_ids = canonical_album_recovery_candidate_ids(album_ids)
    if not canonical_album_ids:
        return
    record[ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD] = MATCHER_VERSION
    record[ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD] = list(canonical_album_ids)


def cache_record_can_store_album_recovery_attempt(
    record: Mapping[str, object],
) -> bool:
    match_status = clean_cell(record.get("match_status"))
    return bool(
        match_status == MATCHED
        or (
            match_status in {AMBIGUOUS, UNMATCHED}
            and cache_record_matcher_version(record) == MATCHER_VERSION
        )
    )


def cache_track_match(
    matches: dict[str, dict[str, object]],
    decision: TrackMatchDecision,
    matched_at: str | None = None,
) -> None:
    if decision.status not in CACHEABLE_MATCH_STATUSES:
        return
    timestamp = matched_at or utc_timestamp()
    track = decision.track
    candidate = decision.candidate
    record = {
        "release_id": track.release_id,
        "track_number": track.track_number,
        "artist_name": track.artist_name,
        "album_name": track.album_name,
        "track_name": track.track_name,
        "search_query": build_spotify_track_search_query(track),
        "search_queries": list(decision.search_queries),
        "match_status": decision.status,
        "match_reason": decision.reason,
        "matcher_version": MATCHER_VERSION,
        "last_seen_at": timestamp,
    }
    if decision.status == MATCHED:
        if not decision.spotify_uri.startswith("spotify:track:"):
            return
        record.update(
            {
                "spotify_uri": decision.spotify_uri,
                "spotify_url": spotify_url_from_uri(decision.spotify_uri),
                "spotify_track_name": candidate.name if candidate else track.track_name,
                "spotify_artist_names": list(candidate.artists if candidate else (track.artist_name,)),
                "spotify_album_name": candidate.album_name if candidate else track.album_name,
                "matched_at": timestamp,
            }
        )
        match_strategy = decision.match_strategy
        if not match_strategy and decision.reason.startswith(CONSTRAINED_TYPO_MATCH_REASON_PREFIX):
            match_strategy = CONSTRAINED_TYPO_MATCH_STRATEGY
        if match_strategy:
            record["match_strategy"] = match_strategy
        if candidate and candidate.album_id:
            record["spotify_album_id"] = candidate.album_id
        if match_strategy in {
            ALPHANUMERIC_SPACING_MATCH_STRATEGY,
            ALBUM_ALPHANUMERIC_SPACING_MATCH_STRATEGY,
            CONSTRAINED_TYPO_MATCH_STRATEGY,
            ALBUM_POSITION_MATCH_STRATEGY,
            SPOTIFY_ORIGINAL_ANNOTATION_MATCH_STRATEGY,
            TRACK_CANDIDATE_ALBUM_ALPHANUMERIC_MATCH_STRATEGY,
            TRACK_CANDIDATE_ALBUM_EXACT_MATCH_STRATEGY,
            TRACK_CANDIDATE_ALBUM_POSITION_MATCH_STRATEGY,
            VERSION_SUBSTITUTE_MATCH_STRATEGY,
        }:
            record.update(
                {
                    "version_sensitive": True,
                }
            )
    else:
        record.update(
            {
                "spotify_uri": "",
                "spotify_url": "",
                "review_candidates": [spotify_candidate_cache_record(candidate) for candidate in decision.review_candidates],
                "searched_at": timestamp,
            }
        )
    matches[spotify_track_match_key(track)] = record


def spotify_candidate_cache_record(candidate: SpotifyTrackCandidate) -> dict[str, object]:
    record = {
        "spotify_uri": candidate.uri,
        "spotify_url": spotify_url_from_uri(candidate.uri),
        "spotify_track_name": candidate.name,
        "spotify_artist_names": list(candidate.artists),
        "spotify_album_name": candidate.album_name,
    }
    if candidate.album_id:
        record["spotify_album_id"] = candidate.album_id
    return record


def clean_string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(clean_value for item in value if (clean_value := clean_cell(item)))


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
