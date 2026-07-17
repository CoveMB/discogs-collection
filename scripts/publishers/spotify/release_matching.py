"""Validated Spotify album matching for Discogs release track groups."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from publishers.spotify.matching import (
    MATCHED,
    PlaylistTrack,
    SpotifyTrackCandidate,
    TrackMatchDecision,
    music_values_match_only_after_alphanumeric_spacing,
    normalize_alphanumeric_spacing,
    normalize_music_text,
    normalized_candidate_artist_set,
    normalized_source_artist_set,
    quote_search_filter_value,
)


MINIMUM_RELEASE_TRACKS_FOR_ALBUM_LOOKUP = 3
MAXIMUM_EXACT_ALBUM_CANDIDATES_TO_FETCH = 5
MAXIMUM_POSITIONAL_GAP_TRACKS = 3
ALBUM_EXACT_TRACK_MATCH_STRATEGY = "exact_track_in_validated_album"
ALBUM_ALPHANUMERIC_SPACING_MATCH_STRATEGY = (
    "exact_track_in_alphanumeric_spacing_validated_album"
)
ALBUM_POSITION_MATCH_STRATEGY = "validated_album_position"
ALBUM_EXACT_TRACK_MATCH_REASON = "track matched exactly within a validated Spotify album"
ALBUM_ALPHANUMERIC_SPACING_MATCH_REASON = (
    f"{ALBUM_EXACT_TRACK_MATCH_REASON} after normalizing letter-number spacing"
)
ALBUM_POSITION_MATCH_REASON = "track matched by position within a validated Spotify album sequence"
TRACK_CANDIDATE_ALBUM_EXACT_MATCH_STRATEGY = "exact_track_in_track_candidate_album"
TRACK_CANDIDATE_ALBUM_ALPHANUMERIC_MATCH_STRATEGY = (
    "exact_track_in_alphanumeric_spacing_track_candidate_album"
)
TRACK_CANDIDATE_ALBUM_POSITION_MATCH_STRATEGY = "position_in_track_candidate_album"
TRACK_CANDIDATE_ALBUM_REASON_SUFFIX = "album recovered from cached track-search candidates"


@dataclass(frozen=True)
class SpotifyAlbumCandidate:
    album_id: str
    uri: str
    name: str
    artists: tuple[str, ...]
    total_tracks: int


@dataclass(frozen=True)
class SpotifyAlbumTrack:
    uri: str
    name: str
    artists: tuple[str, ...]
    disc_number: int
    track_number: int
    is_playable: bool | None = None


@dataclass(frozen=True)
class SpotifyAlbumValidationDiagnostic:
    album_id: str
    album_name: str
    source_track_count: int
    spotify_track_count: int
    exact_anchor_count: int
    title_anchor_count: int
    artist_only_source_track_count: int
    anchor_order_is_valid: bool
    unequal_gap_count: int
    oversized_gap_count: int
    positional_artist_mismatch_count: int
    unusable_spotify_track_count: int


@dataclass(frozen=True)
class SpotifyReleaseAlbumMatchResult:
    decisions: tuple[TrackMatchDecision, ...]
    search_count: int
    diagnostic: str = ""
    validation_diagnostics: tuple[SpotifyAlbumValidationDiagnostic, ...] = ()
    used_fallback_track_candidates: bool = False


@runtime_checkable
class SpotifyAlbumLookupClient(Protocol):
    def search_albums(
        self,
        access_token: str,
        query: str,
        limit: int = 10,
    ) -> tuple[SpotifyAlbumCandidate, ...]: ...

    def get_album_tracks(
        self,
        access_token: str,
        album_id: str,
    ) -> tuple[SpotifyAlbumTrack, ...]: ...


def build_spotify_album_search_query(album_name: str) -> str:
    clean_album_name = quote_search_filter_value(album_name)
    return f'album:"{clean_album_name}"' if clean_album_name else ""


def release_is_eligible_for_album_lookup(source_tracks: Sequence[PlaylistTrack]) -> bool:
    if len(source_tracks) < MINIMUM_RELEASE_TRACKS_FOR_ALBUM_LOOKUP:
        return False
    first_track = source_tracks[0]
    release_id = first_track.release_id.strip()
    normalized_album_name = normalize_music_text(first_track.album_name)
    if not release_id or not normalized_album_name:
        return False
    return all(
        track.release_id.strip() == release_id
        and normalize_music_text(track.album_name) == normalized_album_name
        for track in source_tracks
    )


def resolve_release_with_album(
    source_tracks: Sequence[PlaylistTrack],
    spotify_client: SpotifyAlbumLookupClient,
    access_token: str,
    search_limit: int = 10,
    fallback_track_candidates: Sequence[SpotifyTrackCandidate] = (),
) -> SpotifyReleaseAlbumMatchResult:
    source_tracks = tuple(source_tracks)
    if not release_is_eligible_for_album_lookup(source_tracks):
        return SpotifyReleaseAlbumMatchResult(
            decisions=(),
            search_count=0,
            diagnostic="release did not meet album lookup requirements",
        )

    album_search_query = build_spotify_album_search_query(source_tracks[0].album_name)
    candidates = spotify_client.search_albums(
        access_token=access_token,
        query=album_search_query,
        limit=search_limit,
    )
    normalized_source_album_name = normalize_alphanumeric_spacing(source_tracks[0].album_name)
    exact_album_candidates_by_id: dict[str, SpotifyAlbumCandidate] = {}
    for candidate in candidates:
        if (
            candidate.album_id
            and normalize_alphanumeric_spacing(candidate.name) == normalized_source_album_name
        ):
            exact_album_candidates_by_id.setdefault(candidate.album_id, candidate)
    exact_album_candidates = tuple(exact_album_candidates_by_id.values())
    candidates_came_from_track_search = False
    if not exact_album_candidates:
        exact_album_candidates = album_candidates_from_track_candidates(
            source_tracks=source_tracks,
            track_candidates=fallback_track_candidates,
        )
        candidates_came_from_track_search = bool(exact_album_candidates)
        if not exact_album_candidates:
            return SpotifyReleaseAlbumMatchResult(
                decisions=(),
                search_count=1,
                diagnostic="Spotify returned no album candidate with a matching title",
            )
    if len(exact_album_candidates) > MAXIMUM_EXACT_ALBUM_CANDIDATES_TO_FETCH:
        return SpotifyReleaseAlbumMatchResult(
            decisions=(),
            search_count=1,
            diagnostic=(
                f"Spotify returned {len(exact_album_candidates)} album candidates with a matching title; "
                f"the safe maximum is {MAXIMUM_EXACT_ALBUM_CANDIDATES_TO_FETCH}"
            ),
            used_fallback_track_candidates=candidates_came_from_track_search,
        )

    candidate_matches: list[tuple[TrackMatchDecision, ...]] = []
    validation_diagnostics: list[SpotifyAlbumValidationDiagnostic] = []
    for album in exact_album_candidates:
        spotify_tracks = spotify_client.get_album_tracks(
            access_token=access_token,
            album_id=album.album_id,
        )
        validation_diagnostics.append(
            analyze_album_sequence(
                source_tracks=source_tracks,
                album=album,
                spotify_tracks=spotify_tracks,
            )
        )
        decisions_for_candidate = match_release_tracks_to_album(
            source_tracks=source_tracks,
            album=album,
            spotify_tracks=spotify_tracks,
            album_search_query=album_search_query,
        )
        if candidates_came_from_track_search:
            decisions_for_candidate = mark_track_candidate_album_decisions(
                decisions_for_candidate
            )
        candidate_matches.append(decisions_for_candidate)
    validated_matches = tuple(decisions for decisions in candidate_matches if decisions)
    decisions = choose_validated_album_match(tuple(candidate_matches))
    if not validated_matches:
        diagnostic = (
            "matching-title Spotify albums failed sequence validation: "
            f"{format_album_validation_diagnostics(validation_diagnostics)}"
        )
    elif len(validated_matches) > 1:
        diagnostic = (
            f"{len(validated_matches)} Spotify album editions passed sequence validation, so none was selected; "
            f"validation details: {format_album_validation_diagnostics(validation_diagnostics)}"
        )
    elif len(decisions) < len(source_tracks):
        selected_index = next(
            index
            for index, candidate_decisions in enumerate(candidate_matches)
            if candidate_decisions
        )
        diagnostic = (
            f"validated Spotify album resolved {len(decisions)} of {len(source_tracks)} release tracks; "
            f"validation details: {format_album_validation_diagnostic(validation_diagnostics[selected_index])}"
        )
    else:
        diagnostic = ""
    return SpotifyReleaseAlbumMatchResult(
        decisions=decisions,
        search_count=1,
        diagnostic=diagnostic,
        validation_diagnostics=tuple(validation_diagnostics),
        used_fallback_track_candidates=candidates_came_from_track_search,
    )


def album_candidates_from_track_candidates(
    source_tracks: Sequence[PlaylistTrack],
    track_candidates: Sequence[SpotifyTrackCandidate],
) -> tuple[SpotifyAlbumCandidate, ...]:
    source_tracks = tuple(source_tracks)
    if not source_tracks:
        return ()
    source_album = normalize_alphanumeric_spacing(source_tracks[0].album_name)
    candidates_by_album_id: dict[str, SpotifyAlbumCandidate] = {}
    for candidate in track_candidates:
        if (
            not candidate.album_id
            or not source_album
            or normalize_alphanumeric_spacing(candidate.album_name) != source_album
        ):
            continue
        candidates_by_album_id.setdefault(
            candidate.album_id,
            SpotifyAlbumCandidate(
                album_id=candidate.album_id,
                uri=f"spotify:album:{candidate.album_id}",
                name=candidate.album_name,
                artists=candidate.artists,
                total_tracks=0,
            ),
        )
    return tuple(candidates_by_album_id.values())


def mark_track_candidate_album_decisions(
    decisions: Sequence[TrackMatchDecision],
) -> tuple[TrackMatchDecision, ...]:
    strategy_by_album_strategy = {
        ALBUM_EXACT_TRACK_MATCH_STRATEGY: TRACK_CANDIDATE_ALBUM_EXACT_MATCH_STRATEGY,
        ALBUM_ALPHANUMERIC_SPACING_MATCH_STRATEGY: TRACK_CANDIDATE_ALBUM_ALPHANUMERIC_MATCH_STRATEGY,
        ALBUM_POSITION_MATCH_STRATEGY: TRACK_CANDIDATE_ALBUM_POSITION_MATCH_STRATEGY,
    }
    return tuple(
        replace(
            decision,
            reason=f"{decision.reason}; {TRACK_CANDIDATE_ALBUM_REASON_SUFFIX}",
            match_strategy=strategy_by_album_strategy[decision.match_strategy],
        )
        for decision in decisions
    )


def analyze_album_sequence(
    source_tracks: Sequence[PlaylistTrack],
    album: SpotifyAlbumCandidate,
    spotify_tracks: Sequence[SpotifyAlbumTrack],
) -> SpotifyAlbumValidationDiagnostic:
    source_tracks = tuple(source_tracks)
    spotify_tracks = tuple(spotify_tracks)
    exact_anchors = exact_track_anchors(source_tracks, spotify_tracks)
    title_anchors = unique_title_anchors(source_tracks, spotify_tracks)
    exact_anchor_source_indices = {source_index for source_index, _spotify_index in exact_anchors}
    artist_only_source_track_count = sum(
        1
        for source_index, source_track in enumerate(source_tracks)
        if source_index not in exact_anchor_source_indices
        and any(
            spotify_album_track_is_usable(spotify_track)
            and full_artist_sets_are_equal(source_track.artist_name, spotify_track.artists)
            for spotify_track in spotify_tracks
        )
    )
    anchor_order_is_valid = anchors_are_monotonic(exact_anchors)
    unequal_gap_count = 0
    oversized_gap_count = 0
    positional_artist_mismatch_count = 0
    if anchor_order_is_valid:
        boundaries = (
            ((-1, -1),)
            + exact_anchors
            + ((len(source_tracks), len(spotify_tracks)),)
        )
        for (left_source, left_spotify), (right_source, right_spotify) in zip(
            boundaries,
            boundaries[1:],
        ):
            source_gap = tuple(range(left_source + 1, right_source))
            spotify_gap = tuple(range(left_spotify + 1, right_spotify))
            if len(source_gap) != len(spotify_gap):
                unequal_gap_count += 1
                continue
            if len(source_gap) > MAXIMUM_POSITIONAL_GAP_TRACKS:
                oversized_gap_count += 1
                continue
            positional_artist_mismatch_count += sum(
                1
                for source_index, spotify_index in zip(source_gap, spotify_gap)
                if spotify_album_track_is_usable(spotify_tracks[spotify_index])
                and not full_artist_sets_are_equal(
                    source_tracks[source_index].artist_name,
                    spotify_tracks[spotify_index].artists,
                )
            )
    return SpotifyAlbumValidationDiagnostic(
        album_id=album.album_id,
        album_name=album.name,
        source_track_count=len(source_tracks),
        spotify_track_count=len(spotify_tracks),
        exact_anchor_count=len(exact_anchors),
        title_anchor_count=len(title_anchors),
        artist_only_source_track_count=artist_only_source_track_count,
        anchor_order_is_valid=anchor_order_is_valid,
        unequal_gap_count=unequal_gap_count,
        oversized_gap_count=oversized_gap_count,
        positional_artist_mismatch_count=positional_artist_mismatch_count,
        unusable_spotify_track_count=sum(
            1 for track in spotify_tracks if not spotify_album_track_is_usable(track)
        ),
    )


def format_album_validation_diagnostics(
    diagnostics: Sequence[SpotifyAlbumValidationDiagnostic],
) -> str:
    return " | ".join(format_album_validation_diagnostic(diagnostic) for diagnostic in diagnostics)


def format_album_validation_diagnostic(
    diagnostic: SpotifyAlbumValidationDiagnostic,
) -> str:
    anchor_order = "valid" if diagnostic.anchor_order_is_valid else "conflicting"
    return (
        f"{diagnostic.album_id} ({diagnostic.album_name}): "
        f"source/Spotify tracks {diagnostic.source_track_count}/{diagnostic.spotify_track_count}; "
        f"exact title-and-artist anchors {diagnostic.exact_anchor_count}; "
        f"title anchors {diagnostic.title_anchor_count}; "
        f"artist-only source tracks {diagnostic.artist_only_source_track_count}; "
        f"anchor order {anchor_order}; unequal gaps {diagnostic.unequal_gap_count}; "
        f"oversized gaps {diagnostic.oversized_gap_count}; "
        f"positional artist mismatches {diagnostic.positional_artist_mismatch_count}; "
        f"unusable Spotify tracks {diagnostic.unusable_spotify_track_count}"
    )


def choose_validated_album_match(
    candidate_matches: Sequence[tuple[TrackMatchDecision, ...]],
) -> tuple[TrackMatchDecision, ...]:
    validated_matches = tuple(decisions for decisions in candidate_matches if decisions)
    return validated_matches[0] if len(validated_matches) == 1 else ()


def match_release_tracks_to_album(
    source_tracks: Sequence[PlaylistTrack],
    album: SpotifyAlbumCandidate,
    spotify_tracks: Sequence[SpotifyAlbumTrack],
    album_search_query: str,
) -> tuple[TrackMatchDecision, ...]:
    source_tracks = tuple(source_tracks)
    spotify_tracks = tuple(spotify_tracks)
    exact_anchors = exact_track_anchors(source_tracks, spotify_tracks)
    if len(exact_anchors) < 2 or not anchors_are_monotonic(exact_anchors):
        return ()

    album_match_depends_on_alphanumeric_spacing = (
        music_values_match_only_after_alphanumeric_spacing(
            source_tracks[0].album_name,
            album.name,
        )
        or any(
            music_values_match_only_after_alphanumeric_spacing(
                source_tracks[source_index].track_name,
                spotify_tracks[spotify_index].name,
            )
            for source_index, spotify_index in exact_anchors
        )
    )
    exact_match_strategy = (
        ALBUM_ALPHANUMERIC_SPACING_MATCH_STRATEGY
        if album_match_depends_on_alphanumeric_spacing
        else ALBUM_EXACT_TRACK_MATCH_STRATEGY
    )
    exact_match_reason = (
        ALBUM_ALPHANUMERIC_SPACING_MATCH_REASON
        if album_match_depends_on_alphanumeric_spacing
        else ALBUM_EXACT_TRACK_MATCH_REASON
    )
    matched_indices: dict[int, tuple[int, str, str]] = {
        source_index: (
            spotify_index,
            exact_match_strategy,
            exact_match_reason,
        )
        for source_index, spotify_index in exact_anchors
    }
    boundaries = (
        ((-1, -1),)
        + exact_anchors
        + ((len(source_tracks), len(spotify_tracks)),)
    )
    for (left_source, left_spotify), (right_source, right_spotify) in zip(
        boundaries,
        boundaries[1:],
    ):
        source_gap = tuple(range(left_source + 1, right_source))
        spotify_gap = tuple(range(left_spotify + 1, right_spotify))
        if (
            len(source_gap) != len(spotify_gap)
            or len(source_gap) > MAXIMUM_POSITIONAL_GAP_TRACKS
        ):
            continue
        for source_index, spotify_index in zip(source_gap, spotify_gap):
            if positional_pair_is_safe(
                source_tracks[source_index],
                spotify_tracks[spotify_index],
            ):
                matched_indices[source_index] = (
                    spotify_index,
                    ALBUM_POSITION_MATCH_STRATEGY,
                    ALBUM_POSITION_MATCH_REASON,
                )

    return tuple(
        album_track_match_decision(
            source_track=source_tracks[source_index],
            spotify_track=spotify_tracks[spotify_index],
            album=album,
            album_search_query=album_search_query,
            match_strategy=match_strategy,
            reason=reason,
        )
        for source_index, (spotify_index, match_strategy, reason) in sorted(
            matched_indices.items()
        )
    )


def exact_track_anchors(
    source_tracks: Sequence[PlaylistTrack],
    spotify_tracks: Sequence[SpotifyAlbumTrack],
) -> tuple[tuple[int, int], ...]:
    potential_anchors: list[tuple[int, int]] = []
    for source_index, source_track in enumerate(source_tracks):
        matching_spotify_indices = tuple(
            spotify_index
            for spotify_index, spotify_track in enumerate(spotify_tracks)
            if spotify_album_track_is_usable(spotify_track)
            and normalized_titles_are_equal(source_track.track_name, spotify_track.name)
            and full_artist_sets_are_equal(source_track.artist_name, spotify_track.artists)
        )
        if len(matching_spotify_indices) == 1:
            potential_anchors.append((source_index, matching_spotify_indices[0]))

    spotify_index_counts = Counter(spotify_index for _, spotify_index in potential_anchors)
    return tuple(
        (source_index, spotify_index)
        for source_index, spotify_index in potential_anchors
        if spotify_index_counts[spotify_index] == 1
    )


def unique_title_anchors(
    source_tracks: Sequence[PlaylistTrack],
    spotify_tracks: Sequence[SpotifyAlbumTrack],
) -> tuple[tuple[int, int], ...]:
    potential_anchors: list[tuple[int, int]] = []
    for source_index, source_track in enumerate(source_tracks):
        matching_spotify_indices = tuple(
            spotify_index
            for spotify_index, spotify_track in enumerate(spotify_tracks)
            if spotify_album_track_is_usable(spotify_track)
            and normalized_titles_are_equal(source_track.track_name, spotify_track.name)
        )
        if len(matching_spotify_indices) == 1:
            potential_anchors.append((source_index, matching_spotify_indices[0]))

    spotify_index_counts = Counter(spotify_index for _, spotify_index in potential_anchors)
    return tuple(
        (source_index, spotify_index)
        for source_index, spotify_index in potential_anchors
        if spotify_index_counts[spotify_index] == 1
    )


def anchors_are_monotonic(anchors: Sequence[tuple[int, int]]) -> bool:
    return all(
        left_source < right_source and left_spotify < right_spotify
        for (left_source, left_spotify), (right_source, right_spotify) in zip(
            anchors,
            anchors[1:],
        )
    )


def positional_pair_is_safe(
    source_track: PlaylistTrack,
    spotify_track: SpotifyAlbumTrack,
) -> bool:
    return (
        spotify_album_track_is_usable(spotify_track)
        and full_artist_sets_are_equal(source_track.artist_name, spotify_track.artists)
    )


def spotify_album_track_is_usable(track: SpotifyAlbumTrack) -> bool:
    return bool(
        track.uri.startswith("spotify:track:")
        and track.name.strip()
        and track.is_playable is not False
    )


def normalized_titles_are_equal(source_title: str, spotify_title: str) -> bool:
    normalized_source_title = normalize_alphanumeric_spacing(source_title)
    return bool(
        normalized_source_title
        and normalized_source_title == normalize_alphanumeric_spacing(spotify_title)
    )


def full_artist_sets_are_equal(
    source_artist_name: str,
    spotify_artist_names: tuple[str, ...],
) -> bool:
    source_artists = normalized_source_artist_set(source_artist_name)
    spotify_artists = normalized_candidate_artist_set(spotify_artist_names)
    return bool(source_artists and source_artists == spotify_artists)


def album_track_match_decision(
    source_track: PlaylistTrack,
    spotify_track: SpotifyAlbumTrack,
    album: SpotifyAlbumCandidate,
    album_search_query: str,
    match_strategy: str,
    reason: str,
) -> TrackMatchDecision:
    candidate = SpotifyTrackCandidate(
        uri=spotify_track.uri,
        name=spotify_track.name,
        artists=spotify_track.artists,
        album_name=album.name,
        album_id=album.album_id,
    )
    return TrackMatchDecision(
        track=source_track,
        status=MATCHED,
        spotify_uri=spotify_track.uri,
        reason=reason,
        candidate=candidate,
        review_candidates=(candidate,),
        search_queries=(album_search_query,),
        match_strategy=match_strategy,
    )
