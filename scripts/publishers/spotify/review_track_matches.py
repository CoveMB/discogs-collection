#!/usr/bin/env python3
"""Review Spotify candidates for the credited and canonical Discogs names on one pressing."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from itertools import combinations
from pathlib import Path

SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[2]
if str(SCRIPTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from discogs_tracklists import clean_discogs_artist_name, fetch_release_payload, included_track_items, parse_artists
from publishers.spotify.authorization_flow import DEFAULT_AUTHORIZE_SCOPES
from publishers.spotify.client import (
    SpotifyApiError,
    SpotifyAuthenticationError,
    SpotifyClient,
    SpotifyRateLimitDeferredError,
    SpotifyRateLimitRetriesExhaustedError,
    format_wait_duration,
    parse_search_track_items,
)
from publishers.spotify.env import DEFAULT_ENV_PATH, DEFAULT_TOKEN_CACHE_PATH, load_spotify_settings
from publishers.spotify.match_cache import (
    cache_record_has_recording_conflict,
    cached_track_match,
    compare_cached_manual_source,
    compare_manual_source,
    load_spotify_track_match_cache,
    spotify_candidate_from_cache_record,
    spotify_track_match_key,
    track_match_cache_entry,
)
from publishers.spotify.matching import (
    AMBIGUOUS,
    MATCHED,
    SPOTIFY_TRACK_SEARCH_QUERY_CHARACTER_LIMIT,
    UNMATCHED,
    PlaylistTrack,
    SpotifyRecordingMetadata,
    SpotifyTrackCandidate,
    build_spotify_track_search_query,
    normalize_recording_evidence_text,
    spotify_track_uri_is_structurally_valid,
)
from publishers.spotify.publish_playlist import read_playlist_tracks
from publishers.spotify.session import SpotifySession, get_spotify_access_token, load_existing_token
from shared.discogs_api import DEFAULT_REQUEST_INTERVAL_SECONDS, DiscogsRateLimiter, default_discogs_rate_limit
from shared.workflow_paths import DEFAULT_SPOTIFY_MATCH_CACHE_PATH


SEARCH_LIMIT = 10
DISCOGS_PLACEHOLDER_NAMES = frozenset((
    "various", "unknown artist", "no artist", "traditional", "folk", "anonymous", "guests", "artificial intelligence",
))


@dataclass(frozen=True)
class DiscogsCredit:
    artist_id: int
    printed: str
    canonical: str


@dataclass
class ReviewRow:
    source: dict[str, str]
    playlists: list[str]
    status: str = "pending"
    reason: str = ""
    discogs_credits: tuple[DiscogsCredit, ...] = ()
    queries: list[dict[str, str]] = field(default_factory=list)
    candidates: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class SearchPage:
    hits: tuple[SpotifyTrackCandidate, ...] = ()
    reason: str = ""
    terminal: bool = False


def strict_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def artist_spelling(value: str) -> str:
    text = normalize_recording_evidence_text(value)
    text = re.sub(r"\s*\.\s*", ".", text)
    return re.sub(r"(?<=[^\W_])\s*[-\u2010\u2011]\s*(?=[^\W_])", " ", text)


def discogs_credits(value: object) -> tuple[DiscogsCredit, ...]:
    if not isinstance(value, list) or not value:
        return ()
    credits: list[DiscogsCredit] = []
    by_spelling: dict[str, DiscogsCredit] = {}
    canonical_by_id: dict[int, str] = {}
    for item in value:
        if not isinstance(item, Mapping):
            return ()
        artist_id, name, variation = item.get("id"), item.get("name"), item.get("anv", "")
        if type(artist_id) is not int or artist_id <= 0 or not isinstance(name, str) or not isinstance(variation, str):
            return ()
        canonical = clean_discogs_artist_name(name)
        printed = clean_discogs_artist_name(variation) if variation.strip() else canonical
        if not canonical or not printed:
            return ()
        if any(normalize_recording_evidence_text(name) in DISCOGS_PLACEHOLDER_NAMES for name in (printed, canonical)):
            return ()
        credit = DiscogsCredit(artist_id, printed, canonical)
        canonical_key = artist_spelling(canonical)
        if canonical_by_id.setdefault(artist_id, canonical_key) != canonical_key:
            return ()
        for spelling in {artist_spelling(printed), canonical_key}:
            if by_spelling.setdefault(spelling, credit) != credit:
                return ()
        credits.append(credit)
    return tuple(credits)


def source_groups(tracks: Sequence[PlaylistTrack], release_id: str) -> list[tuple[PlaylistTrack, ReviewRow]]:
    groups: dict[str, list[tuple[PlaylistTrack, ReviewRow]]] = {}
    for track in tracks:
        if track.release_id != release_id:
            continue
        sources = groups.setdefault(spotify_track_match_key(track), [])
        for source, row in sources:
            if (
                track == replace(source, playlist_name=track.playlist_name, spotify_search_query=track.spotify_search_query)
                or compare_manual_source(
                    source, release_id=track.release_id, track_number=track.track_number,
                    title=track.track_name, artist=track.artist_name, album=track.album_name,
                )
            ):
                if track.playlist_name not in row.playlists:
                    row.playlists.append(track.playlist_name)
                break
        else:
            sources.append((track, ReviewRow(
                source={
                    "track_number": track.track_number,
                    "discogs_position": "",
                    "title": track.track_name,
                    "artist": track.artist_name,
                    "album": track.album_name,
                },
                playlists=[track.playlist_name],
            )))
    for sources in groups.values():
        if len(sources) > 1:
            for _, row in sources:
                row.status = "source_collision"
                row.reason = "Conflicting generated rows share the manual import key."
    return [source for sources in groups.values() for source in sources]


def assess_row(track: PlaylistTrack, row: ReviewRow, matches: Mapping[str, Mapping[str, object]]) -> tuple[SpotifyTrackCandidate, ...]:
    primary = matches.get(spotify_track_match_key(track))
    _, selected = track_match_cache_entry(track, matches)
    records = (primary, selected)
    if any(record is not None and strict_text(record.get("match_status")) == "manual" for record in records):
        row.status, row.reason = "manual_hold", "The match cache key has a manual owner."
    elif any(cache_record_has_recording_conflict(record) for record in records):
        row.status, row.reason = "recording_conflict", "The match cache has a recording-conflict hold."
    elif (effective := cached_track_match(track, matches)) and effective.decision.status == MATCHED:
        row.status, row.reason = "already_matched", "The publisher has a current match."
    elif (
        selected is not None
        and effective is not None
        and effective.decision.status in {AMBIGUOUS, UNMATCHED}
        and compare_cached_manual_source(track, selected) is True
    ):
        return source_bound_cached_candidates(selected)
    else:
        row.status = "unassessed"
        row.reason = "Run the ordinary publisher preview to obtain a current assessment for this exact source."
    return ()


def source_bound_cached_candidates(record: Mapping[str, object]) -> tuple[SpotifyTrackCandidate, ...]:
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
        candidate = spotify_candidate_from_cache_record(raw, require_recording_metadata=True)
        if (
            candidate and not candidate.version_title_review_only
            and spotify_track_uri_is_structurally_valid(candidate.uri)
            and "spotify_observed_recording_metadata" not in raw
        ):
            candidates.append(candidate)
    return tuple(candidates)


def separate_credit_issue(value: object, *, other_positions: set[str] | None = None) -> str:
    if not isinstance(value, list):
        return "Discogs returned malformed separate artist credits."
    for credit in value:
        if not isinstance(credit, Mapping) or not (role := strict_text(credit.get("role"))):
            return "Discogs returned malformed separate artist credits."
        # Scope to a different track only when the release names one known printed position.
        scope = normalize_recording_evidence_text(strict_text(credit.get("tracks")))
        if re.search(r"(?:^|,)\s*featuring\s*(?:\[|,|$)", role, re.IGNORECASE) and (
            other_positions is None or scope not in other_positions
        ):
            return "Discogs lists a separate featuring credit that may apply to this track."
    return ""


def bind_discogs_sources(
    payload: object, release_id: str, rows: Sequence[tuple[PlaylistTrack, ReviewRow]],
) -> None:
    if not isinstance(payload, Mapping) or type(payload.get("id")) is not int or str(payload["id"]) != release_id:
        raise ValueError("Discogs did not return the requested pressing ID.")
    album = strict_text(payload.get("title"))
    items = {str(ordinal): entry for ordinal, entry in enumerate(
        included_track_items(payload.get("tracklist"), strict=True), start=1,
    )}
    positions = Counter(normalize_recording_evidence_text(strict_text(item.get("position"))) for item, _ in items.values())
    for track, row in rows:
        entry = items.get(track.track_number)
        if entry is None:
            row.status, row.reason = "source_unverified", "The exported track ordinal is missing or out of range."
            continue
        item, parents = entry
        printed_position = strict_text(item.get("position"))
        position_key = normalize_recording_evidence_text(printed_position)
        credited_artists = item.get("artists", [])
        if credited_artists == []:
            credited_artists = payload.get("artists")
        row.source["discogs_position"] = printed_position
        if any(
            strict_text(parent.get("type_")).casefold() not in {"heading", "index"}
            or parent.get("position") or parent.get("artists") or parent.get("extraartists", []) != []
            for parent in parents
        ):
            row.status, row.reason = "source_unverified", "A nested track has unsupported parent positioning or credits."
        elif not printed_position or positions[position_key] != 1:
            row.status, row.reason = "source_unverified", "The Discogs printed position is missing or ambiguous."
        elif credit_issue := separate_credit_issue(item.get("extraartists", [])) or separate_credit_issue(
            payload.get("extraartists", []), other_positions={key for key in positions if key and key != position_key},
        ):
            row.status, row.reason = "source_unverified", credit_issue
        elif not compare_manual_source(
            track, release_id=release_id, track_number=track.track_number, title=item.get("title"),
            artist=parse_artists(credited_artists), album=album,
        ):
            row.status, row.reason = "source_unverified", "The generated row does not match the pressing at its exported ordinal."
        elif not (credits := discogs_credits(credited_artists)):
            row.status, row.reason = "source_unverified", "Structured Discogs artist credits are missing, malformed or ambiguous."
        else:
            row.discogs_credits = credits
            if not any(credit.printed != credit.canonical for credit in credits):
                row.status, row.reason = "no_variation", "The Discogs credit has no distinct credited and canonical forms."


def matching_credits(
    source_title: str, credits: tuple[DiscogsCredit, ...], hit: SpotifyTrackCandidate,
) -> list[dict[str, object]] | None:
    if not hit.has_complete_recording_metadata or (
        normalize_recording_evidence_text(source_title) != normalize_recording_evidence_text(hit.name)
    ):
        return None
    if len(hit.artists) != len(credits):
        return None
    # Different Discogs credits with overlapping spellings were rejected at source binding.
    remaining = list(credits)
    evidence: list[dict[str, object]] = []
    for artist in hit.artists:
        name = artist_spelling(artist)
        for index, credit in enumerate(remaining):
            printed = name == artist_spelling(credit.printed)
            canonical = name == artist_spelling(credit.canonical)
            if printed or canonical:
                evidence.append({
                    "artist": artist,
                    "discogs_artist_id": credit.artist_id,
                    "spelling": "both" if printed and canonical else "printed" if printed else "canonical",
                })
                remaining.pop(index)
                break
        else:
            return None
    return evidence


def open_spotify_search(args: argparse.Namespace) -> tuple[SpotifyClient, str]:
    settings = load_spotify_settings(args.env_file, token_cache_path=args.token_cache)
    access_token = get_spotify_access_token(settings, required_scopes=DEFAULT_AUTHORIZE_SCOPES)
    token = load_existing_token(settings)
    if token is None or token.access_token != access_token:
        raise SpotifyAuthenticationError("The authorized Spotify token was not saved.")
    return SpotifyClient(token_provider=SpotifySession(settings, token, DEFAULT_AUTHORIZE_SCOPES)), access_token


def parse_review_hit(item: object) -> SpotifyTrackCandidate | None:
    if not isinstance(item, dict) or item.get("type", "track") != "track":
        return None
    parsed = parse_search_track_items((item,))
    if not parsed or not spotify_track_uri_is_structurally_valid(parsed[0].uri):
        return None
    hit = parsed[0]
    if hit.has_complete_recording_metadata:
        return hit
    # Retain only typed facts from a partial item. They can contradict a lead but cannot establish one.
    metadata = hit.recording_metadata()
    raw_artists = item.get("artists")
    artists = tuple(
        name for artist in raw_artists if isinstance(artist, dict) and (name := strict_text(artist.get("name")))
    ) if isinstance(raw_artists, list) else ()
    return replace(
        hit, name=hit.name if metadata.title else "", artists=artists,
        album_name=hit.album_name if metadata.album_name else "", album_id=metadata.album_id,
    )


def search_first_page(client: SpotifyClient, access_token: str, query: str) -> SearchPage:
    if len(query) > SPOTIFY_TRACK_SEARCH_QUERY_CHARACTER_LIMIT:
        return SearchPage(reason="The title and credit exceed Spotify’s query length limit.")
    try:
        response = client.search_tracks_response(access_token, query, SEARCH_LIMIT)
    except SpotifyAuthenticationError:
        return SearchPage(reason="Spotify authentication failed. Reauthorize before restarting.", terminal=True)
    except (SpotifyRateLimitDeferredError, SpotifyRateLimitRetriesExhaustedError) as error:
        return SearchPage(reason=f"Spotify rate limit. Retry after {format_wait_duration(error.retry_after_seconds)}.", terminal=True)
    except UnicodeDecodeError:
        return SearchPage(reason="Spotify returned a response that could not be decoded.")
    except (SpotifyApiError, OSError, http.client.HTTPException):
        return SearchPage(reason="The Spotify search request failed.", terminal=True)
    if response.status != 200:
        return SearchPage(reason=f"Spotify search returned HTTP {response.status}.", terminal=response.status != 400)
    try:
        payload = json.loads(response.body)
    except ValueError:
        return SearchPage(reason="Spotify returned invalid JSON.")
    tracks = payload.get("tracks") if isinstance(payload, dict) else None
    if not isinstance(tracks, dict) or not isinstance(items := tracks.get("items"), list):
        return SearchPage(reason="Spotify returned a malformed track page.")
    offset = tracks.get("offset", 0)
    total = tracks.get("total", len(items))
    if type(offset) is not int or offset != 0 or type(total) is not int or total < len(items) or (total and not items):
        return SearchPage(reason="Spotify returned inconsistent first-page metadata.")
    hits = tuple(hit for item in items[:SEARCH_LIMIT] if (hit := parse_review_hit(item)) is not None)
    incomplete = len(hits) != len(items) or any(not hit.has_complete_recording_metadata for hit in hits)
    return SearchPage(hits, "The Spotify first page contained malformed or excess track items." if incomplete else "")


def observations_conflict(left: SpotifyTrackCandidate, right: SpotifyTrackCandidate) -> bool:
    if left.recording_metadata().conflicts_with(right.recording_metadata()):
        return True
    for partial, other in ((left, right), (right, left)):
        complete_artists = other.recording_metadata().artist_names
        if not partial.recording_metadata().artist_names and complete_artists:
            known_artists = Counter(normalize_recording_evidence_text(artist) for artist in partial.artists)
            if known_artists - Counter(complete_artists):
                return True
    return bool(left.isrc and right.isrc and left.isrc != right.isrc)


def report_candidates(
    track: PlaylistTrack, row: ReviewRow, observations: Mapping[tuple[SpotifyTrackCandidate, SpotifyRecordingMetadata], list[str]],
) -> None:
    by_uri: dict[str, list[tuple[SpotifyTrackCandidate, dict[str, object]]]] = {}
    for (hit, metadata), origins in observations.items():
        by_uri.setdefault(hit.uri, []).append((hit, {
            "title": hit.name,
            "artists": hit.artists,
            "album": hit.album_name,
            "album_id": hit.album_id,
            "isrc": hit.isrc,
            "origins": origins,
            "metadata_complete": hit.has_complete_recording_metadata,
            "artist_credits_complete": bool(metadata.artist_names),
            "credit_evidence": matching_credits(track.track_name, row.discogs_credits, hit),
        }))
    for uri, variants in by_uri.items():
        if not any(observation["credit_evidence"] is not None for _, observation in variants):
            continue
        hits = (hit for hit, _ in variants)
        conflicting = any(observations_conflict(left, right) for left, right in combinations(hits, 2))
        row.candidates.append({
            "uri": uri,
            "conflicting_observations": conflicting,
            "observations": [observation for _, observation in variants],
        })


def collect_candidates(
    args: argparse.Namespace, rows: Sequence[tuple[PlaylistTrack, ReviewRow]],
    cached_candidates: Mapping[PlaylistTrack, tuple[SpotifyTrackCandidate, ...]],
) -> None:
    client: SpotifyClient | None = None
    access_token = ""
    stopped = ""
    pages: dict[str, SearchPage] = {}
    for track, row in rows:
        credit = next(credit for credit in row.discogs_credits if credit.printed != credit.canonical)
        observations: dict[tuple[SpotifyTrackCandidate, SpotifyRecordingMetadata], list[str]] = {
            (hit, hit.recording_metadata()): ["cached_review"] for hit in cached_candidates[track]
        }
        for origin, artist_name in (("printed_search", credit.printed), ("canonical_search", credit.canonical)):
            query = build_spotify_track_search_query(replace(track, artist_name=artist_name, album_name=""))
            entry = {"origin": origin, "credit": artist_name, "query": query}
            row.queries.append(entry)
            if query not in pages and not stopped:
                if client is None:
                    try:
                        client, access_token = open_spotify_search(args)
                    except (SpotifyAuthenticationError, ValueError, OSError, http.client.HTTPException):
                        stopped = "Spotify authentication could not start. Check the OAuth settings or reauthorize."
                if client is not None:
                    page = search_first_page(client, access_token, query)
                    pages[query] = page
                    if page.terminal:
                        stopped = page.reason
            page = pages.get(query)
            if page is None:
                entry.update(status="not_run", reason=stopped)
            else:
                entry.update(status="incomplete" if page.reason else "complete", reason=page.reason)
                for hit in page.hits:
                    origins = observations.setdefault((hit, hit.recording_metadata()), [])
                    if origin not in origins:
                        origins.append(origin)
        report_candidates(track, row, observations)
        if any(query["status"] != "complete" for query in row.queries):
            row.status, row.reason = "incomplete", "At least one Spotify search was incomplete."
        else:
            row.status = "suggestions" if row.candidates else "no_suggestions"


def review_release(args: argparse.Namespace) -> list[ReviewRow]:
    tracks = read_playlist_tracks(args.playlist_output_directory)
    rows = source_groups(tracks, args.release_id)
    matches = load_spotify_track_match_cache(args.match_cache)
    cached_candidates: dict[PlaylistTrack, tuple[SpotifyTrackCandidate, ...]] = {}
    for track, row in rows:
        if row.status == "pending":
            cached_candidates[track] = assess_row(track, row, matches)
    pending = [(track, row) for track, row in rows if row.status == "pending"]
    if pending:
        token = os.environ.get("DISCOGS_TOKEN", "")
        rate_limiter = DiscogsRateLimiter(DEFAULT_REQUEST_INTERVAL_SECONDS, default_discogs_rate_limit(token))
        try:
            payload = fetch_release_payload(args.release_id, token, "DiscogsSpotifyTrackReview/1.0", 30, rate_limiter)
            bind_discogs_sources(payload, args.release_id, pending)
        except json.JSONDecodeError:
            for _, row in pending:
                row.status, row.reason = "source_unverified", "Discogs returned invalid JSON."
        except ValueError as error:
            for _, row in pending:
                row.status, row.reason = "source_unverified", str(error)
        except (RuntimeError, OSError, http.client.HTTPException):
            for _, row in pending:
                row.status, row.reason = "incomplete", "The Discogs pressing request failed."
        collect_candidates(args, [(track, row) for track, row in pending if row.status == "pending"], cached_candidates)
    return [row for _, row in rows]


def positive_release_id(value: str) -> str:
    digits = value.lstrip("0")
    if not (digits and value.isascii() and value.isdecimal()):
        raise argparse.ArgumentTypeError("release ID must be a positive integer")
    return digits


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", type=positive_release_id, required=True)
    parser.add_argument("--playlist-output-directory", type=Path, required=True)
    parser.add_argument("--match-cache", type=Path, default=DEFAULT_SPOTIFY_MATCH_CACHE_PATH)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH)
    parser.add_argument("--token-cache", type=Path, default=DEFAULT_TOKEN_CACHE_PATH)
    args = parser.parse_args(argv)
    try:
        rows = review_release(args)
    except (ValueError, OSError) as error:
        print(f"Spotify track match review: {error}", file=sys.stderr)
        return 1
    report = {
        "release_id": args.release_id,
        "search_scope": {"pages_per_query": 1, "max_hits_per_query": SEARCH_LIMIT},
        "status": "review_only" if rows else "no_generated_rows",
        "rows": [asdict(row) for row in rows],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(any(row.status == "incomplete" for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
