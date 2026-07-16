"""Validated Spotify album matching for Discogs release track groups."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from publishers.spotify.matching import (
    MATCHED,
    PlaylistTrack,
    SpotifyTrackCandidate,
    TrackMatchDecision,
    normalize_music_text,
    normalized_candidate_artist_set,
    normalized_source_artist_set,
    quote_search_filter_value,
)


MINIMUM_RELEASE_TRACKS_FOR_ALBUM_LOOKUP = 3
MAXIMUM_EXACT_ALBUM_CANDIDATES_TO_FETCH = 3
MAXIMUM_POSITIONAL_GAP_TRACKS = 3
ALBUM_EXACT_TRACK_MATCH_STRATEGY = "exact_track_in_validated_album"
ALBUM_POSITION_MATCH_STRATEGY = "validated_album_position"
ALBUM_EXACT_TRACK_MATCH_REASON = "track matched exactly within a validated Spotify album"
ALBUM_POSITION_MATCH_REASON = "track matched by position within a validated Spotify album sequence"


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
class SpotifyReleaseAlbumMatchResult:
    decisions: tuple[TrackMatchDecision, ...]
    search_count: int


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
) -> SpotifyReleaseAlbumMatchResult:
    source_tracks = tuple(source_tracks)
    if not release_is_eligible_for_album_lookup(source_tracks):
        return SpotifyReleaseAlbumMatchResult(decisions=(), search_count=0)

    album_search_query = build_spotify_album_search_query(source_tracks[0].album_name)
    candidates = spotify_client.search_albums(
        access_token=access_token,
        query=album_search_query,
        limit=search_limit,
    )
    normalized_source_album_name = normalize_music_text(source_tracks[0].album_name)
    exact_album_candidates_by_id: dict[str, SpotifyAlbumCandidate] = {}
    for candidate in candidates:
        if (
            candidate.album_id
            and normalize_music_text(candidate.name) == normalized_source_album_name
        ):
            exact_album_candidates_by_id.setdefault(candidate.album_id, candidate)
    exact_album_candidates = tuple(exact_album_candidates_by_id.values())
    if (
        not exact_album_candidates
        or len(exact_album_candidates) > MAXIMUM_EXACT_ALBUM_CANDIDATES_TO_FETCH
    ):
        return SpotifyReleaseAlbumMatchResult(decisions=(), search_count=1)

    candidate_matches: list[tuple[TrackMatchDecision, ...]] = []
    for album in exact_album_candidates:
        spotify_tracks = spotify_client.get_album_tracks(
            access_token=access_token,
            album_id=album.album_id,
        )
        candidate_matches.append(
            match_release_tracks_to_album(
                source_tracks=source_tracks,
                album=album,
                spotify_tracks=spotify_tracks,
                album_search_query=album_search_query,
            )
        )
    return SpotifyReleaseAlbumMatchResult(
        decisions=choose_validated_album_match(tuple(candidate_matches)),
        search_count=1,
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

    matched_indices: dict[int, tuple[int, str, str]] = {
        source_index: (
            spotify_index,
            ALBUM_EXACT_TRACK_MATCH_STRATEGY,
            ALBUM_EXACT_TRACK_MATCH_REASON,
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
    normalized_source_title = normalize_music_text(source_title)
    return bool(
        normalized_source_title
        and normalized_source_title == normalize_music_text(spotify_title)
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
