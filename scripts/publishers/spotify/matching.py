"""Domain logic for matching local playlist rows to Spotify tracks."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


MATCHED = "matched"
AMBIGUOUS = "ambiguous"
UNMATCHED = "unmatched"
ERROR = "error"
TRAILING_FEATURED_ARTIST_PATTERN = re.compile(
    r"\s+(?:feat|ft)\.\s+(?P<artist>[^()\[\]]+?)\s*$",
    re.IGNORECASE,
)
STANDALONE_ENCLOSED_FEATURED_ARTIST_PATTERN = re.compile(
    r"\s*[\(\[]\s*feat\.\s+(?P<artist>[^)\]]+?)\s*[\)\]]",
    re.IGNORECASE,
)
INLINE_ENCLOSED_FEATURED_ARTIST_PATTERN = re.compile(
    r"(?P<open>[\(\[])(?P<prefix>[^)\]]+?)\s+feat\.\s+(?P<artist>[^)\]]+?)\s*(?P<close>[\)\]])",
    re.IGNORECASE,
)
FEATURED_ARTIST_MATCH_REASON_PREFIX = "track matched after moving source featured credit to Spotify artists"
ORIGINAL_MIX_SUFFIX_PATTERN = re.compile(r"\s*\(\s*original\s+mix\s*\)\s*$", re.IGNORECASE)
ORIGINAL_MIX_MATCH_REASON_PREFIX = "track matched after removing source Original Mix annotation"
LEADING_IN_TITLE_MATCH_REASON_PREFIX = "track title differed only by Spotify's leading 'in'"
# Unicode categorizes these apostrophe-like characters as letters rather than punctuation.
LETTERLIKE_APOSTROPHE_CHARACTERS = frozenset(("\u02bb", "\u02bc"))


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

    if track.artist_name.strip() and track.track_name.strip():
        append_unique_search_query(queries, f"{track.artist_name} {track.track_name}")

    featured_credit = split_featured_artist_credit(track.track_name)
    if featured_credit:
        append_title_variant_search_queries(queries, track, featured_credit[0])

    original_mix_title = title_without_original_mix_suffix(track.track_name)
    append_title_variant_search_queries(queries, track, original_mix_title)

    append_unique_search_query(queries, track.spotify_search_query)
    return tuple(queries)


def append_unique_search_query(queries: list[str], query: str) -> None:
    clean_query = re.sub(r"\s+", " ", query).strip()
    if clean_query and clean_query not in queries:
        queries.append(clean_query)


def append_title_variant_search_queries(
    queries: list[str],
    track: PlaylistTrack,
    title_variant: str,
) -> None:
    clean_title_variant = quote_search_filter_value(title_variant)
    clean_source_title = quote_search_filter_value(track.track_name)
    clean_artist_name = quote_search_filter_value(track.artist_name)
    if not clean_title_variant or clean_title_variant == clean_source_title or not clean_artist_name:
        return

    clean_album_name = quote_search_filter_value(track.album_name)
    if clean_album_name:
        append_unique_search_query(
            queries,
            f'track:"{clean_title_variant}" artist:"{clean_artist_name}" album:"{clean_album_name}"',
        )
    append_unique_search_query(
        queries,
        f'track:"{clean_title_variant}" artist:"{clean_artist_name}"',
    )
    for artist_name in split_source_artist_names(track.artist_name):
        append_unique_search_query(
            queries,
            f'track:"{clean_title_variant}" artist:"{quote_search_filter_value(artist_name)}"',
        )
    append_unique_search_query(queries, f"{clean_artist_name} {clean_title_variant}")


def quote_search_filter_value(value: str) -> str:
    return re.sub(r'\s+', " ", value.replace('"', " ")).strip()


def split_featured_artist_credit(track_name: str) -> tuple[str, str] | None:
    clean_track_name = re.sub(r"\s+", " ", track_name).strip()
    trailing_match = TRAILING_FEATURED_ARTIST_PATTERN.search(clean_track_name)
    if trailing_match:
        return build_featured_artist_credit(
            clean_track_name[:trailing_match.start()],
            trailing_match.group("artist"),
        )

    standalone_match = STANDALONE_ENCLOSED_FEATURED_ARTIST_PATTERN.search(clean_track_name)
    if standalone_match:
        base_title = clean_track_name[:standalone_match.start()] + clean_track_name[standalone_match.end():]
        return build_featured_artist_credit(base_title, standalone_match.group("artist"))

    inline_match = INLINE_ENCLOSED_FEATURED_ARTIST_PATTERN.search(clean_track_name)
    if inline_match:
        base_title = "".join(
            (
                clean_track_name[:inline_match.start()],
                inline_match.group("open"),
                inline_match.group("prefix").strip(),
                inline_match.group("close"),
                clean_track_name[inline_match.end():],
            )
        )
        return build_featured_artist_credit(base_title, inline_match.group("artist"))
    return None


def build_featured_artist_credit(base_title: str, artist_name: str) -> tuple[str, str] | None:
    clean_base_title = quote_search_filter_value(base_title)
    clean_artist_name = quote_search_filter_value(artist_name)
    return (clean_base_title, clean_artist_name) if clean_base_title and clean_artist_name else None


def title_without_original_mix_suffix(track_name: str) -> str:
    clean_track_name = track_name.strip()
    base_title = ORIGINAL_MIX_SUFFIX_PATTERN.sub("", clean_track_name).strip()
    return base_title or clean_track_name


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
    allow_leading_in_title_variant: bool = True,
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

    original_mix_album_matches = tuple(
        candidate for candidate in candidates if candidate_matches_original_mix_track(track, candidate)
    )
    if len(original_mix_album_matches) == 1:
        return TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=original_mix_album_matches[0].uri,
            reason=f"{ORIGINAL_MIX_MATCH_REASON_PREFIX}; artist and album matched",
            candidate=original_mix_album_matches[0],
            review_candidates=(original_mix_album_matches[0],),
            search_queries=search_queries,
        )
    if len(original_mix_album_matches) > 1:
        return TrackMatchDecision(
            track=track,
            status=AMBIGUOUS,
            spotify_uri="",
            reason=(
                f"{len(original_mix_album_matches)} candidates matched after removing source Original Mix annotation, "
                "artist, and album"
            ),
            candidate=None,
            review_candidates=original_mix_album_matches,
            search_queries=search_queries,
        )

    original_mix_artist_matches = tuple(
        candidate for candidate in candidates if candidate_matches_original_mix_track_and_artist(track, candidate)
    )
    if len(original_mix_artist_matches) == 1:
        return TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=original_mix_artist_matches[0].uri,
            reason=f"{ORIGINAL_MIX_MATCH_REASON_PREFIX}; artist matched; album differed",
            candidate=original_mix_artist_matches[0],
            review_candidates=(original_mix_artist_matches[0],),
            search_queries=search_queries,
        )
    if len(original_mix_artist_matches) > 1:
        return TrackMatchDecision(
            track=track,
            status=AMBIGUOUS,
            spotify_uri="",
            reason=(
                f"{len(original_mix_artist_matches)} candidates matched after removing source Original Mix annotation "
                "and matching artist"
            ),
            candidate=None,
            review_candidates=original_mix_artist_matches,
            search_queries=search_queries,
        )

    featured_credit_album_matches = tuple(
        candidate for candidate in candidates if candidate_matches_relocated_featured_credit_track(track, candidate)
    )
    if len(featured_credit_album_matches) == 1:
        return TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=featured_credit_album_matches[0].uri,
            reason=f"{FEATURED_ARTIST_MATCH_REASON_PREFIX}; source artist, featured artist, and album matched",
            candidate=featured_credit_album_matches[0],
            review_candidates=(featured_credit_album_matches[0],),
            search_queries=search_queries,
        )
    if len(featured_credit_album_matches) > 1:
        return TrackMatchDecision(
            track=track,
            status=AMBIGUOUS,
            spotify_uri="",
            reason=(
                f"{len(featured_credit_album_matches)} candidates matched after moving source featured credit "
                "to Spotify artists and matching album"
            ),
            candidate=None,
            review_candidates=featured_credit_album_matches,
            search_queries=search_queries,
        )

    featured_credit_artist_matches = tuple(
        candidate
        for candidate in candidates
        if candidate_matches_relocated_featured_credit_track_and_artist(track, candidate)
    )
    if len(featured_credit_artist_matches) == 1:
        return TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=featured_credit_artist_matches[0].uri,
            reason=f"{FEATURED_ARTIST_MATCH_REASON_PREFIX}; source and featured artists matched; album differed",
            candidate=featured_credit_artist_matches[0],
            review_candidates=(featured_credit_artist_matches[0],),
            search_queries=search_queries,
        )
    if len(featured_credit_artist_matches) > 1:
        return TrackMatchDecision(
            track=track,
            status=AMBIGUOUS,
            spotify_uri="",
            reason=(
                f"{len(featured_credit_artist_matches)} candidates matched after moving source featured credit "
                "to Spotify artists and matching source artist"
            ),
            candidate=None,
            review_candidates=featured_credit_artist_matches,
            search_queries=search_queries,
        )

    leading_in_title_matches: tuple[SpotifyTrackCandidate, ...] = ()
    if allow_leading_in_title_variant:
        leading_in_title_matches = tuple(
            candidate
            for candidate in candidates
            if candidate_matches_leading_in_title_variant(track, candidate)
        )
    if len(leading_in_title_matches) == 1:
        return TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=leading_in_title_matches[0].uri,
            reason=f"{LEADING_IN_TITLE_MATCH_REASON_PREFIX}; artist and album matched",
            candidate=leading_in_title_matches[0],
            review_candidates=(leading_in_title_matches[0],),
            search_queries=search_queries,
        )
    if len(leading_in_title_matches) > 1:
        return TrackMatchDecision(
            track=track,
            status=AMBIGUOUS,
            spotify_uri="",
            reason=(
                f"{len(leading_in_title_matches)} candidates had Spotify's leading 'in' title variant "
                "and matched artist and album"
            ),
            candidate=None,
            review_candidates=leading_in_title_matches,
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


def candidate_matches_original_mix_track(track: PlaylistTrack, candidate: SpotifyTrackCandidate) -> bool:
    return (
        candidate_matches_original_mix_track_and_artist(track, candidate)
        and normalize_music_text(track.album_name) == normalize_music_text(candidate.album_name)
    )


def candidate_matches_original_mix_track_and_artist(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> bool:
    source_track_name = track.track_name.strip()
    base_track_name = title_without_original_mix_suffix(source_track_name)
    return (
        base_track_name != source_track_name
        and normalize_music_text(base_track_name) == normalize_music_text(candidate.name)
        and source_artist_matches_candidate(track.artist_name, candidate.artists)
    )


def candidate_matches_relocated_featured_credit_track(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> bool:
    return (
        candidate_matches_relocated_featured_credit_track_and_artist(track, candidate)
        and normalize_music_text(track.album_name) == normalize_music_text(candidate.album_name)
    )


def candidate_matches_relocated_featured_credit_track_and_artist(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> bool:
    featured_credit = split_featured_artist_credit(track.track_name)
    if not featured_credit:
        return False
    base_title, featured_artist_name = featured_credit
    return (
        normalize_music_text(base_title) == normalize_music_text(candidate.name)
        and source_artist_matches_candidate_without_featured_credit(
            track.artist_name,
            featured_artist_name,
            candidate.artists,
        )
        and featured_artist_credit_matches_candidate(featured_artist_name, candidate.artists)
    )


def candidate_matches_leading_in_title_variant(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> bool:
    source_album_key = normalize_music_text(track.album_name)
    return (
        spotify_title_has_leading_in_variant(track.track_name, candidate.name)
        and source_artist_matches_candidate(track.artist_name, candidate.artists)
        and bool(source_album_key)
        and source_album_key == normalize_music_text(candidate.album_name)
    )


def spotify_title_has_leading_in_variant(source_title: str, spotify_title: str) -> bool:
    source_title_tokens = tuple(normalize_music_text(source_title).split())
    spotify_title_tokens = tuple(normalize_music_text(spotify_title).split())
    return (
        len(source_title_tokens) >= 2
        and source_title_tokens[0] != "in"
        and spotify_title_tokens == ("in", *source_title_tokens)
    )


def source_artist_matches_candidate(artist_name: str, candidate_artists: tuple[str, ...]) -> bool:
    return any(
        normalized_artist_in_candidates(source_artist_name, candidate_artists)
        for source_artist_name in source_artist_match_names(artist_name)
    )


def source_artist_matches_candidate_without_featured_credit(
    source_artist_name: str,
    featured_artist_name: str,
    candidate_artists: tuple[str, ...],
) -> bool:
    featured_artist_keys = {
        normalize_music_text(artist_name)
        for artist_name in featured_artist_match_names(featured_artist_name)
    }
    non_featured_source_names = tuple(
        artist_name
        for artist_name in source_artist_match_names(source_artist_name)
        if normalize_music_text(artist_name) not in featured_artist_keys
    )
    if not non_featured_source_names:
        return False
    return any(
        normalized_artist_in_candidates(artist_name, candidate_artists)
        for artist_name in non_featured_source_names
    )


def featured_artist_credit_matches_candidate(
    featured_artist_name: str,
    candidate_artists: tuple[str, ...],
) -> bool:
    if normalized_artist_in_candidates(featured_artist_name, candidate_artists):
        return True
    split_names = split_featured_artist_names(featured_artist_name)
    return bool(split_names) and all(
        normalized_artist_in_candidates(artist_name, candidate_artists)
        for artist_name in split_names
    )


def featured_artist_match_names(featured_artist_name: str) -> tuple[str, ...]:
    clean_artist_name = quote_search_filter_value(featured_artist_name)
    return tuple(dict.fromkeys((clean_artist_name, *split_featured_artist_names(clean_artist_name))))


def split_featured_artist_names(featured_artist_name: str) -> tuple[str, ...]:
    parts = tuple(part.strip() for part in re.split(r"\s+&\s+", featured_artist_name) if part.strip())
    return parts if len(parts) >= 2 else ()


def source_artist_match_names(artist_name: str) -> tuple[str, ...]:
    clean_artist_name = quote_search_filter_value(artist_name)
    return tuple(dict.fromkeys((clean_artist_name, *split_source_artist_names(clean_artist_name)))) if clean_artist_name else ()


def normalized_artist_in_candidates(artist_name: str, candidate_artists: tuple[str, ...]) -> bool:
    artist_key = normalize_music_text(artist_name)
    return bool(artist_key) and artist_key in {normalize_music_text(artist) for artist in candidate_artists}


def normalize_music_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    punctuation_separated = "".join(
        " "
        if character in LETTERLIKE_APOSTROPHE_CHARACTERS
        or unicodedata.category(character).startswith(("P", "S"))
        else character
        for character in normalized
    )
    ascii_text = punctuation_separated.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^a-zA-Z0-9]+", " ", ascii_text.casefold())
    return re.sub(r"\s+", " ", ascii_text).strip()
