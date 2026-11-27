"""Local Spotify track match cache."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from publishers.spotify.matching import (
    ALPHANUMERIC_SPACING_MATCH_STRATEGY,
    AMBIGUOUS,
    CATALOGUE_RECORDING_STRATEGIES,
    CONSTRAINED_TYPO_MATCH_REASON_PREFIX,
    MANUAL_URI_ONLY_MATCH_STRATEGY,
    MATCHED,
    EXACT_TITLE_RECORDING_STRATEGY,
    REPEATED_PERFORMER_CREDIT_STRATEGY,
    SAME_RECORDING_EDITION_MATCH_STRATEGY,
    UNMATCHED,
    PlaylistTrack,
    SPOTIFY_ORIGINAL_ANNOTATION_MATCH_STRATEGY,
    SpotifyTrackCandidate,
    TrackMatchDecision,
    VERSION_SUBSTITUTE_MATCH_STRATEGY,
    build_spotify_track_search_query,
    choose_best_track_match,
    has_legacy_loss,
    legacy_music_text,
    match_catalogue_variants,
    match_exact_title_recording,
    match_repeated_performer_credit,
    music_values_match_only_after_alphanumeric_spacing,
    normalize_recording_evidence_text,
    normalize_spotify_isrc,
    recording_proof_source_matches,
    resolve_spotify_edition_tie,
    spotify_recording_metadata_from_values,
    spotify_recording_metadata_is_complete,
    spotify_track_uri_is_structurally_valid,
    with_version_title_review_candidates,
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
MATCHER_VERSION = 15
RECORDING_COHORT_STRATEGIES = CATALOGUE_RECORDING_STRATEGIES | {
    SAME_RECORDING_EDITION_MATCH_STRATEGY, REPEATED_PERFORMER_CREDIT_STRATEGY,
}
CACHEABLE_MATCH_STATUSES = {MATCHED, AMBIGUOUS, UNMATCHED}
CONSTRAINED_TYPO_MATCH_STRATEGY = "constrained_title_typo"
ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD = "album_recovery_attempt_matcher_version"
ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD = "album_recovery_attempt_album_ids"
VERSION_TITLE_REVIEW_CANDIDATES_FIELD = "version_title_review_candidates"
ALBUM_HINT_RECORDING_CONFLICT_FIELD = "album_hint_recording_conflict"
RECORDING_CONFLICT_REASON_FIELD = "recording_conflict_reason"
STRICT_ALBUM_RECOVERY_FIELD = "strict_album_recovery"
STRICT_ALBUM_MATCH_STORAGE_STATUS = "strict_album_matched"

MatchCacheRecord = TypeVar("MatchCacheRecord", bound=Mapping[str, object])


@dataclass(frozen=True)
class CachedSpotifyTrackMatch:
    cache_key: str
    decision: TrackMatchDecision
    has_recording_conflict: bool = False
    is_manual: bool = False


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
        clean_cell(cache_key): _match_record_from_storage(record)
        for cache_key, record in matches.items()
        if clean_cell(cache_key) and isinstance(record, Mapping)
    }


def save_spotify_track_match_cache(path: Path, matches: Mapping[str, Mapping[str, object]]) -> None:
    write_json_file(
        path,
        {
            "schema_version": MATCH_CACHE_SCHEMA_VERSION,
            "record_type": MATCH_CACHE_RECORD_TYPE,
            "matches": {
                cache_key: _match_record_for_storage(record) for cache_key, record in sorted(matches.items())
            },
        },
    )


def _match_record_from_storage(record: Mapping[str, object]) -> dict[str, object]:
    result = dict(record)
    if result.get("match_status") == STRICT_ALBUM_MATCH_STORAGE_STATUS:
        result["match_status"] = MATCHED
        result[STRICT_ALBUM_RECOVERY_FIELD] = True
    return result


def _match_record_for_storage(record: Mapping[str, object]) -> dict[str, object]:
    result = dict(record)
    # Older readers skip this status because strict source verification is required.
    if result.pop(STRICT_ALBUM_RECOVERY_FIELD, False) is True and result.get("match_status") == MATCHED:
        result["match_status"] = STRICT_ALBUM_MATCH_STORAGE_STATUS
    return result


def compare_manual_source(
    track: PlaylistTrack,
    *,
    release_id: object,
    track_number: object,
    title: object,
    artist: object,
    album: object,
) -> bool | None:
    """Compare the source identity, or return None for incomplete source fields."""
    if not (
        isinstance(release_id, str) and (saved_release_id := release_id.strip())
        and isinstance(track_number, str) and (saved_track_number := track_number.strip())
        and isinstance(title, str) and title.strip()
        and isinstance(artist, str) and artist.strip()
        and isinstance(album, str) and album.strip()
    ):
        return None
    return (
        saved_release_id == track.release_id.strip()
        and saved_track_number == track.track_number.strip()
        and recording_proof_source_matches(track, title=title, artist=artist, album=album)
    )


def compare_cached_manual_source(track: PlaylistTrack, record: Mapping[str, object]) -> bool | None:
    return compare_manual_source(
        track,
        release_id=record.get("release_id"),
        track_number=record.get("track_number"),
        title=record.get("track_name"),
        artist=record.get("artist_name"),
        album=record.get("album_name"),
    )


def _source_identity_fields(track: PlaylistTrack) -> tuple[tuple[str, str, Callable[[str], str]], ...]:
    return (
        ("release_id", track.release_id, str.strip),
        ("track_number", track.track_number, str.strip),
        ("artist_name", track.artist_name, normalize_recording_evidence_text),
        ("album_name", track.album_name, normalize_recording_evidence_text),
        ("track_name", track.track_name, normalize_recording_evidence_text),
    )


def cached_manual_source_conflicts(track: PlaylistTrack, record: Mapping[str, object]) -> bool:
    """Check each recorded source string, including when other source fields are missing."""
    return any(
        isinstance(saved := record.get(field), str)
        and (saved_value := normalize(saved))
        and saved_value != normalize(source_value)
        for field, source_value, normalize in _source_identity_fields(track)
    )


def _manual_collision_cache_key(track: PlaylistTrack) -> str:
    encoded = json.dumps(source_track_binding(track), ensure_ascii=True, separators=(",", ":")).encode("ascii")
    # Ordinary keys contain pipes, so older readers cannot select this entry.
    return f"manual-collision:{hashlib.sha256(encoded).hexdigest()}"


def automatic_source_matches(track: PlaylistTrack, record: Mapping[str, object]) -> bool:
    # A blank source field must be saved as a string for an automatic decision to be replayable.
    return all(
        isinstance(saved_value := record.get(field), str) and normalize(saved_value) == normalize(source_value)
        for field, source_value, normalize in _source_identity_fields(track)
    )


def track_match_cache_entry(
    track: PlaylistTrack,
    matches: Mapping[str, MatchCacheRecord],
) -> tuple[str, MatchCacheRecord | None]:
    """Select the legacy slot or the exact source's entry beside a conflicting binding."""
    cache_key = spotify_track_match_key(track)
    record = matches.get(cache_key)
    if record is not None and (
        compare_cached_manual_source(track, record) is False if record.get("match_status") == "manual"
        else _automatic_source_conflicts(track, record)
    ):
        collision_key = _manual_collision_cache_key(track)
        collision_record = matches.get(collision_key)
        if collision_record is not None and (
            clean_cell(collision_record.get("match_status")) not in CACHEABLE_MATCH_STATUSES | {"manual"}
            or not automatic_source_matches(track, collision_record)
        ):
            collision_record = None
        return collision_key, collision_record
    return cache_key, record


