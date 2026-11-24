"""Local Spotify track match cache."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from publishers.spotify.matching import (
    ALPHANUMERIC_SPACING_MATCH_STRATEGY,
    AMBIGUOUS,
    CONSTRAINED_TYPO_MATCH_REASON_PREFIX,
    MATCHED,
    SAME_RECORDING_EDITION_MATCH_STRATEGY,
    UNMATCHED,
    PlaylistTrack,
    SPOTIFY_ORIGINAL_ANNOTATION_MATCH_STRATEGY,
    SpotifyTrackCandidate,
    TrackMatchDecision,
    VERSION_SUBSTITUTE_MATCH_STRATEGY,
    build_spotify_track_search_query,
    music_values_match_only_after_alphanumeric_spacing,
    normalize_music_text,
    normalize_spotify_isrc,
    recording_proof_source_matches,
    resolve_spotify_edition_tie,
    spotify_recording_metadata_from_values,
    spotify_recording_metadata_is_complete,
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
ALBUM_HINT_RECORDING_CONFLICT_FIELD = "album_hint_recording_conflict"
RECORDING_CONFLICT_REASON_FIELD = "recording_conflict_reason"


@dataclass(frozen=True)
class CachedSpotifyTrackMatch:
    cache_key: str
    decision: TrackMatchDecision
    has_recording_conflict: bool = False


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
    if cache_record_has_recording_conflict(record):
        return cached_review_decision(track, cache_key, record, AMBIGUOUS, seen_at=seen_at)
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


def cache_record_has_recording_conflict(record: Mapping[str, object] | None) -> bool:
    return (
        record is not None
        and record.get(ALBUM_HINT_RECORDING_CONFLICT_FIELD) is True
        and clean_cell(record.get("match_status")) != "manual"
    )


def record_cached_recording_conflict(record: dict[str, object], reason: str) -> None:
    if clean_cell(record.get("match_status")) == "manual":
        return
    record[ALBUM_HINT_RECORDING_CONFLICT_FIELD] = True
    record[RECORDING_CONFLICT_REASON_FIELD] = reason
    record.pop(ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD, None)
    record.pop(ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD, None)


def cache_record_matcher_version(record: Mapping[str, object]) -> int:
    try:
        return int(str(record.get("matcher_version", "")).strip())
    except ValueError:
        return 0


def cached_recording_proof_is_current(track: PlaylistTrack, record: Mapping[str, object]) -> bool:
    title = record.get("track_name")
    artist = record.get("artist_name")
    album = record.get("album_name")
    return (
        cache_record_matcher_version(record) == MATCHER_VERSION
        and isinstance(title, str)
        and isinstance(artist, str)
        and isinstance(album, str)
        and recording_proof_source_matches(track, title=title, artist=artist, album=album)
    )


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
    match_strategy = clean_cell(record.get("match_strategy"))
    match_status = clean_cell(record.get("match_status"))
    review_candidates = (candidate,)
    if match_strategy == SAME_RECORDING_EDITION_MATCH_STRATEGY and match_status == MATCHED:
        review_candidates = cached_recording_edition_cohort(track, record)
        if not review_candidates:
            return None
    if seen_at is not None and isinstance(record, dict):
        record["last_seen_at"] = seen_at
        if match_status == "manual":
            record.pop(ALBUM_HINT_RECORDING_CONFLICT_FIELD, None)
            record.pop(RECORDING_CONFLICT_REASON_FIELD, None)
    return CachedSpotifyTrackMatch(
        cache_key=cache_key,
        decision=TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=candidate.uri,
            reason=clean_cell(record.get("match_reason")) or "cached Spotify match",
            candidate=candidate,
            review_candidates=review_candidates,
            search_queries=search_queries_from_cache_record(record),
            match_strategy=match_strategy,
        ),
    )


def cached_review_decision(
    track: PlaylistTrack,
    cache_key: str,
    record: Mapping[str, object],
    match_status: str,
    seen_at: str | None = None,
) -> CachedSpotifyTrackMatch:
    has_recording_conflict = cache_record_has_recording_conflict(record)
    reason = clean_cell(record.get("match_reason")) or f"cached Spotify {match_status} decision"
    review_candidates = review_candidates_from_cache_record(record)
    if has_recording_conflict:
        reason = clean_cell(record.get(RECORDING_CONFLICT_REASON_FIELD)) or "cached Spotify recording conflict requires review"
        if not review_candidates and (candidate := spotify_candidate_from_cache_record(record)):
            review_candidates = (candidate,)
    if seen_at is not None and isinstance(record, dict):
        record["last_seen_at"] = seen_at
        if has_recording_conflict:
            record_cached_recording_conflict(record, reason)
    return CachedSpotifyTrackMatch(
        cache_key=cache_key,
        decision=TrackMatchDecision(
            track=track,
            status=match_status,
            spotify_uri="",
            reason=reason,
            candidate=None,
            review_candidates=review_candidates,
            search_queries=search_queries_from_cache_record(record),
        ),
        has_recording_conflict=has_recording_conflict,
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
    *,
    require_recording_metadata: bool = False,
) -> SpotifyTrackCandidate | None:
    if require_recording_metadata and not spotify_recording_metadata_is_complete(
        uri=record.get("spotify_uri"),
        title=record.get("spotify_track_name"),
        artist_names=record.get("spotify_artist_names"),
        album_name=record.get("spotify_album_name"),
        album_id=record.get("spotify_album_id"),
    ):
        return None
    spotify_uri = clean_cell(record.get("spotify_uri"))
    if not spotify_uri.startswith("spotify:track:"):
        return None
    raw_title = record.get("spotify_track_name")
    raw_artists = record.get("spotify_artist_names")
    raw_album_name = record.get("spotify_album_name")
    raw_album_id = record.get("spotify_album_id")
    artist_names = clean_string_sequence(raw_artists)
    is_playable = record.get("spotify_is_playable")
    saved_observation = record.get("spotify_observed_recording_metadata")
    if isinstance(saved_observation, Mapping) and not require_recording_metadata:
        observed_metadata = spotify_recording_metadata_from_values(
            title=saved_observation.get("title"),
            artist_names=saved_observation.get("artist_names"),
            album_name=saved_observation.get("album_name"),
            album_id=saved_observation.get("album_id"),
        )
    else:
        observed_metadata = spotify_recording_metadata_from_values(
            title=raw_title, artist_names=raw_artists, album_name=raw_album_name, album_id=raw_album_id,
        )
    return SpotifyTrackCandidate(
        uri=spotify_uri,
        name=clean_cell(raw_title) or (default_track.track_name if default_track else ""),
        artists=artist_names or ((default_track.artist_name,) if default_track else ()),
        album_name=clean_cell(raw_album_name) or (default_track.album_name if default_track else ""),
        album_id=clean_cell(raw_album_id),
        isrc=normalize_spotify_isrc(record.get("spotify_isrc")),
        is_playable=is_playable if isinstance(is_playable, bool) else None,
        observed_recording_metadata=observed_metadata,
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


def cached_recording_edition_cohort(
    track: PlaylistTrack,
    record: Mapping[str, object],
) -> tuple[SpotifyTrackCandidate, ...]:
    if not cached_recording_proof_is_current(track, record):
        return ()
    chosen = spotify_candidate_from_cache_record(record, require_recording_metadata=True)
    candidate_records = record.get("review_candidates")
    if chosen is None or not isinstance(candidate_records, list):
        return ()
    candidates: list[SpotifyTrackCandidate] = []
    for candidate_record in candidate_records:
        if not isinstance(candidate_record, Mapping):
            return ()
        candidate = spotify_candidate_from_cache_record(candidate_record, require_recording_metadata=True)
        if candidate is None:
            return ()
        candidates.append(candidate)
    cohort = tuple(candidates)
    proof = resolve_spotify_edition_tie(
        TrackMatchDecision(track=track, status=AMBIGUOUS, spotify_uri="", reason="", review_candidates=cohort),
        cohort,
    )
    return proof.review_candidates if proof.candidate == chosen else ()


def album_lookup_candidates_from_cache_record(
    track: PlaylistTrack,
    record: Mapping[str, object],
) -> tuple[SpotifyTrackCandidate, ...]:
    if cache_record_has_recording_conflict(record):
        return ()
    match_status = clean_cell(record.get("match_status"))
    if match_status == MATCHED:
        if clean_cell(record.get("match_strategy")) == SAME_RECORDING_EDITION_MATCH_STRATEGY:
            return cached_recording_edition_cohort(track, record)
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
    track: PlaylistTrack,
    record: Mapping[str, object],
    album_ids: Sequence[str],
) -> bool:
    if not cache_record_can_store_album_recovery_attempt(track, record):
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
    track: PlaylistTrack,
    record: dict[str, object],
    album_ids: Sequence[str],
) -> None:
    if not cache_record_can_store_album_recovery_attempt(track, record):
        return
    canonical_album_ids = canonical_album_recovery_candidate_ids(album_ids)
    if not canonical_album_ids:
        return
    record[ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD] = MATCHER_VERSION
    record[ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD] = list(canonical_album_ids)


def cache_record_can_store_album_recovery_attempt(
    track: PlaylistTrack,
    record: Mapping[str, object],
) -> bool:
    if cache_record_has_recording_conflict(record):
        return False
    match_status = clean_cell(record.get("match_status"))
    if match_status == MATCHED and clean_cell(record.get("match_strategy")) == SAME_RECORDING_EDITION_MATCH_STRATEGY:
        return bool(cached_recording_edition_cohort(track, record))
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
        record.update(spotify_candidate_cache_record(
            candidate or SpotifyTrackCandidate(
                uri=decision.spotify_uri,
                name=track.track_name,
                artists=(track.artist_name,),
                album_name=track.album_name,
            )
        ))
        record.update(
            {
                "spotify_uri": decision.spotify_uri,
                "spotify_url": spotify_url_from_uri(decision.spotify_uri),
                "matched_at": timestamp,
            }
        )
        match_strategy = decision.match_strategy
        if not match_strategy and decision.reason.startswith(CONSTRAINED_TYPO_MATCH_REASON_PREFIX):
            match_strategy = CONSTRAINED_TYPO_MATCH_STRATEGY
        if match_strategy:
            record["match_strategy"] = match_strategy
        if match_strategy == SAME_RECORDING_EDITION_MATCH_STRATEGY:
            record["review_candidates"] = [
                spotify_candidate_cache_record(candidate) for candidate in decision.review_candidates
            ]
        if match_strategy in {
            ALPHANUMERIC_SPACING_MATCH_STRATEGY,
            ALBUM_ALPHANUMERIC_SPACING_MATCH_STRATEGY,
            CONSTRAINED_TYPO_MATCH_STRATEGY,
            ALBUM_POSITION_MATCH_STRATEGY,
            SAME_RECORDING_EDITION_MATCH_STRATEGY,
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
    record: dict[str, object] = {
        "spotify_uri": candidate.uri,
        "spotify_url": spotify_url_from_uri(candidate.uri),
        "spotify_track_name": candidate.name,
        "spotify_artist_names": list(candidate.artists),
        "spotify_album_name": candidate.album_name,
    }
    if candidate.album_id:
        record["spotify_album_id"] = candidate.album_id
    if candidate.isrc:
        record["spotify_isrc"] = candidate.isrc
    if candidate.is_playable is not None:
        record["spotify_is_playable"] = candidate.is_playable
    observed_metadata = candidate.observed_recording_metadata
    if observed_metadata is not None and observed_metadata != spotify_recording_metadata_from_values(
        title=candidate.name, artist_names=candidate.artists, album_name=candidate.album_name, album_id=candidate.album_id,
    ):
        record["spotify_observed_recording_metadata"] = asdict(observed_metadata)
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
