"""Domain logic for matching local playlist rows to Spotify tracks."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


MATCHED = "matched"
AMBIGUOUS = "ambiguous"
UNMATCHED = "unmatched"
ERROR = "error"


@dataclass(frozen=True)
class PlaylistTrack:
    playlist_name: str
    release_id: str
    album_name: str
    track_number: str
    track_name: str
    artist_name: str
    spotify_search_query: str


@dataclass(frozen=True)
class SpotifyTrackCandidate:
    uri: str
    name: str
    artists: tuple[str, ...]
    album_name: str


@dataclass(frozen=True)
class TrackMatchDecision:
    track: PlaylistTrack
    status: str
    spotify_uri: str
    reason: str
    candidate: SpotifyTrackCandidate | None = None
    review_candidates: tuple[SpotifyTrackCandidate, ...] = ()
    search_queries: tuple[str, ...] = ()


def build_spotify_track_search_query(track: PlaylistTrack) -> str:
    filters = []
    if track.track_name.strip():
        filters.append(f'track:"{quote_search_filter_value(track.track_name)}"')
    if track.artist_name.strip():
        filters.append(f'artist:"{quote_search_filter_value(track.artist_name)}"')
    if track.album_name.strip():
        filters.append(f'album:"{quote_search_filter_value(track.album_name)}"')
    return " ".join(filters) if filters else track.spotify_search_query.strip()


def build_spotify_track_search_queries(track: PlaylistTrack) -> tuple[str, ...]:
    queries: list[str] = []
    strict_query = build_spotify_track_search_query(track)
    append_unique_search_query(queries, strict_query)

    if track.track_name.strip() and track.artist_name.strip():
        title_artist_query = " ".join(
            (
                f'track:"{quote_search_filter_value(track.track_name)}"',
                f'artist:"{quote_search_filter_value(track.artist_name)}"',
            )
        )
        append_unique_search_query(queries, title_artist_query)

    for artist_name in split_source_artist_names(track.artist_name):
        split_artist_query = " ".join(
            (
                f'track:"{quote_search_filter_value(track.track_name)}"',
                f'artist:"{quote_search_filter_value(artist_name)}"',
            )
        )
        append_unique_search_query(queries, split_artist_query)

    append_unique_search_query(queries, track.spotify_search_query)
    return tuple(queries)


def append_unique_search_query(queries: list[str], query: str) -> None:
    clean_query = re.sub(r"\s+", " ", query).strip()
    if clean_query and clean_query not in queries:
        queries.append(clean_query)


def quote_search_filter_value(value: str) -> str:
    return re.sub(r'\s+', " ", value.replace('"', " ")).strip()


def split_source_artist_names(artist_name: str) -> tuple[str, ...]:
    parts = tuple(part.strip() for part in artist_name.split(",") if part.strip())
    if len(parts) < 2:
        return ()
    # Avoid splitting band names like "Earth, Wind & Fire" after Discogs data
    # has already been flattened to one string.
    if any(" & " in part or " and " in part.casefold() for part in parts):
        return ()
    return parts


def choose_best_track_match(
    track: PlaylistTrack,
    candidates: tuple[SpotifyTrackCandidate, ...],
    search_queries: tuple[str, ...] = (),
) -> TrackMatchDecision:
    candidates = deduplicate_spotify_track_candidates(candidates)
    exact_matches = tuple(candidate for candidate in candidates if candidate_matches_track(track, candidate))
    if len(exact_matches) == 1:
        return TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=exact_matches[0].uri,
            reason="track, artist, and album matched",
            candidate=exact_matches[0],
            review_candidates=(exact_matches[0],),
            search_queries=search_queries,
        )
    if len(exact_matches) > 1:
        return TrackMatchDecision(
            track=track,
            status=AMBIGUOUS,
            spotify_uri="",
            reason=f"{len(exact_matches)} candidates matched track, artist, and album",
            candidate=None,
            review_candidates=exact_matches,
            search_queries=search_queries,
        )

    title_artist_matches = tuple(candidate for candidate in candidates if candidate_matches_track_and_artist(track, candidate))
    if len(title_artist_matches) == 1:
        return TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=title_artist_matches[0].uri,
            reason="track and artist matched; album differed",
            candidate=title_artist_matches[0],
            review_candidates=(title_artist_matches[0],),
            search_queries=search_queries,
        )
    if len(title_artist_matches) > 1:
        return TrackMatchDecision(
            track=track,
            status=AMBIGUOUS,
            spotify_uri="",
            reason=f"{len(title_artist_matches)} candidates matched track and artist",
            candidate=None,
            review_candidates=title_artist_matches,
            search_queries=search_queries,
        )
    return TrackMatchDecision(
        track=track,
        status=UNMATCHED,
        spotify_uri="",
        reason="no candidates matched track, artist, and album",
        candidate=None,
        review_candidates=candidates,
        search_queries=search_queries,
    )


def deduplicate_spotify_track_candidates(
    candidates: tuple[SpotifyTrackCandidate, ...],
) -> tuple[SpotifyTrackCandidate, ...]:
    candidates_by_uri: dict[str, SpotifyTrackCandidate] = {}
    for candidate in candidates:
        candidates_by_uri.setdefault(candidate.uri, candidate)
    return tuple(candidates_by_uri.values())


def track_match_error(track: PlaylistTrack, reason: str, search_queries: tuple[str, ...] = ()) -> TrackMatchDecision:
    return TrackMatchDecision(
        track=track,
        status=ERROR,
        spotify_uri="",
        reason=reason,
        candidate=None,
        search_queries=search_queries,
    )


def candidate_matches_track(track: PlaylistTrack, candidate: SpotifyTrackCandidate) -> bool:
    return (
        candidate_matches_track_and_artist(track, candidate)
        and normalize_music_text(track.album_name) == normalize_music_text(candidate.album_name)
    )


def candidate_matches_track_and_artist(track: PlaylistTrack, candidate: SpotifyTrackCandidate) -> bool:
    return (
        normalize_music_text(track.track_name) == normalize_music_text(candidate.name)
        and source_artist_matches_candidate(track.artist_name, candidate.artists)
    )


def source_artist_matches_candidate(artist_name: str, candidate_artists: tuple[str, ...]) -> bool:
    return any(
        normalized_artist_in_candidates(source_artist_name, candidate_artists)
        for source_artist_name in source_artist_match_names(artist_name)
    )


def source_artist_match_names(artist_name: str) -> tuple[str, ...]:
    clean_artist_name = quote_search_filter_value(artist_name)
    return tuple(dict.fromkeys((clean_artist_name, *split_source_artist_names(clean_artist_name)))) if clean_artist_name else ()


def normalized_artist_in_candidates(artist_name: str, candidate_artists: tuple[str, ...]) -> bool:
    artist_key = normalize_music_text(artist_name)
    return bool(artist_key) and artist_key in {normalize_music_text(artist) for artist in candidate_artists}


def normalize_music_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^a-zA-Z0-9]+", " ", ascii_text.casefold())
    return re.sub(r"\s+", " ", ascii_text).strip()
