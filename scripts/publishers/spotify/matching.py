"""Domain logic for matching local playlist rows to Spotify tracks."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from publishers.spotify.spelling import (
    conjunction_difference,
    normalize_recording_evidence_text as normalize_recording_evidence_text,
    spelling_difference,
)

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
SPOTIFY_ORIGINAL_ANNOTATION_MATCH_REASON = (
    "track matched after removing Spotify Original annotation; artist set and album matched"
)
SPOTIFY_ORIGINAL_ANNOTATION_MATCH_STRATEGY = "spotify_original_annotation"
TRAILING_DASHED_SPOTIFY_ORIGINAL_PATTERN = re.compile(
    r"\s*[-\u2013\u2014]\s*(?:original(?:\s+mix)?|orginal\s+mix)\s*$",
    re.IGNORECASE,
)
TRAILING_ENCLOSED_SPOTIFY_ORIGINAL_PATTERN = re.compile(
    r"\s*[\(\[]\s*(?:original(?:\s+mix)?|orginal\s+mix)\s*[\)\]]\s*$",
    re.IGNORECASE,
)
LEADING_IN_TITLE_MATCH_REASON_PREFIX = "track title differed only by Spotify's leading 'in'"
CONSTRAINED_TYPO_MATCH_REASON_PREFIX = "track title matched by constrained typo fallback"
ALPHANUMERIC_SPACING_MATCH_REASON = "track and album matched after normalizing letter-number spacing"
ALPHANUMERIC_SPACING_MATCH_STRATEGY = "alphanumeric_spacing"
VERSION_SUBSTITUTE_MATCH_STRATEGY = "version_substitute"
SAME_RECORDING_EDITION_MATCH_STRATEGY = "same_recording_edition"
MANUAL_URI_ONLY_MATCH_STRATEGY = "manual_uri_only"
REPEATED_PERFORMER_CREDIT_STRATEGY = "repeated_performer_credit"
TRAILING_PERFORMER_CREDIT = re.compile(r"\s*\(\s*with\s+([^()[\]]+?)\s*\)$")
TRAILING_FEATURING_CREDIT = re.compile(r"\s+featuring\s+([^()[\]]+?)$")
COMPOSER_PREFIX_STRATEGY = "credited_composer_prefix"
TITLE_CONJUNCTION_STRATEGY = "title_conjunction"
CATALOGUE_SPELLING_STRATEGY = "catalogue_spelling"
EXACT_TITLE_RECORDING_STRATEGY = "exact_title_recording"
CATALOGUE_RECORDING_STRATEGIES = frozenset((
    COMPOSER_PREFIX_STRATEGY, TITLE_CONJUNCTION_STRATEGY, CATALOGUE_SPELLING_STRATEGY, EXACT_TITLE_RECORDING_STRATEGY,
))
TRAILING_ENCLOSED_VERSION_PATTERN = re.compile(
    r"\s*[\(\[]\s*(?P<label>remaster(?:ed)?|live|(?:radio\s+)?edit)\s*[\)\]]\s*$",
    re.IGNORECASE,
)
TRAILING_DASHED_VERSION_PATTERN = re.compile(
    r"\s*[-\u2013\u2014]\s*(?P<label>remaster(?:ed)?|live|(?:radio\s+)?edit)\s*$",
    re.IGNORECASE,
)
DASHED_VERSION_REVIEW_LABEL_PATTERN = re.compile(r"^(.*)\s[-\u2013\u2014]\s([^()\[\]]+)$", re.DOTALL)
VERSION_REVIEW_TERM_PATTERN = re.compile(
    r"\b(?:acoustic|club|demo|dub|edit|extended|instrumental|live|mix|original|radio|"
    r"remaster|remastered|remix|rework|version|vocal)\b"
)
SPOTIFY_TRACK_SEARCH_QUERY_CHARACTER_LIMIT = 250
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
class SpotifyRecordingMetadata:
    title: str
    artist_names: tuple[str, ...]
    album_name: str
    album_id: str

    def conflicts_with(self, other: SpotifyRecordingMetadata) -> bool:
        return any(
            left and right and left != right
            for left, right in (
                (self.title, other.title),
                (self.artist_names, other.artist_names),
                (self.album_name, other.album_name),
                (self.album_id, other.album_id),
            )
        )


@dataclass(frozen=True)
class SpotifyTrackCandidate:
    uri: str
    name: str
    artists: tuple[str, ...]
    album_name: str
    album_id: str = ""
    isrc: str = ""
    is_playable: bool | None = None
    has_complete_recording_metadata: bool = field(default=True, compare=False)
    observed_recording_metadata: SpotifyRecordingMetadata | None = field(default=None, compare=False)
    version_title_review_only: bool = False
    title_only_search: bool = False

    def recording_metadata(self) -> SpotifyRecordingMetadata:
        if self.observed_recording_metadata is not None:
            return self.observed_recording_metadata
        return spotify_recording_metadata_from_values(
            title=self.name, artist_names=self.artists, album_name=self.album_name, album_id=self.album_id,
        )


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
    strict_album_recovery: bool = False
    title_search_complete: bool = False
    composer_coverage: str = ""


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

    version_substitute = split_version_substitute_title(track.track_name)
    if version_substitute is not None:
        append_title_variant_search_queries(queries, track, version_substitute[0])

    append_unique_search_query(queries, track.spotify_search_query)
    return tuple(queries)


def append_unique_search_query(queries: list[str], query: str) -> None:
    clean_query = _normalize_search_query(query)
    if clean_query and clean_query not in queries:
        queries.append(clean_query)


def _normalize_search_query(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip()


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


def title_without_spotify_original_annotation(track_name: str) -> str:
    clean_track_name = track_name.strip()
    for pattern in (
        TRAILING_DASHED_SPOTIFY_ORIGINAL_PATTERN,
        TRAILING_ENCLOSED_SPOTIFY_ORIGINAL_PATTERN,
    ):
        base_title = pattern.sub("", clean_track_name).strip()
        if base_title != clean_track_name:
            return base_title or clean_track_name
    return clean_track_name


def split_version_substitute_title(track_name: str) -> tuple[str, str] | None:
    clean_track_name = track_name.strip()
    for pattern in (TRAILING_ENCLOSED_VERSION_PATTERN, TRAILING_DASHED_VERSION_PATTERN):
        match = pattern.search(clean_track_name)
        if match is None:
            continue
        base_title = clean_track_name[:match.start()].strip()
        if not base_title:
            return None
        return base_title, normalize_version_substitute_label(match.group("label"))
    return None


def normalize_version_substitute_label(label: str) -> str:
    normalized_label = re.sub(r"\s+", " ", label.casefold()).strip()
    if "remaster" in normalized_label:
        return "remastered"
    if normalized_label.endswith("edit"):
        return "edit"
    return "live"


def split_source_artist_names(artist_name: str) -> tuple[str, ...]:
    parts = tuple(part.strip() for part in artist_name.split(",") if part.strip())
    if len(parts) < 2:
        return ()
    # Avoid splitting band names like "Earth, Wind & Fire" after Discogs data
    # has already been flattened to one string.
    if any(" & " in part or " and " in part.casefold() for part in parts):
        return ()
    return parts


def _version_review_base_title(title: str) -> str | None:
    clean_title = title.strip()
    if clean_title.endswith((")", "]")):
        expected_openers: list[str] = []
        for index in range(len(clean_title) - 1, -1, -1):
            delimiter = clean_title[index]
            if delimiter in (")", "]"):
                expected_openers.append("(" if delimiter == ")" else "[")
            elif delimiter in ("(", "["):
                if expected_openers.pop() != delimiter:
                    return None
                if not expected_openers:
                    base_title, label = clean_title[:index].strip(), clean_title[index + 1:-1]
                    break
        else:
            return None
    else:
        dashed = DASHED_VERSION_REVIEW_LABEL_PATTERN.fullmatch(clean_title)
        if not dashed:
            return None
        base_title, label = dashed.group(1).strip(), dashed.group(2)
    if base_title and VERSION_REVIEW_TERM_PATTERN.search(normalize_recording_evidence_text(label)):
        return base_title
    return None


def _candidate_matches_version_review(
    base_title: str, artist_name: str, candidate: SpotifyTrackCandidate,
) -> bool:
    normalized_artist_name = normalize_recording_evidence_text(artist_name)
    source_artists = set(split_source_artist_names(normalized_artist_name) or (normalized_artist_name,))
    candidate_metadata = candidate.recording_metadata()
    candidate_base_title = _version_review_base_title(candidate_metadata.title) or candidate_metadata.title
    return (
        "" not in source_artists
        and source_artists == set(candidate_metadata.artist_names)
        and normalize_recording_evidence_text(base_title) == normalize_recording_evidence_text(candidate_base_title)
    )


def _prepare_version_title_review_query(decision: TrackMatchDecision) -> tuple[str, str] | None:
    if decision.status != UNMATCHED:
        return None
    track = decision.track
    base_title = _version_review_base_title(track.track_name)
    if base_title is None:
        return None
    quoted_title = quote_search_filter_value(base_title)
    quoted_artist = quote_search_filter_value(track.artist_name)
    if not quoted_title or not quoted_artist:
        return None
    query = f'track:"{quoted_title}" artist:"{quoted_artist}"'
    if len(query) > SPOTIFY_TRACK_SEARCH_QUERY_CHARACTER_LIMIT:
        return None
    for candidate in decision.review_candidates:
        if not candidate.version_title_review_only and _candidate_matches_version_review(base_title, track.artist_name, candidate):
            return base_title, query
    return None


def build_base_title_version_review_query(decision: TrackMatchDecision) -> str | None:
    prepared = _prepare_version_title_review_query(decision)
    if prepared is None:
        return None
    _, query = prepared
    if any(query == _normalize_search_query(previous_query) for previous_query in decision.search_queries):
        return None
    return query


def with_version_title_review_candidates(
    decision: TrackMatchDecision, candidates: tuple[SpotifyTrackCandidate, ...],
) -> TrackMatchDecision:
    if not candidates:
        return decision
    prepared = _prepare_version_title_review_query(decision)
    if prepared is None:
        return decision
    base_title, _ = prepared
    known_uris = {candidate.uri for candidate in decision.review_candidates}
    new_candidates = deduplicate_spotify_track_candidates(tuple(
        replace(candidate, version_title_review_only=True)
        for candidate in candidates
        if candidate.uri not in known_uris
        and spotify_track_uri_is_structurally_valid(candidate.uri)
        and _candidate_matches_version_review(base_title, decision.track.artist_name, candidate)
    ))
    return replace(decision, review_candidates=(*decision.review_candidates, *new_candidates))


def complete_version_title_review(
    decision: TrackMatchDecision, query: str, candidates: tuple[SpotifyTrackCandidate, ...],
) -> TrackMatchDecision:
    return replace(
        with_version_title_review_candidates(decision, candidates),
        search_queries=(*decision.search_queries, query),
    )


def _decision_from_candidates(
    track: PlaylistTrack,
    candidates: tuple[SpotifyTrackCandidate, ...],
    search_queries: tuple[str, ...],
    *,
    matched_reason: str,
    ambiguous_reason: str,
    match_strategy: str = "",
) -> TrackMatchDecision:
    candidate = candidates[0]
    if len(candidates) == 1:
        return TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri=candidate.uri,
            reason=matched_reason,
            candidate=candidate,
            review_candidates=(candidate,),
            search_queries=search_queries,
            match_strategy=match_strategy,
        )
    return TrackMatchDecision(
        track=track,
        status=AMBIGUOUS,
        spotify_uri="",
        reason=ambiguous_reason,
        candidate=None,
        review_candidates=candidates,
        search_queries=search_queries,
    )


def choose_best_track_match(
    track: PlaylistTrack,
    candidates: tuple[SpotifyTrackCandidate, ...],
    search_queries: tuple[str, ...] = (),
    allow_leading_in_title_variant: bool = True,
    allow_alphanumeric_spacing_fallback: bool = True,
    allow_constrained_typo_fallback: bool = True,
    allow_version_substitute: bool = True,
    *,
    repeated_credit_observations: Sequence[SpotifyTrackCandidate] | None = None,
    allow_catalogue_recovery: bool = True,
    aligned_album_positions: Sequence[SpotifyTrackCandidate] = (),
) -> TrackMatchDecision:
    if repeated_credit_observations is None:
        repeated_credit_observations = candidates
    candidates = deduplicate_spotify_track_candidates(candidates)
    exact_matches = tuple(candidate for candidate in candidates if candidate_matches_track(track, candidate))
    if exact_matches:
        return _decision_from_candidates(
            track,
            exact_matches,
            search_queries,
            matched_reason="track, artist, and album matched",
            ambiguous_reason=f"{len(exact_matches)} candidates matched track, artist, and album",
        )

    title_artist_matches = tuple(candidate for candidate in candidates if candidate_matches_track_and_artist(track, candidate))
    if title_artist_matches:
        return _decision_from_candidates(
            track,
            title_artist_matches,
            search_queries,
            matched_reason="track and artist matched; album differed",
            ambiguous_reason=f"{len(title_artist_matches)} candidates matched track and artist",
        )

    original_mix_album_matches = tuple(
        candidate for candidate in candidates if candidate_matches_original_mix_track(track, candidate)
    )
    if original_mix_album_matches:
        return _decision_from_candidates(
            track,
            original_mix_album_matches,
            search_queries,
            matched_reason=f"{ORIGINAL_MIX_MATCH_REASON_PREFIX}; artist and album matched",
            ambiguous_reason=(
                f"{len(original_mix_album_matches)} candidates matched after removing source Original Mix annotation, "
                "artist, and album"
            ),
        )

    original_mix_artist_matches = tuple(
        candidate for candidate in candidates if candidate_matches_original_mix_track_and_artist(track, candidate)
    )
    if original_mix_artist_matches:
        return _decision_from_candidates(
            track,
            original_mix_artist_matches,
            search_queries,
            matched_reason=f"{ORIGINAL_MIX_MATCH_REASON_PREFIX}; artist matched; album differed",
            ambiguous_reason=(
                f"{len(original_mix_artist_matches)} candidates matched after removing source Original Mix annotation "
                "and matching artist"
            ),
        )

    spotify_original_annotation_matches = tuple(
        candidate
        for candidate in candidates
        if candidate_matches_spotify_original_annotation(track, candidate)
    )
    if spotify_original_annotation_matches:
        return _decision_from_candidates(
            track,
            spotify_original_annotation_matches,
            search_queries,
            matched_reason=SPOTIFY_ORIGINAL_ANNOTATION_MATCH_REASON,
            ambiguous_reason=(
                f"{len(spotify_original_annotation_matches)} candidates matched after removing "
                "Spotify Original annotation"
            ),
            match_strategy=SPOTIFY_ORIGINAL_ANNOTATION_MATCH_STRATEGY,
        )

    featured_credit_reason_prefix = one_sided_featured_credit_match_reason_prefix(track)
    featured_credit_reason_tail = featured_credit_reason_prefix.removeprefix("track matched ")
    featured_credit_album_matches = tuple(
        candidate for candidate in candidates if candidate_matches_one_sided_featured_credit_track(track, candidate)
    )
    if featured_credit_album_matches:
        return _decision_from_candidates(
            track,
            featured_credit_album_matches,
            search_queries,
            matched_reason=(
                f"{featured_credit_reason_prefix}; source artist, featured artist, and album matched"
            ),
            ambiguous_reason=(
                f"{len(featured_credit_album_matches)} candidates matched {featured_credit_reason_tail} "
                "and matching album"
            ),
        )

    featured_credit_artist_matches = tuple(
        candidate
        for candidate in candidates
        if candidate_matches_one_sided_featured_credit_track_and_artist(track, candidate)
    )
    if featured_credit_artist_matches:
        return _decision_from_candidates(
            track,
            featured_credit_artist_matches,
            search_queries,
            matched_reason=(
                f"{featured_credit_reason_prefix}; source and featured artists matched; album differed"
            ),
            ambiguous_reason=(
                f"{len(featured_credit_artist_matches)} candidates matched {featured_credit_reason_tail} "
                "and matching source artist"
            ),
        )

    leading_in_title_matches: tuple[SpotifyTrackCandidate, ...] = ()
    if allow_leading_in_title_variant:
        leading_in_title_matches = tuple(
            candidate
            for candidate in candidates
            if candidate_matches_leading_in_title_variant(track, candidate)
        )
    if leading_in_title_matches:
        return _decision_from_candidates(
            track,
            leading_in_title_matches,
            search_queries,
            matched_reason=f"{LEADING_IN_TITLE_MATCH_REASON_PREFIX}; artist and album matched",
            ambiguous_reason=(
                f"{len(leading_in_title_matches)} candidates had Spotify's leading 'in' title variant "
                "and matched artist and album"
            ),
        )

    alphanumeric_spacing_matches: tuple[SpotifyTrackCandidate, ...] = ()
    if allow_alphanumeric_spacing_fallback:
        alphanumeric_spacing_matches = tuple(
            candidate
            for candidate in candidates
            if candidate_matches_alphanumeric_spacing(track, candidate)
        )
    if alphanumeric_spacing_matches:
        return _decision_from_candidates(
            track,
            alphanumeric_spacing_matches,
            search_queries,
            matched_reason=ALPHANUMERIC_SPACING_MATCH_REASON,
            ambiguous_reason=(
                f"{len(alphanumeric_spacing_matches)} candidates matched after normalizing letter-number spacing"
            ),
            match_strategy=ALPHANUMERIC_SPACING_MATCH_STRATEGY,
        )

    if allow_catalogue_recovery:
        recovery = match_catalogue_variants(track, repeated_credit_observations, search_queries, aligned_album_positions)
        if recovery is not None:
            return recovery

    constrained_typo_matches: tuple[tuple[SpotifyTrackCandidate, int], ...] = ()
    if allow_constrained_typo_fallback:
        constrained_typo_matches = tuple(
            (candidate, distance)
            for candidate in candidates
            if (distance := constrained_title_typo_distance(track, candidate)) is not None
        )
    if constrained_typo_matches:
        typo_candidates = tuple(candidate for candidate, _distance in constrained_typo_matches)
        _candidate, distance = constrained_typo_matches[0]
        return _decision_from_candidates(
            track,
            typo_candidates,
            search_queries,
            matched_reason=(
                f"{CONSTRAINED_TYPO_MATCH_REASON_PREFIX} (distance {distance}); "
                "artist set and album matched"
            ),
            ambiguous_reason=f"{len(typo_candidates)} candidates matched by constrained typo fallback",
        )

    version_substitute_matches: tuple[tuple[SpotifyTrackCandidate, str], ...] = ()
    if allow_version_substitute:
        version_substitute_matches = tuple(
            (candidate, version_label)
            for candidate in candidates
            if (version_label := candidate_version_substitute_label(track, candidate)) is not None
        )
    if version_substitute_matches:
        substitute_candidates = tuple(candidate for candidate, _version_label in version_substitute_matches)
        _candidate, version_label = version_substitute_matches[0]
        return _decision_from_candidates(
            track,
            substitute_candidates,
            search_queries,
            matched_reason=(
                f"track matched using the {version_label} version substitute "
                "after no ordinary title match"
            ),
            ambiguous_reason=f"{len(substitute_candidates)} candidates matched as version substitutes",
            match_strategy=VERSION_SUBSTITUTE_MATCH_STRATEGY,
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


def trailing_performer_credit(label: str) -> tuple[str, str] | None:
    label = normalize_recording_evidence_text(label)
    match = TRAILING_PERFORMER_CREDIT.search(label) or TRAILING_FEATURING_CREDIT.search(label)
    if not match:
        return None
    base = label[:match.start()]
    depth = 0
    for character in base:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                return None
    if not base or depth or TRAILING_PERFORMER_CREDIT.search(base) or TRAILING_FEATURING_CREDIT.search(base):
        return None
    return base, match[1]


def match_repeated_performer_credit(
    track: PlaylistTrack,
    observations: Sequence[SpotifyTrackCandidate],
    search_queries: tuple[str, ...] = (),
) -> TrackMatchDecision | None:
    if not observations:
        return None
    source_credit = normalize_recording_evidence_text(track.artist_name)
    artists = sorted(split_source_artist_names(source_credit))
    if len(set(artists)) < 2 or any(not part.strip() for part in source_credit.split(",")):
        return None
    title = normalize_recording_evidence_text(track.track_name)
    competitors: dict[str, SpotifyTrackCandidate] = {}
    preferred: set[str] = set()
    source_album = normalize_recording_evidence_text(track.album_name)
    for candidate in observations:
        credit = trailing_performer_credit(candidate.name)
        if (
            candidate.version_title_review_only or candidate.title_only_search or not credit or credit[0] != title
            or artists.count(credit[1]) != 1
            or sorted(normalize_recording_evidence_text(name) for name in candidate.artists) != artists
        ):
            continue
        competitors.setdefault(candidate.uri, candidate)
        if source_album and (
            normalize_recording_evidence_text(candidate.album_name) == source_album
            or trailing_performer_credit(candidate.album_name) == (source_album, credit[1])
        ):
            preferred.add(candidate.uri)
    if not competitors:
        return None
    cohort = tuple(competitors.values())
    decision = TrackMatchDecision(
        track=track, status=AMBIGUOUS, spotify_uri="", reason="", review_candidates=cohort,
        search_queries=search_queries, match_strategy=REPEATED_PERFORMER_CREDIT_STRATEGY,
    )
    # A later search can return an unavailable or incomplete observation for a competing URI.
    for candidate in observations:
        first = competitors.get(candidate.uri)
        if first is None:
            continue
        metadata = spotify_recording_metadata_from_values(
            title=candidate.name, artist_names=candidate.artists, album_name=candidate.album_name, album_id=candidate.album_id,
        )
        if (
            candidate.is_playable is not True or candidate.version_title_review_only
            or not candidate.has_complete_recording_metadata or not metadata.album_id or not metadata.album_name
            or not spotify_track_uri_is_structurally_valid(candidate.uri)
            or metadata != candidate.recording_metadata() or metadata != first.recording_metadata()
            or normalize_spotify_isrc(candidate.isrc) != normalize_spotify_isrc(first.isrc)
        ):
            return replace(decision, reason="repeated performer credit has incomplete or conflicting Spotify evidence")
    selected = cohort[0]
    reason = "matched Spotify title with repeated performer credit"
    if len(cohort) > 1:
        codes = {normalize_spotify_isrc(item.isrc) for item in cohort}
        if "" in codes or len(codes) != 1:
            return replace(decision, reason="repeated performer credit has Spotify copies with missing or conflicting ISRCs")
        if len({item.album_id.strip() for item in cohort}) != len(cohort) or len(preferred) != 1:
            return replace(decision, reason="repeated performer credit has no unique source-album edition")
        selected = competitors[preferred.pop()]
        reason += " on the source album among editions with the same ISRC"
    return replace(
        decision, status=MATCHED, candidate=selected, spotify_uri=selected.uri, reason=f'{reason}: "{selected.name}"',
    )


def complete_credit_keys(names: Sequence[str]) -> tuple[str, ...]:
    keys = tuple(sorted(normalize_recording_evidence_text(name) for name in names))
    return keys if keys and all(keys) and len(set(keys)) == len(keys) else ()


def source_credit_names(artist: str) -> tuple[str, ...]:
    return split_source_artist_names(artist) or (artist,)


def exact_title_and_full_credits(track: PlaylistTrack, candidate: SpotifyTrackCandidate) -> bool:
    title = normalize_recording_evidence_text(track.track_name)
    credits = complete_credit_keys(source_credit_names(track.artist_name))
    return (
        bool(title) and title == normalize_recording_evidence_text(candidate.name)
        and bool(credits) and credits == complete_credit_keys(candidate.artists)
    )


def artist_spelling_difference(left: str, right: str) -> str:
    if spelling_difference(left, right):
        return "accent or letter"
    if conjunction_difference(left, right):
        return "conjunction"
    left, right = (normalize_recording_evidence_text(value) for value in (left, right))
    if left != right and left.replace(" ", "") == right.replace(" ", ""):
        return "spacing"
    return ""


def catalogue_spelling_strategy(
    source_title: str, source_artists: Sequence[str], candidate: SpotifyTrackCandidate,
) -> str:
    title = normalize_recording_evidence_text(source_title)
    differences: set[str] = set()
    title_conjunction = False
    if not title:
        return ""
    if title != normalize_recording_evidence_text(candidate.name):
        if spelling_difference(source_title, candidate.name):
            differences.add("accent or letter")
        elif conjunction_difference(source_title, candidate.name):
            title_conjunction = True
        else:
            return ""
    source_keys = complete_credit_keys(source_artists)
    other_keys = complete_credit_keys(candidate.artists)
    if not source_keys or len(source_keys) != len(other_keys):
        return ""
    remaining = set(other_keys) - set(source_keys)
    matched_credits: set[str] = set()
    for source in set(source_keys) - set(other_keys):
        matches = [(other, rule) for other in remaining if (rule := artist_spelling_difference(source, other))]
        if len(matches) != 1 or matches[0][0] in matched_credits:
            return ""
        other, rule = matches[0]
        matched_credits.add(other)
        differences.add(rule)
    # Only accent/letter differences may combine across the title and individual credits.
    if len(differences) > 1 or (title_conjunction and differences):
        return ""
    if title_conjunction:
        return TITLE_CONJUNCTION_STRATEGY
    return CATALOGUE_SPELLING_STRATEGY if differences else ""


def _composer_title_key(title: str) -> str:
    return re.sub(r"\s*/\s*", "/", normalize_recording_evidence_text(title))


def _composer_album_forms(label: str) -> set[str]:
    clean = _composer_title_key(label)
    subtitle = re.fullmatch(r"([^()]+?)\s*\(([^()]+)\)", clean)
    return {clean, _composer_title_key(f"{subtitle[1]}/{subtitle[2]}")} if subtitle else {clean}


def _composer_prefix_matches(prefix: str, artist: str) -> bool:
    label = normalize_recording_evidence_text(prefix)
    names = normalize_recording_evidence_text(artist).split()
    if not label or not names:
        return False
    surname, comma, suffix = label.partition(",")
    if comma:
        initials = re.sub(r"[.\s]", "", suffix)
        return (
            surname.strip() == names[-1] and 0 < len(initials) < len(names)
            and initials == "".join(name[0] for name in names[:len(initials)])
        )
    parts = label.replace(".", " ").split()
    if not parts or parts[-1] != names[-1]:
        return False
    initials = parts[:-1]
    return len(initials) <= len(names) - 1 and all(
        len(initial) == 1 and name.startswith(initial) for initial, name in zip(initials, names)
    )


def credited_composer(candidate: SpotifyTrackCandidate) -> str:
    prefix, delimiter, _ = candidate.name.partition(":")
    if not delimiter:
        return ""
    matches = [artist for artist in candidate.artists if _composer_prefix_matches(prefix, artist)]
    return normalize_recording_evidence_text(matches[0]) if len(matches) == 1 else ""


def composer_prefix_candidates(
    track: PlaylistTrack, candidates: Sequence[SpotifyTrackCandidate],
) -> tuple[SpotifyTrackCandidate, ...]:
    source_title = _composer_title_key(track.track_name)
    source_album = _composer_album_forms(track.album_name) - {""}
    credits = complete_credit_keys(source_credit_names(track.artist_name))
    rivals: list[SpotifyTrackCandidate] = []
    verified_prefix = False
    for candidate in candidates:
        _, delimiter, remainder = candidate.name.partition(":")
        if (
            not source_title or not credits or not set(credits) <= set(complete_credit_keys(candidate.artists))
            or not source_album.intersection(_composer_album_forms(candidate.album_name))
        ):
            continue
        prefixed = bool(delimiter) and _composer_title_key(remainder) == source_title
        if prefixed or _composer_title_key(candidate.name) == source_title:
            rivals.append(candidate)
            verified_prefix = verified_prefix or (prefixed and bool(credited_composer(candidate)))
    return tuple(rivals) if verified_prefix else ()


def has_aligned_album_position(
    candidate: SpotifyTrackCandidate, positions: Sequence[SpotifyTrackCandidate],
) -> bool:
    return any(
        candidate.uri == position.uri and candidate.recording_metadata() == position.recording_metadata()
        for position in positions
    )


def select_catalogue_recording(
    track: PlaylistTrack,
    competitors: Sequence[SpotifyTrackCandidate],
    observations: Sequence[SpotifyTrackCandidate],
    strategy: str,
    search_queries: tuple[str, ...],
    *,
    aligned_album_positions: Sequence[SpotifyTrackCandidate] = (),
) -> TrackMatchDecision:
    rivals: dict[str, SpotifyTrackCandidate] = {}
    for candidate in competitors:
        rivals.setdefault(candidate.uri, candidate)
    label = strategy.replace("_", " ")
    hold = TrackMatchDecision(track, AMBIGUOUS, "", "", review_candidates=tuple(rivals.values()),
                              search_queries=search_queries, match_strategy=strategy)
    if strategy == COMPOSER_PREFIX_STRATEGY and len(rivals) != 1:
        return replace(hold, reason="composer prefix has competing copies on the source album")
    playable: list[SpotifyTrackCandidate] = []
    for uri, rival in rivals.items():
        expected = rival.recording_metadata()
        variants = [candidate for candidate in observations if candidate.uri == uri]
        codes = {code for candidate in variants if (code := normalize_spotify_isrc(candidate.isrc))}
        supporting = [
            candidate for candidate in variants
            if not candidate.version_title_review_only
            and (not candidate.title_only_search or strategy == EXACT_TITLE_RECORDING_STRATEGY)
        ]
        if (
            not spotify_track_uri_is_structurally_valid(uri) or not expected.album_id or not expected.album_name
            or any(
                not candidate.has_complete_recording_metadata or candidate.recording_metadata() != expected
                or candidate.is_playable is False for candidate in variants
            )
            or len(codes) > 1
            or not any(candidate.is_playable is True for candidate in supporting)
        ):
            return replace(hold, reason=f"{label} has incomplete or conflicting Spotify evidence")
        isrc = next((code for candidate in supporting if (code := normalize_spotify_isrc(candidate.isrc))), "")
        playable.append(replace(rival, is_playable=True, isrc=isrc))
    if len(playable) > 1 and (
        not playable[0].isrc or len({candidate.isrc for candidate in playable}) != 1
        or len({candidate.album_id for candidate in playable}) != len(playable)
    ):
        return replace(hold, reason=f"{label} has competing copies without one shared recording code")

    preferred = [
        candidate for candidate in playable
        if normalized_nonempty_music_values_match(track.album_name, candidate.album_name)
    ]
    if strategy == CATALOGUE_SPELLING_STRATEGY:
        anchored = [
            candidate for candidate in playable if has_aligned_album_position(candidate, aligned_album_positions)
        ]
        if not anchored:
            return replace(hold, reason="catalogue spelling requires a verified recording or an independently aligned position")
        preferred = anchored
    choice = min(preferred or playable, key=lambda candidate: candidate.uri)
    return replace(
        hold, status=MATCHED, candidate=choice, spotify_uri=choice.uri, review_candidates=tuple(playable),
        reason=f'{label} matched "{choice.name}"',
    )


def match_catalogue_variants(
    track: PlaylistTrack,
    observations: Sequence[SpotifyTrackCandidate],
    search_queries: tuple[str, ...],
    aligned_album_positions: Sequence[SpotifyTrackCandidate] = (),
) -> TrackMatchDecision | None:
    eligible = tuple(
        candidate for candidate in observations if not candidate.version_title_review_only and not candidate.title_only_search
    )
    competitors: dict[str, list[SpotifyTrackCandidate]] = {}
    credits = source_credit_names(track.artist_name)
    for candidate in eligible:
        strategy = catalogue_spelling_strategy(track.track_name, credits, candidate)
        if strategy:
            competitors.setdefault(strategy, []).append(candidate)
    spelling_uris = {candidate.uri for candidate in competitors.get(CATALOGUE_SPELLING_STRATEGY, ())}
    for position in aligned_album_positions:
        if (
            position.uri not in spelling_uris
            and catalogue_spelling_strategy(track.track_name, credits, position) == CATALOGUE_SPELLING_STRATEGY
        ):
            competitors.setdefault(CATALOGUE_SPELLING_STRATEGY, []).append(position)
    if composer := composer_prefix_candidates(track, eligible):
        competitors[COMPOSER_PREFIX_STRATEGY] = list(composer)
    matches = [
        select_catalogue_recording(
            track, rivals, observations, strategy, search_queries, aligned_album_positions=aligned_album_positions,
        )
        for strategy, rivals in competitors.items()
    ]
    if repeated := match_repeated_performer_credit(track, observations, search_queries):
        matches.append(repeated)
    if len(matches) > 1 and (not matches[0].spotify_uri or any(
        decision.spotify_uri != matches[0].spotify_uri for decision in matches[1:]
    )):
        return TrackMatchDecision(
            track, AMBIGUOUS, "", "catalogue spelling or credit rules found competing candidates",
            review_candidates=deduplicate_spotify_track_candidates(tuple(
                candidate for decision in matches for candidate in decision.review_candidates
            )), search_queries=search_queries,
        )
    return matches[0] if matches else None


def match_exact_title_recording(
    track: PlaylistTrack,
    observations: Sequence[SpotifyTrackCandidate],
    search_queries: tuple[str, ...],
    *,
    title_search_complete: bool = False,
) -> TrackMatchDecision | None:
    competitors = tuple(
        candidate for candidate in observations
        if not candidate.version_title_review_only and exact_title_and_full_credits(track, candidate)
    )
    if not competitors:
        return None
    if not title_search_complete and any(candidate.title_only_search for candidate in competitors):
        return TrackMatchDecision(
            track, AMBIGUOUS, "", "exact-title results need review because title-only search coverage was incomplete",
            review_candidates=deduplicate_spotify_track_candidates(competitors), search_queries=search_queries,
        )
    return replace(
        select_catalogue_recording(track, competitors, observations, EXACT_TITLE_RECORDING_STRATEGY, search_queries),
        title_search_complete=title_search_complete,
    )


def resolve_spotify_edition_tie(
    decision: TrackMatchDecision,
    observed_candidates: Sequence[SpotifyTrackCandidate],
) -> TrackMatchDecision:
    if decision.status != AMBIGUOUS:
        return decision
    track = decision.track
    winning_uris = {candidate.uri for candidate in decision.review_candidates}
    if (
        len(winning_uris) < 2
        or not all(candidate_matches_track(track, candidate) for candidate in decision.review_candidates)
        or not all(spotify_track_uri_is_structurally_valid(uri) for uri in winning_uris)
        or {
            candidate.uri for candidate in observed_candidates
            if candidate.name and candidate_matches_track(track, candidate)
        } != winning_uris
    ):
        return decision

    winning_observations = tuple(candidate for candidate in observed_candidates if candidate.uri in winning_uris)
    if len({spotify_candidate_identity_key(track.playlist_name, candidate) for candidate in winning_observations}) != 1:
        return decision

    source_title = normalize_recording_evidence_text(track.track_name)
    source_album = normalize_recording_evidence_text(track.album_name)
    source_artist_names = split_source_artist_names(track.artist_name) or (track.artist_name,)
    source_artists = sorted(normalize_recording_evidence_text(artist) for artist in source_artist_names)
    if not source_title or not source_album or not all(source_artists):
        return decision

    candidates_by_uri: dict[str, SpotifyTrackCandidate] = {}
    recording_isrc = ""
    for candidate in winning_observations:
        isrc = normalize_spotify_isrc(candidate.isrc)
        album_id = candidate.album_id.strip()
        if (
            not candidate.has_complete_recording_metadata
            or not isrc
            or (recording_isrc and isrc != recording_isrc)
            or not album_id
            or normalize_recording_evidence_text(candidate.name) != source_title
            or normalize_recording_evidence_text(candidate.album_name) != source_album
            or sorted(normalize_recording_evidence_text(artist) for artist in candidate.artists) != source_artists
        ):
            return decision
        recording_isrc = isrc
        previous = candidates_by_uri.get(candidate.uri)
        if previous is None:
            candidates_by_uri[candidate.uri] = replace(candidate, album_id=album_id, isrc=isrc)
        elif previous.album_id != album_id or (
            previous.is_playable is not None
            and candidate.is_playable is not None
            and previous.is_playable != candidate.is_playable
        ):
            return decision
        elif previous.is_playable is None and candidate.is_playable is not None:
            candidates_by_uri[candidate.uri] = replace(previous, is_playable=candidate.is_playable)

    cohort = tuple(candidates_by_uri[candidate.uri] for candidate in decision.review_candidates)
    if len({candidate.album_id for candidate in cohort}) != len(cohort):
        return decision
    selected = min(
        (candidate for candidate in cohort if candidate.is_playable is not False),
        key=lambda candidate: (candidate.is_playable is not True, candidate.uri),
        default=None,
    )
    if selected is None:
        return decision
    return replace(
        decision,
        status=MATCHED,
        spotify_uri=selected.uri,
        reason="same recording across Spotify editions with matching track title, artists, and album",
        candidate=selected,
        review_candidates=cohort,
        match_strategy=SAME_RECORDING_EDITION_MATCH_STRATEGY,
    )


def normalize_spotify_isrc(value: object) -> str:
    if not isinstance(value, str):
        return ""
    code = value.strip()
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", code, flags=re.ASCII | re.IGNORECASE):
        return code.upper()
    return ""


def spotify_recording_metadata_is_complete(
    *,
    uri: object,
    title: object,
    artist_names: object,
    album_name: object,
    album_id: object,
) -> bool:
    return (
        all(isinstance(value, str) and bool(value.strip()) for value in (uri, title, album_name, album_id))
        and isinstance(artist_names, (list, tuple))
        and bool(artist_names)
        and all(isinstance(artist, str) and bool(artist.strip()) for artist in artist_names)
    )


def spotify_recording_metadata_from_values(
    *,
    title: object,
    artist_names: object,
    album_name: object,
    album_id: object,
) -> SpotifyRecordingMetadata:
    artists: tuple[str, ...] = ()
    if isinstance(artist_names, (list, tuple)) and all(
        isinstance(artist, str) and artist.strip() for artist in artist_names
    ):
        artists = tuple(sorted(normalize_recording_evidence_text(artist) for artist in artist_names))
    return SpotifyRecordingMetadata(
        title=normalize_recording_evidence_text(title) if isinstance(title, str) else "",
        artist_names=artists,
        album_name=normalize_recording_evidence_text(album_name) if isinstance(album_name, str) else "",
        album_id=album_id.strip() if isinstance(album_id, str) else "",
    )


def spotify_track_uri_is_structurally_valid(spotify_uri: str) -> bool:
    segments = spotify_uri.split(":")
    return bool(
        len(segments) == 3
        and segments[0] == "spotify"
        and segments[1] == "track"
        and segments[2]
        and not any(character.isspace() for character in segments[2])
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
        and normalized_nonempty_music_values_match(track.album_name, candidate.album_name)
    )


def candidate_matches_track_and_artist(track: PlaylistTrack, candidate: SpotifyTrackCandidate) -> bool:
    return (
        normalized_nonempty_music_values_match(track.track_name, candidate.name)
        and source_artist_matches_candidate(track.artist_name, candidate.artists)
    )


def candidate_matches_original_mix_track(track: PlaylistTrack, candidate: SpotifyTrackCandidate) -> bool:
    return (
        candidate_matches_original_mix_track_and_artist(track, candidate)
        and normalized_nonempty_music_values_match(track.album_name, candidate.album_name)
    )


def candidate_matches_original_mix_track_and_artist(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> bool:
    source_track_name = track.track_name.strip()
    base_track_name = title_without_original_mix_suffix(source_track_name)
    return (
        base_track_name != source_track_name
        and normalized_nonempty_music_values_match(base_track_name, candidate.name)
        and source_artist_matches_candidate(track.artist_name, candidate.artists)
    )


def candidate_matches_spotify_original_annotation(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> bool:
    candidate_title = candidate.name.strip()
    base_candidate_title = title_without_spotify_original_annotation(candidate_title)
    source_artist_set = normalized_source_artist_set(track.artist_name)
    source_album = normalize_music_text(track.album_name)
    return bool(
        base_candidate_title != candidate_title
        and normalized_nonempty_music_values_match(track.track_name, base_candidate_title)
        and source_artist_set
        and source_artist_set == normalized_candidate_artist_set(candidate.artists)
        and source_album
        and source_album == normalize_music_text(candidate.album_name)
    )


def candidate_matches_one_sided_featured_credit_track(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> bool:
    return (
        candidate_matches_one_sided_featured_credit_track_and_artist(track, candidate)
        and normalized_nonempty_music_values_match(track.album_name, candidate.album_name)
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
        normalized_nonempty_music_values_match(base_title, compared_title)
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


def candidate_matches_alphanumeric_spacing(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> bool:
    source_album = normalize_alphanumeric_spacing(track.album_name)
    candidate_album = normalize_alphanumeric_spacing(candidate.album_name)
    return bool(
        music_values_match_only_after_alphanumeric_spacing(
            track.track_name,
            candidate.name,
        )
        and source_album
        and source_album == candidate_album
        and source_artist_set_is_subset_of_candidate(track.artist_name, candidate.artists)
    )


def candidate_version_substitute_label(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> str | None:
    source_version = split_version_substitute_title(track.track_name)
    candidate_version = split_version_substitute_title(candidate.name)
    if (source_version is None) == (candidate_version is None):
        return None
    source_base_title = source_version[0] if source_version is not None else track.track_name
    candidate_base_title = candidate_version[0] if candidate_version is not None else candidate.name
    if (
        not normalized_nonempty_music_values_match(source_base_title, candidate_base_title)
        or not source_artist_set_is_subset_of_candidate(track.artist_name, candidate.artists)
    ):
        return None
    if source_version is not None:
        return source_version[1]
    if candidate_version is not None:
        return candidate_version[1]
    return None


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
    # An edit distance does not distinguish an accented letter or another script from a typo.
    if not source_token.isascii() or not candidate_token.isascii():
        return None
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


def source_artist_set_is_subset_of_candidate(
    source_artist_name: str,
    candidate_artists: tuple[str, ...],
) -> bool:
    source_artist_set = normalized_source_artist_set(source_artist_name)
    candidate_artist_set = normalized_candidate_artist_set(candidate_artists)
    return bool(source_artist_set) and source_artist_set <= candidate_artist_set


def source_artist_matches_candidate(artist_name: str, candidate_artists: tuple[str, ...]) -> bool:
    source_names = source_credit_names(artist_name)
    if len(source_names) > 1 and (
        any(has_legacy_loss(name) for name in (*source_names, *candidate_artists))
        or any(artist_spelling_difference(source, candidate) for source in source_names for candidate in candidate_artists)
    ):
        return source_artist_set_is_subset_of_candidate(artist_name, candidate_artists)
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


def artist_spelling(value: str) -> str:
    """Compare documented artist spellings while preserving letters, accents and words."""
    text = normalize_recording_evidence_text(value)
    text = re.sub(r"\s*\.\s*", ".", text)
    return re.sub(r"(?<=[^\W_])\s*[-\u2010\u2011]\s*(?=[^\W_])", " ", text)


def recording_proof_source_matches(track: PlaylistTrack, *, title: str, artist: str, album: str) -> bool:
    return all(
        bool(source_text := normalize_recording_evidence_text(source))
        and source_text == normalize_recording_evidence_text(saved)
        for source, saved in ((track.track_name, title), (track.artist_name, artist), (track.album_name, album))
    )


def normalize_music_text(value: str) -> str:
    return " ".join("".join(
        character if character not in LETTERLIKE_APOSTROPHE_CHARACTERS
        and unicodedata.category(character)[0] in "LMN" else " "
        for character in normalize_recording_evidence_text(value)
    ).split())


def legacy_music_text(value: str) -> str:
    """Read the original on-disk cache key without applying it to matching."""
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


def has_legacy_loss(value: str) -> bool:
    return bool(value.strip()) and (
        not (current := normalize_music_text(value)) or current != legacy_music_text(value)
    )


def spotify_candidate_identity_key(target_playlist_name: str, candidate: SpotifyTrackCandidate) -> str:
    return spotify_track_identity_key(
        target_playlist_name=target_playlist_name,
        artist_names=candidate.artists,
        album_name=candidate.album_name,
        track_name=candidate.name,
    )


def spotify_track_identity_key(
    target_playlist_name: str,
    artist_names: Sequence[str],
    album_name: str,
    track_name: str,
) -> str:
    normalized_artists = normalized_identity_artists(artist_names)
    return "|".join(
        (
            music_identity_component(target_playlist_name),
            ",".join(normalized_artists),
            music_identity_component(album_name),
            music_identity_component(track_name),
        )
    )


def normalized_identity_artists(artist_names: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            normalized_artist
            for normalized_artist in (music_identity_component(artist_name) for artist_name in artist_names)
            if normalized_artist
        )
    )


def music_identity_component(value: str) -> str:
    if normalized := normalize_music_text(value):
        return normalized
    literal = normalize_recording_evidence_text(value)
    return "#" + literal.encode("utf-8").hex() if literal else ""


def legacy_spotify_track_identity_key(target: str, artists: Sequence[str], album: str, title: str) -> str:
    legacy_artists = sorted(key for name in artists if (key := legacy_music_text(name)))
    return "|".join((legacy_music_text(target), " ".join(legacy_artists), legacy_music_text(album), legacy_music_text(title)))


def normalize_alphanumeric_spacing(value: str) -> str:
    normalized = normalize_music_text(value)
    return re.sub(r"(?<=[a-z])\s+(?=\d)|(?<=\d)\s+(?=[a-z])", "", normalized)


def music_values_match_only_after_alphanumeric_spacing(left: str, right: str) -> bool:
    normalized_left = normalize_music_text(left)
    normalized_right = normalize_music_text(right)
    return bool(
        normalized_left
        and normalized_right
        and normalized_left != normalized_right
        and normalize_alphanumeric_spacing(left) == normalize_alphanumeric_spacing(right)
    )


def normalized_nonempty_music_values_match(left: str, right: str) -> bool:
    left_key = normalize_music_text(left)
    return bool(left_key) and left_key == normalize_music_text(right)