def _automatic_source_conflicts(track: PlaylistTrack, record: Mapping[str, object]) -> bool:
    return any(
        isinstance(saved := record.get(field), str)
        and normalize(saved) != normalize(value)
        for field, value, normalize in _source_identity_fields(track)
    )


def source_track_binding(track: PlaylistTrack) -> tuple[str, ...]:
    return tuple(normalize(value) for _, value, normalize in _source_identity_fields(track))


def clear_manual_collision_cache_entry(track: PlaylistTrack, matches: dict[str, dict[str, object]]) -> None:
    matches.pop(_manual_collision_cache_key(track), None)


def cached_track_match(
    track: PlaylistTrack,
    matches: Mapping[str, Mapping[str, object]],
    seen_at: str | None = None,
    *,
    spelling_positions: Sequence[SpotifyTrackCandidate] = (),
) -> CachedSpotifyTrackMatch | None:
    cache_key, record = track_match_cache_entry(track, matches)
    if not record:
        return None
    if cache_record_has_recording_conflict(record):
        return cached_review_decision(track, cache_key, record, AMBIGUOUS, seen_at=seen_at)
    match_status = clean_cell(record.get("match_status"))
    if match_status == "manual":
        source_match = compare_cached_manual_source(track, record)
        if source_match is False:
            return None
        if source_match is None:
            hold_reason = "cached manual Spotify match has incomplete source fields"
        elif not spotify_track_uri_is_structurally_valid(clean_cell(record.get("spotify_uri"))):
            hold_reason = "cached manual Spotify match has an invalid Spotify track URI"
        else:
            return cached_matched_track(track, cache_key, record, seen_at=seen_at)
        return CachedSpotifyTrackMatch(
            cache_key=cache_key,
            decision=TrackMatchDecision(
                track=track,
                status=AMBIGUOUS,
                spotify_uri="",
                reason=f"{hold_reason}. Re-import the verified source with manual_matches.py",
            ),
            is_manual=True,
        )
    if match_status == MATCHED:
        if (
            not _strict_album_recovery_applies_to(track, record)
            or not legacy_positive_is_safe(track, record)
            or (
                cache_record_is_version_sensitive(record)
                and cache_record_matcher_version(record) != MATCHER_VERSION
            )
        ):
            return None
        return cached_matched_track(track, cache_key, record, seen_at=seen_at, spelling_positions=spelling_positions)
    if match_status in {AMBIGUOUS, UNMATCHED}:
        if cache_record_matcher_version(record) != MATCHER_VERSION:
            return None
        return cached_review_decision(track, cache_key, record, match_status, seen_at=seen_at)
    return None


