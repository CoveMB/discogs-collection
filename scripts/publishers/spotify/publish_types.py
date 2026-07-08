"""Shared Spotify playlist publishing data structures."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from publishers.spotify.matching import PlaylistTrack, SpotifyTrackCandidate, TrackMatchDecision


DEFAULT_MAX_NEW_SEARCHES_PER_RUN = 500
UNLIMITED_NEW_SEARCHES_PER_RUN = 0
APPEND_SYNC_MODE = "append"
REPLACE_SYNC_MODE = "replace"
PUBLISHER_SYNC_MODES = (APPEND_SYNC_MODE, REPLACE_SYNC_MODE)
ALREADY_PRESENT = "already_present"
WOULD_ADD = "would_add"
ADDED = "added"
WOULD_INCLUDE = "would_include"
INCLUDED = "included"
DUPLICATE_IN_SOURCE = "duplicate_in_source"
MATCH_SOURCE_CACHE = "cache"
MATCH_SOURCE_SEARCH = "search"

InfoLog = Callable[[str], None]


@dataclass(frozen=True)
class SpotifyDryRunSummary:
    playlist_count: int
    track_count: int
    matched_count: int
    ambiguous_count: int
    unmatched_count: int
    error_count: int
    report_path: Path
    decisions: tuple[TrackMatchDecision, ...]


@dataclass(frozen=True)
class PlaylistPublishDecision:
    playlist_name: str
    target_playlist_name: str
    track: PlaylistTrack
    status: str
    spotify_uri: str
    reason: str
    match_source: str
    candidate: SpotifyTrackCandidate | None = None
    review_candidates: tuple[SpotifyTrackCandidate, ...] = ()
    search_queries: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalPlaylistItem:
    playlist_name: str
    position: int
    status: str
    spotify_uri: str
    track_name: str
    artist_names: tuple[str, ...]
    album_name: str
    source_track: PlaylistTrack | None = None


@dataclass(frozen=True)
class PlaylistPublishContext:
    playlist_name: str
    target_playlist_name: str
    playlist_id: str
    existed: bool
    current_item_count: int
    info_message: str


@dataclass(frozen=True)
class SpotifyPublishSummary:
    playlist_count: int
    track_count: int
    run_status: str
    cache_hit_count: int
    search_count: int
    searched_row_count: int
    matched_count: int
    ambiguous_count: int
    unmatched_count: int
    error_count: int
    already_present_count: int
    would_add_count: int
    added_count: int
    would_include_count: int
    included_count: int
    duplicate_in_source_count: int
    report_path: Path
    apply: bool
    publisher_sync_mode: str
    decisions: tuple[PlaylistPublishDecision, ...]
    final_items: tuple[FinalPlaylistItem, ...]
    playlist_contexts: tuple[PlaylistPublishContext, ...]


@dataclass(frozen=True)
class SpotifyTrackSearchResult:
    decision: TrackMatchDecision
    search_count: int
