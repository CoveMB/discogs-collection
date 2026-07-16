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
SPOTIFY_FEATURED_ARTIST_MATCH_REASON_PREFIX = (
    "track matched after removing Spotify featured credit from track title"
)
ORIGINAL_MIX_SUFFIX_PATTERN = re.compile(r"\s*\(\s*original\s+mix\s*\)\s*$", re.IGNORECASE)
ORIGINAL_MIX_MATCH_REASON_PREFIX = "track matched after removing source Original Mix annotation"
LEADING_IN_TITLE_MATCH_REASON_PREFIX = "track title differed only by Spotify's leading 'in'"
CONSTRAINED_TYPO_MATCH_REASON_PREFIX = "track title matched by constrained typo fallback"
PROTECTED_TITLE_TOKENS = frozenset(
    (
        "acoustic",
        "dub",
        "edit",
        "extended",
        "instrumental",
        "live",
        "mix",
        "original",
        "radio",
        "remaster",
        "remastered",
        "remix",
        "version",
    )
)
MINIMUM_TYPO_TOKEN_LENGTH = 5
MAXIMUM_CONSTRAINED_TYPO_DISTANCE = 3
MINIMUM_LONG_TITLE_LENGTH = 20
MAXIMUM_LONG_TITLE_EDIT_PERCENTAGE = 10
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
    album_id: str = ""