def legacy_positive_is_safe(track: PlaylistTrack, record: Mapping[str, object]) -> bool:
    if cache_record_matcher_version(record) >= MATCHER_VERSION:
        return True
    fields = (track.track_name, track.artist_name, track.album_name, clean_cell(record.get("spotify_track_name")),
              clean_cell(record.get("spotify_album_name")), *clean_string_sequence(record.get("spotify_artist_names")))
    if not any(has_legacy_loss(value) for value in fields):
        return True
    if not automatic_source_matches(track, record):
        return False
    candidate = spotify_candidate_from_cache_record(record, require_recording_metadata=True)
    return candidate is not None and choose_best_track_match(
        track, (candidate,), allow_leading_in_title_variant=False, allow_alphanumeric_spacing_fallback=False,
        allow_constrained_typo_fallback=False, allow_version_substitute=False, repeated_credit_observations=(),
        allow_catalogue_recovery=False,
    ).status == MATCHED


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


def _strict_album_recovery_applies_to(track: PlaylistTrack, record: Mapping[str, object]) -> bool:
    return record.get(STRICT_ALBUM_RECOVERY_FIELD) is not True or cached_recording_proof_is_current(track, record)


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
    *,
    spelling_positions: Sequence[SpotifyTrackCandidate] = (),
) -> CachedSpotifyTrackMatch | None:
    candidate = spotify_candidate_from_cache_record(record, default_track=track)
    if not candidate:
        return None
    match_strategy = clean_cell(record.get("match_strategy"))
    match_status = clean_cell(record.get("match_status"))
    review_candidates = (candidate,)
    if match_strategy in RECORDING_COHORT_STRATEGIES and match_status == MATCHED:
        review_candidates = cached_recording_edition_cohort(track, record, spelling_positions=spelling_positions)
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
            strict_album_recovery=match_status == MATCHED and record.get(STRICT_ALBUM_RECOVERY_FIELD) is True,
            title_search_complete=record.get("title_search_complete") is True,
        ),
        is_manual=match_status == "manual",
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
    saved_candidates = review_candidates_from_cache_record(record)
    review_candidates = tuple(candidate for candidate in saved_candidates if not candidate.version_title_review_only)
    if has_recording_conflict:
        reason = clean_cell(record.get(RECORDING_CONFLICT_REASON_FIELD)) or "cached Spotify recording conflict requires review"
        if not review_candidates and (candidate := spotify_candidate_from_cache_record(record)):
            review_candidates = (candidate,)
    if seen_at is not None and isinstance(record, dict):
        record["last_seen_at"] = seen_at
        if has_recording_conflict:
            record_cached_recording_conflict(record, reason)
    decision = TrackMatchDecision(
        track=track,
        status=match_status,
        spotify_uri="",
        reason=reason,
        review_candidates=review_candidates,
        search_queries=search_queries_from_cache_record(record),
    )
    if match_status == UNMATCHED:
        version_title_candidates = (
            *review_candidates_from_cache_record(record, field_name=VERSION_TITLE_REVIEW_CANDIDATES_FIELD),
            *(candidate for candidate in saved_candidates if candidate.version_title_review_only),
        )
        decision = with_version_title_review_candidates(decision, version_title_candidates)
    return CachedSpotifyTrackMatch(cache_key=cache_key, decision=decision, has_recording_conflict=has_recording_conflict)


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
    uri_only = record.get("match_status") == "manual" and record.get("match_strategy") == MANUAL_URI_ONLY_MATCH_STRATEGY
    metadata_complete = record.get("spotify_metadata_incomplete") is not True and spotify_recording_metadata_is_complete(
        uri=record.get("spotify_uri"),
        title=record.get("spotify_track_name"),
        artist_names=record.get("spotify_artist_names"),
        album_name=record.get("spotify_album_name"),
        album_id=record.get("spotify_album_id"),
    )
    if require_recording_metadata and not metadata_complete:
        return None
    if uri_only:
        default_track = None
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
        has_complete_recording_metadata=metadata_complete,
        observed_recording_metadata=observed_metadata,
        version_title_review_only=record.get("version_title_review_only") is True,
        title_only_search=record.get("title_only_search") is True,
    )


def review_candidates_from_cache_record(
    record: Mapping[str, object], *, field_name: str = "review_candidates",
) -> tuple[SpotifyTrackCandidate, ...]:
    candidate_records = record.get(field_name)
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


def source_bound_cached_candidates(
    record: Mapping[str, object], *, allow_incomplete_album: bool = False,
) -> tuple[SpotifyTrackCandidate, ...]:
    raw_candidates = record.get("review_candidates")
    if not isinstance(raw_candidates, list):
        return ()
    candidates: list[SpotifyTrackCandidate] = []
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            continue
        # Ordinary suggestions inherit their source only from the containing decision.
        if any(field in raw for field in ("release_id", "track_number", "track_name", "artist_name", "album_name")):
            continue
        raw_artists = raw.get("spotify_artist_names")
        if not isinstance(raw_artists, list) or not raw_artists or not all(
            isinstance(name, str) and name.strip() for name in raw_artists
        ):
            continue
        if not isinstance(raw_title := raw.get("spotify_track_name"), str) or not raw_title.strip():
            continue
        candidate = spotify_candidate_from_cache_record(raw, require_recording_metadata=not allow_incomplete_album)
        if (
            candidate and not candidate.version_title_review_only
            and spotify_track_uri_is_structurally_valid(candidate.uri)
            and "spotify_observed_recording_metadata" not in raw
        ):
            candidates.append(candidate)
    return tuple(candidates)