@dataclass(frozen=True)
class TrackMatchDecision:
    track: PlaylistTrack
    status: str
    spotify_uri: str
    reason: str
    candidate: SpotifyTrackCandidate | None = None
    review_candidates: tuple[SpotifyTrackCandidate, ...] = ()
    search_queries: tuple[str, ...] = ()
    match_strategy: str = ""


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
    allow_constrained_typo_fallback: bool = True,
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

    featured_credit_reason_prefix = one_sided_featured_credit_match_reason_prefix(track)
    featured_credit_reason_tail = featured_credit_reason_prefix.removeprefix("track matched ")
    featured_credit_album_matches = tuple(
        candidate for candidate in candidates if candidate_matches_one_sided_featured_credit_track(track, candidate)
    )
    if len(featured_credit_album_matches) == 1:
        return TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=featured_credit_album_matches[0].uri,
            reason=f"{featured_credit_reason_prefix}; source artist, featured artist, and album matched",
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
                f"{len(featured_credit_album_matches)} candidates matched {featured_credit_reason_tail} "
                "and matching album"
            ),
            candidate=None,
            review_candidates=featured_credit_album_matches,
            search_queries=search_queries,
        )

    featured_credit_artist_matches = tuple(
        candidate
        for candidate in candidates
        if candidate_matches_one_sided_featured_credit_track_and_artist(track, candidate)
    )
    if len(featured_credit_artist_matches) == 1:
        return TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=featured_credit_artist_matches[0].uri,
            reason=f"{featured_credit_reason_prefix}; source and featured artists matched; album differed",
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
                f"{len(featured_credit_artist_matches)} candidates matched {featured_credit_reason_tail} "
                "and matching source artist"
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

    constrained_typo_matches: tuple[tuple[SpotifyTrackCandidate, int], ...] = ()
    if allow_constrained_typo_fallback:
        constrained_typo_matches = tuple(
            (candidate, distance)
            for candidate in candidates
            if (distance := constrained_title_typo_distance(track, candidate)) is not None
        )
    if len(constrained_typo_matches) == 1:
        candidate, distance = constrained_typo_matches[0]
        return TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=candidate.uri,
            reason=(
                f"{CONSTRAINED_TYPO_MATCH_REASON_PREFIX} (distance {distance}); "
                "artist set and album matched"
            ),
            candidate=candidate,
            review_candidates=(candidate,),
            search_queries=search_queries,
        )
    if len(constrained_typo_matches) > 1:
        typo_candidates = tuple(candidate for candidate, _distance in constrained_typo_matches)
        return TrackMatchDecision(
            track=track,
            status=AMBIGUOUS,
            spotify_uri="",
            reason=f"{len(typo_candidates)} candidates matched by constrained typo fallback",
            candidate=None,
            review_candidates=typo_candidates,
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


def candidate_matches_one_sided_featured_credit_track(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> bool:
    return (
        candidate_matches_one_sided_featured_credit_track_and_artist(track, candidate)
        and normalize_music_text(track.album_name) == normalize_music_text(candidate.album_name)
    )


def candidate_matches_one_sided_featured_credit_track_and_artist(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> bool:
    source_featured_credit = split_featured_artist_credit(track.track_name)
    spotify_featured_credit = split_featured_artist_credit(candidate.name)
    if source_featured_credit is not None:
        if spotify_featured_credit is not None:
            return False
        base_title, featured_artist_name = source_featured_credit
        compared_title = candidate.name
    elif spotify_featured_credit is not None:
        base_title, featured_artist_name = spotify_featured_credit
        compared_title = track.track_name
    else:
        return False
    return (
        normalize_music_text(base_title) == normalize_music_text(compared_title)
        and source_artist_matches_candidate_without_featured_credit(
            track.artist_name,
            featured_artist_name,
            candidate.artists,
        )
        and featured_artist_credit_matches_candidate(featured_artist_name, candidate.artists)
    )


def one_sided_featured_credit_match_reason_prefix(track: PlaylistTrack) -> str:
    if split_featured_artist_credit(track.track_name) is not None:
        return FEATURED_ARTIST_MATCH_REASON_PREFIX
    return SPOTIFY_FEATURED_ARTIST_MATCH_REASON_PREFIX


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


def constrained_title_typo_distance(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> int | None:
    source_album_key = normalize_music_text(track.album_name)
    if not source_album_key or source_album_key != normalize_music_text(candidate.album_name):
        return None
    source_artist_set = normalized_source_artist_set(track.artist_name)
    if (
        not source_artist_set
        or source_artist_set != normalized_candidate_artist_set(candidate.artists)
    ):
        return None
    return tightly_constrained_title_distance(track.track_name, candidate.name)


def tightly_constrained_title_distance(source_title: str, candidate_title: str) -> int | None:
    source_key = normalize_music_text(source_title)
    candidate_key = normalize_music_text(candidate_title)
    if not source_key or not candidate_key or source_key == candidate_key:
        return None

    source_tokens = tuple(source_key.split())
    candidate_tokens = tuple(candidate_key.split())
    if len(source_tokens) != len(candidate_tokens):
        return None
    differing_tokens = tuple(
        (source_token, candidate_token)
        for source_token, candidate_token in zip(source_tokens, candidate_tokens, strict=True)
        if source_token != candidate_token
    )
    if len(differing_tokens) != 1:
        return None

    source_token, candidate_token = differing_tokens[0]
    if min(len(source_token), len(candidate_token)) < MINIMUM_TYPO_TOKEN_LENGTH:
        return None
    if source_token in PROTECTED_TITLE_TOKENS or candidate_token in PROTECTED_TITLE_TOKENS:
        return None

    distance = bounded_damerau_levenshtein_distance(
        source_key,
        candidate_key,
        maximum_distance=MAXIMUM_CONSTRAINED_TYPO_DISTANCE,
    )
    if distance == 1:
        return distance

    maximum_title_length = max(len(source_key), len(candidate_key))
    if (
        maximum_title_length >= MINIMUM_LONG_TITLE_LENGTH
        and distance <= MAXIMUM_CONSTRAINED_TYPO_DISTANCE
        and distance * 100 <= maximum_title_length * MAXIMUM_LONG_TITLE_EDIT_PERCENTAGE
    ):
        return distance
    return None


def bounded_damerau_levenshtein_distance(
    left: str,
    right: str,
    maximum_distance: int,
) -> int:
    if left == right:
        return 0
    if abs(len(left) - len(right)) > maximum_distance:
        return maximum_distance + 1

    previous_previous_row: list[int] | None = None
    previous_row = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current_row = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            deletion_cost = previous_row[right_index] + 1
            insertion_cost = current_row[right_index - 1] + 1
            substitution_cost = previous_row[right_index - 1] + (left_character != right_character)
            distance = min(deletion_cost, insertion_cost, substitution_cost)
            if (
                previous_previous_row is not None
                and left_index > 1
                and right_index > 1
                and left_character == right[right_index - 2]
                and left[left_index - 2] == right_character
            ):
                distance = min(distance, previous_previous_row[right_index - 2] + 1)
            current_row.append(distance)
        previous_previous_row, previous_row = previous_row, current_row
    return previous_row[-1]


def normalized_source_artist_set(artist_name: str) -> frozenset[str]:
    clean_artist_name = quote_search_filter_value(artist_name)
    if not clean_artist_name:
        return frozenset()
    split_names = split_source_artist_names(clean_artist_name)
    artist_names = split_names or (clean_artist_name,)
    return frozenset(
        artist_key
        for value in artist_names
        if (artist_key := normalize_music_text(value))
    )


def normalized_candidate_artist_set(candidate_artists: tuple[str, ...]) -> frozenset[str]:
    return frozenset(
        artist_key
        for artist_name in candidate_artists
        if (artist_key := normalize_music_text(artist_name))
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