def cached_recording_edition_cohort(
    track: PlaylistTrack,
    record: Mapping[str, object],
    *,
    spelling_positions: Sequence[SpotifyTrackCandidate] = (),
) -> tuple[SpotifyTrackCandidate, ...]:
    strategy = clean_cell(record.get("match_strategy"))
    repeated_credit = strategy == REPEATED_PERFORMER_CREDIT_STRATEGY
    catalogue_recovery = strategy in CATALOGUE_RECORDING_STRATEGIES
    if repeated_credit or catalogue_recovery:
        if cache_record_matcher_version(record) != MATCHER_VERSION or not automatic_source_matches(track, record):
            return ()
    elif not cached_recording_proof_is_current(track, record):
        return ()
    chosen = spotify_candidate_from_cache_record(record, require_recording_metadata=True)
    candidate_records = record.get("review_candidates")
    if chosen is None or not isinstance(candidate_records, list):
        return ()
    if repeated_credit or catalogue_recovery:
        count = record.get("repeated_credit_candidate_count" if repeated_credit else "recording_candidate_count")
        if (
            type(count) is not int or count != len(candidate_records)
            or "spotify_observed_recording_metadata" in record or chosen.version_title_review_only
        ):
            return ()
    candidates: list[SpotifyTrackCandidate] = []
    for candidate_record in candidate_records:
        if not isinstance(candidate_record, Mapping):
            return ()
        candidate = spotify_candidate_from_cache_record(candidate_record, require_recording_metadata=True)
        if candidate is None or ((repeated_credit or catalogue_recovery) and (
            not candidate.has_complete_recording_metadata or candidate.version_title_review_only
            or "spotify_observed_recording_metadata" in candidate_record
        )):
            return ()
        candidates.append(candidate)
    cohort = tuple(candidates)
    if repeated_credit:
        credit_match = match_repeated_performer_credit(track, cohort)
        if (
            credit_match is None or credit_match.status != MATCHED or credit_match.candidate != chosen
            or len(credit_match.review_candidates) != len(cohort)
        ):
            return ()
        return credit_match.review_candidates
    if catalogue_recovery:
        proof = (
            match_exact_title_recording(
                track, cohort, (), title_search_complete=record.get("title_search_complete") is True,
            )
            if strategy == EXACT_TITLE_RECORDING_STRATEGY
            else match_catalogue_variants(track, cohort, (), spelling_positions)
        )
        if (
            proof is None or proof.status != MATCHED or proof.match_strategy != strategy or proof.candidate != chosen
            or len(proof.review_candidates) != len(cohort)
        ):
            return ()
        return proof.review_candidates
    proof = resolve_spotify_edition_tie(
        TrackMatchDecision(track=track, status=AMBIGUOUS, spotify_uri="", reason="", review_candidates=cohort),
        cohort,
    )
    return proof.review_candidates if proof.candidate == chosen else ()


def album_lookup_candidates_from_cache_record(
    track: PlaylistTrack,
    record: Mapping[str, object],
) -> tuple[SpotifyTrackCandidate, ...]:
    if (
        _automatic_source_conflicts(track, record) or cache_record_has_recording_conflict(record)
        or not _strict_album_recovery_applies_to(track, record)
    ):
        return ()
    match_status = clean_cell(record.get("match_status"))
    if match_status == MATCHED:
        if not legacy_positive_is_safe(track, record):
            return ()
        if clean_cell(record.get("match_strategy")) in RECORDING_COHORT_STRATEGIES:
            return tuple(candidate for candidate in cached_recording_edition_cohort(track, record)
                         if not candidate.title_only_search)
        candidate = spotify_candidate_from_cache_record(record)
        return (candidate,) if candidate else ()
    if match_status in {AMBIGUOUS, UNMATCHED}:
        return tuple(
            candidate for candidate in review_candidates_from_cache_record(record)
            if not candidate.version_title_review_only and not candidate.title_only_search
        )
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
    if cache_record_has_recording_conflict(record) or not _strict_album_recovery_applies_to(track, record):
        return False
    match_status = clean_cell(record.get("match_status"))
    if match_status == MATCHED and clean_cell(record.get("match_strategy")) in RECORDING_COHORT_STRATEGIES:
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
    *,
    refresh_match_cache: bool = False,
) -> bool:
    if decision.status not in CACHEABLE_MATCH_STATUSES:
        return False
    timestamp = matched_at or utc_timestamp()
    track = decision.track
    cache_key, existing_record = track_match_cache_entry(track, matches)
    if existing_record is not None and not refresh_match_cache:
        if clean_cell(existing_record.get("match_status")) == "manual":
            return False
        if (
            existing_record.get(STRICT_ALBUM_RECOVERY_FIELD) is True
            and cache_record_matcher_version(existing_record) == MATCHER_VERSION
            and not cached_recording_proof_is_current(track, existing_record)
        ):
            return False
    candidate = decision.candidate
    record: dict[str, object] = {
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
            return False
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
        if decision.strict_album_recovery:
            record[STRICT_ALBUM_RECOVERY_FIELD] = True
        if match_strategy == REPEATED_PERFORMER_CREDIT_STRATEGY:
            record["repeated_credit_candidate_count"] = len(decision.review_candidates)
        if match_strategy in CATALOGUE_RECORDING_STRATEGIES:
            record["recording_candidate_count"] = len(decision.review_candidates)
            record["version_sensitive"] = True
        if decision.title_search_complete:
            record["title_search_complete"] = True
        if match_strategy in RECORDING_COHORT_STRATEGIES:
            record["review_candidates"] = [
                spotify_candidate_cache_record(candidate) for candidate in decision.review_candidates
            ]
        if match_strategy in {
            ALPHANUMERIC_SPACING_MATCH_STRATEGY,
            ALBUM_ALPHANUMERIC_SPACING_MATCH_STRATEGY,
            CONSTRAINED_TYPO_MATCH_STRATEGY,
            ALBUM_POSITION_MATCH_STRATEGY,
            SAME_RECORDING_EDITION_MATCH_STRATEGY,
            REPEATED_PERFORMER_CREDIT_STRATEGY,
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
                "review_candidates": [
                    spotify_candidate_cache_record(candidate) for candidate in decision.review_candidates
                    if not candidate.version_title_review_only
                ],
                "searched_at": timestamp,
            }
        )
        _store_version_title_review_candidates(record, decision.review_candidates)
    if refresh_match_cache and cache_key == spotify_track_match_key(track):
        clear_manual_collision_cache_entry(track, matches)
    matches[cache_key] = record
    return True


def record_cached_version_title_review(record: dict[str, object], decision: TrackMatchDecision) -> bool:
    if clean_cell(record.get("match_status")) == "manual" or not _strict_album_recovery_applies_to(decision.track, record):
        return False
    record["search_queries"] = list(decision.search_queries)
    _store_version_title_review_candidates(record, decision.review_candidates)
    return True


def _store_version_title_review_candidates(
    record: dict[str, object], candidates: Sequence[SpotifyTrackCandidate],
) -> None:
    new_records = [
        spotify_candidate_cache_record(candidate) for candidate in candidates if candidate.version_title_review_only
    ]
    if not new_records:
        return
    saved_records = record.get(VERSION_TITLE_REVIEW_CANDIDATES_FIELD)
    combined_records = [*saved_records, *new_records] if isinstance(saved_records, list) else new_records
    # A shared URI can carry different title or artist evidence for colliding source rows.
    records_by_value = {
        json.dumps(candidate_record, sort_keys=True): candidate_record for candidate_record in combined_records
    }
    record[VERSION_TITLE_REVIEW_CANDIDATES_FIELD] = list(records_by_value.values())


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
    if not candidate.has_complete_recording_metadata:
        record["spotify_metadata_incomplete"] = True
    if candidate.title_only_search:
        record["title_only_search"] = True
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
            legacy_music_text(track.artist_name),
            legacy_music_text(track.album_name),
            legacy_music_text(track.track_name),
        )
    )


def spotify_url_from_uri(uri: str) -> str:
    prefix = "spotify:track:"
    if not uri.startswith(prefix):
        return ""
    return f"https://open.spotify.com/track/{uri.removeprefix(prefix)}"


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
