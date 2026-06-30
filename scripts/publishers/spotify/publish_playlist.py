#!/usr/bin/env python3
"""Spotify playlist publishing from generated playlist CSVs."""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path


SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[2]
if str(SCRIPTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.authorization_flow import DEFAULT_AUTHORIZE_SCOPES, authorize_spotify_interactively  # noqa: E402
from publishers.spotify.client import (  # noqa: E402
    SpotifyApiError,
    SpotifyClient,
    SpotifyPlaylist,
    SpotifyPlaylistItem,
    SpotifyRateLimitDeferredError,
)
from publishers.spotify.env import DEFAULT_ENV_PATH, DEFAULT_TOKEN_CACHE_PATH, load_spotify_settings  # noqa: E402
from publishers.spotify.match_cache import (  # noqa: E402
    cache_track_match,
    cached_track_match,
    load_spotify_track_match_cache,
    save_spotify_track_match_cache,
    utc_timestamp,
)
from publishers.spotify.matching import (  # noqa: E402
    AMBIGUOUS,
    ERROR,
    MATCHED,
    UNMATCHED,
    PlaylistTrack,
    TrackMatchDecision,
    SpotifyTrackCandidate,
    build_spotify_track_search_query,
    choose_best_track_match,
    normalize_music_text,
    normalized_artist_in_candidates,
    track_match_error,
)
from publishers.spotify.session import get_spotify_access_token  # noqa: E402
from shared.debug_log import DebugLog, build_debug_logger  # noqa: E402
from shared.files import read_csv_file  # noqa: E402
from shared.playlist_selection import resolve_playlist_master_paths  # noqa: E402
from shared.progress import ProgressReporter  # noqa: E402
from shared.publisher_config import (  # noqa: E402
    DEFAULT_PLAYLIST_PREFIX,
    DEFAULT_PLAYLIST_SUFFIX,
    DEFAULT_PUBLISHER,
    DEFAULT_PUBLISHER_CONFIG_PATH,
    PublisherConfig,
    load_or_create_publisher_config,
)
from shared.reports import format_report_section, format_report_title, script_report_path, write_text_report  # noqa: E402


DEFAULT_PLAYLIST_OUTPUT_DIRECTORY = Path("collection/playlists")
DEFAULT_MATCH_CACHE_PATH = Path("collection/cache/spotify-track-matches.cache.json")
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
PLAYLIST_DESCRIPTION = "Generated from Discogs collection"
TUNEMYMUSIC_COLUMNS = (
    "Release Id",
    "Album Name",
    "Track Number",
    "Track Name",
    "Artist Name",
    "Spotify Search Query",
)


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


InfoLog = Callable[[str], None]


def search_spotify_track(
    track: PlaylistTrack,
    spotify_client: SpotifyClient,
    access_token: str,
    search_limit: int,
) -> TrackMatchDecision:
    query = build_spotify_track_search_query(track)
    try:
        candidates = spotify_client.search_tracks(
            access_token=access_token,
            query=query,
            limit=search_limit,
        )
    except SpotifyRateLimitDeferredError:
        raise
    except SpotifyApiError as error:
        return track_match_error(track, str(error))
    return choose_best_track_match(track, candidates)


def dry_run_spotify_playlist_publish(
    playlist_output_directory: Path,
    report_path: Path,
    spotify_client: SpotifyClient,
    access_token: str,
    search_limit: int = 10,
    progress: ProgressReporter | None = None,
    playlist_selectors: Sequence[str] | None = None,
    playlist_master_paths: Sequence[Path] | None = None,
    playlist_names_by_master_path: Mapping[Path, str] | None = None,
    debug_log: DebugLog | None = None,
) -> SpotifyDryRunSummary:
    if playlist_selectors is not None and playlist_master_paths is not None:
        raise ValueError("playlist_selectors and playlist_master_paths cannot both be provided")
    if playlist_master_paths is not None:
        selected_master_paths = tuple(playlist_master_paths)
    else:
        selected_master_paths = resolve_playlist_master_paths(
            playlist_output_directory,
            playlist_selectors,
            allow_all_selector=False,
        )
    if debug_log:
        debug_log(f"dry_run_playlist_masters count={len(selected_master_paths)}")
    playlist_tracks = read_playlist_tracks_from_master_paths(
        selected_master_paths,
        playlist_names_by_master_path=playlist_names_by_master_path,
    )
    if debug_log:
        debug_log(f"loaded_playlist_tracks count={len(playlist_tracks)}")
    decisions: list[TrackMatchDecision] = []
    if progress:
        progress.start(len(playlist_tracks))
    try:
        for row_number, track in enumerate(playlist_tracks, start=1):
            try:
                if debug_log:
                    debug_log(f"track_search_start index={row_number} total={len(playlist_tracks)}")
                try:
                    decision = search_spotify_track(
                        track=track,
                        spotify_client=spotify_client,
                        access_token=access_token,
                        search_limit=search_limit,
                    )
                except SpotifyRateLimitDeferredError:
                    if debug_log:
                        debug_log(f"track_search_deferred index={row_number}")
                    raise
                decisions.append(decision)
                if debug_log:
                    debug_log(f"track_search_done index={row_number} status={decision.status}")
            finally:
                if progress:
                    progress.update(row_number)
    finally:
        if progress:
            progress.finish()

    summary = build_summary(tuple(decisions), report_path)
    write_dry_run_report(report_path, summary)
    return summary


def publish_spotify_playlists(
    playlist_output_directory: Path,
    report_path: Path,
    spotify_client: SpotifyClient,
    access_token: str,
    search_limit: int = 10,
    progress: ProgressReporter | None = None,
    playlist_selectors: Sequence[str] | None = None,
    playlist_master_paths: Sequence[Path] | None = None,
    playlist_names_by_master_path: Mapping[Path, str] | None = None,
    debug_log: DebugLog | None = None,
    match_cache_path: Path = DEFAULT_MATCH_CACHE_PATH,
    publisher_config: PublisherConfig | None = None,
    apply: bool = True,
    publisher_sync_mode: str = APPEND_SYNC_MODE,
    info_log: InfoLog | None = None,
    refresh_match_cache: bool = False,
) -> SpotifyPublishSummary:
    if publisher_sync_mode not in PUBLISHER_SYNC_MODES:
        raise ValueError(f"publisher_sync_mode must be one of: {', '.join(PUBLISHER_SYNC_MODES)}")
    if playlist_selectors is not None and playlist_master_paths is not None:
        raise ValueError("playlist_selectors and playlist_master_paths cannot both be provided")
    config = publisher_config or PublisherConfig(
        default_publisher=DEFAULT_PUBLISHER,
        playlist_prefix=DEFAULT_PLAYLIST_PREFIX,
        playlist_suffix=DEFAULT_PLAYLIST_SUFFIX,
    )
    selected_master_paths = tuple(playlist_master_paths) if playlist_master_paths is not None else resolve_playlist_master_paths(
        playlist_output_directory,
        playlist_selectors,
        allow_all_selector=False,
    )
    playlist_name_overrides = normalize_playlist_names_by_master_path(playlist_names_by_master_path)
    match_cache = load_spotify_track_match_cache(match_cache_path)
    timestamp = utc_timestamp()
    spotify_playlists = spotify_client.list_current_user_playlists(access_token=access_token)
    current_user_id = spotify_client.get_current_user_id(access_token=access_token)
    decisions: list[PlaylistPublishDecision] = []
    final_items: list[FinalPlaylistItem] = []
    playlist_contexts: list[PlaylistPublishContext] = []
    planned_write_uris_by_playlist: dict[str, tuple[str, ...]] = {}
    tracks_by_master_path = {
        playlist_master_path: read_playlist_tracks_from_master_paths(
            (playlist_master_path,),
            playlist_names_by_master_path=playlist_name_overrides,
        )
        for playlist_master_path in selected_master_paths
    }
    total_tracks = sum(len(tracks) for tracks in tracks_by_master_path.values())
    search_count = 0
    cache_hit_count = 0
    if progress:
        progress.start(total_tracks)
    processed_track_count = 0
    try:
        for playlist_master_path, playlist_tracks in tracks_by_master_path.items():
            playlist_name = playlist_name_for_master_path(playlist_master_path, playlist_name_overrides)
            target_playlist_name = publisher_playlist_name(playlist_name, config)
            context, existing_items, spotify_playlists = resolve_playlist_context(
                spotify_client=spotify_client,
                access_token=access_token,
                spotify_playlists=spotify_playlists,
                current_user_id=current_user_id,
                playlist_name=playlist_name,
                target_playlist_name=target_playlist_name,
                apply=apply,
                info_log=info_log,
            )
            playlist_contexts.append(context)
            if publisher_sync_mode == APPEND_SYNC_MODE:
                final_items.extend(existing_final_playlist_items(target_playlist_name, existing_items))
            planned_write_uris: list[str] = []
            seen_source_identity_keys: set[str] = set()
            existing_identity_keys = {
                spotify_playlist_item_identity_key(target_playlist_name, item)
                for item in existing_items
            }
            existing_incomplete_spotify_uris = incomplete_spotify_playlist_item_uris(existing_items)
            for track in playlist_tracks:
                processed_track_count += 1
                try:
                    decision, match_source = resolve_track_match(
                        track=track,
                        spotify_client=spotify_client,
                        access_token=access_token,
                        search_limit=search_limit,
                        match_cache=match_cache,
                        timestamp=timestamp,
                        refresh_match_cache=refresh_match_cache,
                    )
                    if match_source == MATCH_SOURCE_CACHE:
                        cache_hit_count += 1
                    elif match_source == MATCH_SOURCE_SEARCH:
                        search_count += 1
                    publish_decision = build_publish_decision(
                        playlist_name=playlist_name,
                        target_playlist_name=target_playlist_name,
                        decision=decision,
                        match_source=match_source,
                        publisher_sync_mode=publisher_sync_mode,
                        apply=False,
                        existing_identity_keys=existing_identity_keys,
                        existing_incomplete_spotify_uris=existing_incomplete_spotify_uris,
                        seen_source_identity_keys=seen_source_identity_keys,
                    )
                    decisions.append(publish_decision)
                    identity_key = publish_decision_identity_key(publish_decision)
                    if publish_decision.status in {WOULD_ADD, ADDED, WOULD_INCLUDE, INCLUDED}:
                        seen_source_identity_keys.add(identity_key)
                        planned_write_uris.append(publish_decision.spotify_uri)
                        final_items.append(final_playlist_item_from_decision(len(final_items) + 1, publish_decision))
                    elif identity_key and publish_decision.status in {ALREADY_PRESENT, DUPLICATE_IN_SOURCE}:
                        seen_source_identity_keys.add(identity_key)
                    if match_source == MATCH_SOURCE_SEARCH:
                        cache_track_match(match_cache, decision, matched_at=timestamp)
                    if debug_log:
                        debug_log(f"publish_track_done index={processed_track_count} status={publish_decision.status} source={match_source}")
                finally:
                    if progress:
                        progress.update(processed_track_count)

            planned_write_uris_by_playlist[target_playlist_name] = tuple(planned_write_uris)
    except SpotifyRateLimitDeferredError:
        save_spotify_track_match_cache(match_cache_path, match_cache)
        write_publish_summary(
            decisions=tuple(decisions),
            final_items=tuple(reindex_final_items(final_items)),
            playlist_contexts=tuple(playlist_contexts),
            report_path=report_path,
            apply=apply,
            publisher_sync_mode=publisher_sync_mode,
            cache_hit_count=cache_hit_count,
            search_count=search_count,
            run_status=(
                f"aborted - Spotify rate limit deferred after {len(decisions)} of {total_tracks} tracks; "
                "no playlist writes were attempted"
            ),
        )
        raise
    finally:
        if progress:
            progress.finish()

    save_spotify_track_match_cache(match_cache_path, match_cache)
    summary = write_publish_summary(
        decisions=tuple(decisions),
        final_items=tuple(reindex_final_items(final_items)),
        playlist_contexts=tuple(playlist_contexts),
        report_path=report_path,
        apply=apply,
        publisher_sync_mode=publisher_sync_mode,
        cache_hit_count=cache_hit_count,
        search_count=search_count,
    )
    if apply:
        validate_replace_apply_decisions(tuple(decisions), publisher_sync_mode)
        decisions, final_items, playlist_contexts, spotify_playlists = apply_planned_playlist_writes(
            spotify_client=spotify_client,
            access_token=access_token,
            decisions=tuple(decisions),
            final_items=tuple(final_items),
            playlist_contexts=tuple(playlist_contexts),
            planned_write_uris_by_playlist=planned_write_uris_by_playlist,
            spotify_playlists=spotify_playlists,
            publisher_sync_mode=publisher_sync_mode,
            report_path=report_path,
            cache_path=match_cache_path,
            match_cache=match_cache,
            current_user_id=current_user_id,
            cache_hit_count=cache_hit_count,
            search_count=search_count,
        )
        summary = write_publish_summary(
            decisions=tuple(decisions),
            final_items=tuple(reindex_final_items(final_items)),
            playlist_contexts=tuple(playlist_contexts),
            report_path=report_path,
            apply=apply,
            publisher_sync_mode=publisher_sync_mode,
            cache_hit_count=cache_hit_count,
            search_count=search_count,
        )
    return summary


def resolve_track_match(
    track: PlaylistTrack,
    spotify_client: SpotifyClient,
    access_token: str,
    search_limit: int,
    match_cache: dict[str, dict[str, object]],
    timestamp: str,
    refresh_match_cache: bool = False,
) -> tuple[TrackMatchDecision, str]:
    if not refresh_match_cache:
        cached_match = cached_track_match(track, match_cache, seen_at=timestamp)
        if cached_match:
            return cached_match.decision, MATCH_SOURCE_CACHE
    return search_spotify_track(
        track=track,
        spotify_client=spotify_client,
        access_token=access_token,
        search_limit=search_limit,
    ), MATCH_SOURCE_SEARCH


def build_publish_decision(
    playlist_name: str,
    target_playlist_name: str,
    decision: TrackMatchDecision,
    match_source: str,
    publisher_sync_mode: str,
    apply: bool,
    existing_identity_keys: set[str],
    existing_incomplete_spotify_uris: set[str],
    seen_source_identity_keys: set[str],
) -> PlaylistPublishDecision:
    if decision.status != MATCHED:
        return PlaylistPublishDecision(
            playlist_name=playlist_name,
            target_playlist_name=target_playlist_name,
            track=decision.track,
            status=decision.status,
            spotify_uri=decision.spotify_uri,
            reason=decision.reason,
            match_source=match_source,
            candidate=decision.candidate,
        )
    identity_key = track_match_decision_identity_key(target_playlist_name, decision)
    if identity_key in seen_source_identity_keys:
        status = DUPLICATE_IN_SOURCE
        reason = "Spotify artist, album, and track already planned from an earlier local row"
    elif publisher_sync_mode == APPEND_SYNC_MODE and identity_key in existing_identity_keys:
        status = ALREADY_PRESENT
        reason = "Spotify artist, album, and track already exist in playlist"
    elif publisher_sync_mode == APPEND_SYNC_MODE and decision.spotify_uri in existing_incomplete_spotify_uris:
        status = ALREADY_PRESENT
        reason = "Spotify URI already exists in playlist with incomplete metadata"
    elif publisher_sync_mode == APPEND_SYNC_MODE:
        status = ADDED if apply else WOULD_ADD
        reason = "Spotify artist, album, and track will be appended to playlist"
    else:
        status = INCLUDED if apply else WOULD_INCLUDE
        reason = "Spotify artist, album, and track will be included in replacement playlist"
    return PlaylistPublishDecision(
        playlist_name=playlist_name,
        target_playlist_name=target_playlist_name,
        track=decision.track,
        status=status,
        spotify_uri=decision.spotify_uri,
        reason=reason,
        match_source=match_source,
        candidate=decision.candidate,
    )


def validate_replace_apply_decisions(
    decisions: Sequence[PlaylistPublishDecision],
    publisher_sync_mode: str,
) -> None:
    if publisher_sync_mode != REPLACE_SYNC_MODE:
        return
    blocking_decisions = [
        decision
        for decision in decisions
        if decision.status in {AMBIGUOUS, UNMATCHED, ERROR}
    ]
    if not blocking_decisions:
        return
    blocking_counts = ", ".join(
        f"{status}={sum(1 for decision in blocking_decisions if decision.status == status)}"
        for status in (ERROR, AMBIGUOUS, UNMATCHED)
        if any(decision.status == status for decision in blocking_decisions)
    )
    raise ValueError(
        "replace mode aborted before writing because not every source row resolved "
        f"to a publishable Spotify URI ({blocking_counts})"
    )


def resolve_playlist_context(
    spotify_client: SpotifyClient,
    access_token: str,
    spotify_playlists: Sequence[SpotifyPlaylist],
    current_user_id: str,
    playlist_name: str,
    target_playlist_name: str,
    apply: bool,
    info_log: InfoLog | None,
) -> tuple[PlaylistPublishContext, tuple[SpotifyPlaylistItem, ...], tuple[SpotifyPlaylist, ...]]:
    playlist = find_owned_private_spotify_playlist_by_name(
        spotify_playlists,
        target_playlist_name,
        current_user_id=current_user_id,
    )
    if playlist:
        existing_items = spotify_client.get_playlist_items(access_token=access_token, playlist_id=playlist.playlist_id)
        info_message = f"Playlist {target_playlist_name} already exists with {len(existing_items)} songs, updating"
        emit_info(info_log, info_message)
        return (
            PlaylistPublishContext(
                playlist_name=playlist_name,
                target_playlist_name=target_playlist_name,
                playlist_id=playlist.playlist_id,
                existed=True,
                current_item_count=len(existing_items),
                info_message=info_message,
            ),
            existing_items,
            tuple(spotify_playlists),
        )
    action = "creating" if apply else "would create"
    info_message = f"Playlist {target_playlist_name} does not exist, {action}"
    emit_info(info_log, info_message)
    return (
        PlaylistPublishContext(
            playlist_name=playlist_name,
            target_playlist_name=target_playlist_name,
            playlist_id="",
            existed=False,
            current_item_count=0,
            info_message=info_message,
        ),
        (),
        tuple(spotify_playlists),
    )


def apply_playlist_writes(
    spotify_client: SpotifyClient,
    access_token: str,
    context: PlaylistPublishContext,
    planned_write_uris: tuple[str, ...],
    publisher_sync_mode: str,
) -> PlaylistPublishContext:
    playlist_id = context.playlist_id
    if not playlist_id and planned_write_uris:
        playlist = spotify_client.create_playlist(
            access_token=access_token,
            name=context.target_playlist_name,
            public=False,
            description=PLAYLIST_DESCRIPTION,
        )
        playlist_id = playlist.playlist_id
    if not playlist_id:
        return context
    if publisher_sync_mode == APPEND_SYNC_MODE:
        spotify_client.add_playlist_items(
            access_token=access_token,
            playlist_id=playlist_id,
            uris=planned_write_uris,
        )
    else:
        first_batch = planned_write_uris[:100]
        remaining_batches = planned_write_uris[100:]
        spotify_client.replace_playlist_items(
            access_token=access_token,
            playlist_id=playlist_id,
            uris=first_batch,
        )
        try:
            spotify_client.add_playlist_items(
                access_token=access_token,
                playlist_id=playlist_id,
                uris=remaining_batches,
            )
        except (SpotifyApiError, ValueError) as error:
            raise ValueError(
                f"replace mode partially replaced {context.target_playlist_name}; "
                f"sent first {len(first_batch)} URI(s), but appending remaining "
                f"{len(remaining_batches)} URI(s) failed: {error}"
            ) from error
    return PlaylistPublishContext(
        playlist_name=context.playlist_name,
        target_playlist_name=context.target_playlist_name,
        playlist_id=playlist_id,
        existed=context.existed,
        current_item_count=context.current_item_count,
        info_message=context.info_message,
    )


def find_owned_private_spotify_playlist_by_name(
    playlists: Sequence[SpotifyPlaylist],
    name: str,
    current_user_id: str,
) -> SpotifyPlaylist | None:
    matches = [
        playlist
        for playlist in playlists
        if playlist.name == name and playlist.owner_id == current_user_id
    ]
    if len(matches) > 1:
        raise ValueError(f"multiple Spotify playlists named {name}; rename duplicates before publishing")
    if not matches:
        return None
    playlist = matches[0]
    validate_existing_spotify_playlist_target(playlist)
    return playlist


def validate_existing_spotify_playlist_target(playlist: SpotifyPlaylist) -> None:
    if playlist.public is True:
        raise ValueError(
            f"{playlist.name}: public Spotify playlist publishing is not supported; "
            "make the playlist private or choose a different publisher target"
        )
    if playlist.collaborative:
        raise ValueError(
            f"{playlist.name}: collaborative Spotify playlist publishing is not supported; "
            "choose an owned private publisher target"
        )


def publisher_playlist_name(playlist_name: str, config: PublisherConfig) -> str:
    return f"{config.playlist_prefix}{playlist_name}{config.playlist_suffix}"


def emit_info(info_log: InfoLog | None, message: str) -> None:
    if info_log:
        info_log(message)


def read_playlist_tracks(playlist_output_directory: Path, playlist_selectors: Sequence[str] | None = None) -> tuple[PlaylistTrack, ...]:
    playlist_master_paths = resolve_playlist_master_paths(
        playlist_output_directory,
        playlist_selectors,
        allow_all_selector=False,
    )
    return read_playlist_tracks_from_master_paths(playlist_master_paths)


def read_playlist_tracks_from_master_paths(
    playlist_master_paths: Sequence[Path],
    playlist_names_by_master_path: Mapping[Path, str] | None = None,
) -> tuple[PlaylistTrack, ...]:
    playlist_name_overrides = normalize_playlist_names_by_master_path(playlist_names_by_master_path)
    tracks: list[PlaylistTrack] = []
    for playlist_path in playlist_master_paths:
        playlist_name = playlist_name_for_master_path(playlist_path, playlist_name_overrides)
        rows, fieldnames = read_csv_file(playlist_path)
        validate_playlist_fieldnames(playlist_path, fieldnames)
        for row in rows:
            tracks.append(
                PlaylistTrack(
                    playlist_name=playlist_name,
                    release_id=clean_cell(row.get("Release Id")),
                    album_name=clean_cell(row.get("Album Name")),
                    track_number=clean_cell(row.get("Track Number")),
                    track_name=clean_cell(row.get("Track Name")),
                    artist_name=clean_cell(row.get("Artist Name")),
                    spotify_search_query=clean_cell(row.get("Spotify Search Query")),
                )
            )
    return tuple(tracks)


def normalize_playlist_names_by_master_path(
    playlist_names_by_master_path: Mapping[Path, str] | None,
) -> dict[Path, str]:
    if not playlist_names_by_master_path:
        return {}
    names_by_path: dict[Path, str] = {}
    for path, playlist_name in playlist_names_by_master_path.items():
        clean_playlist_name = clean_cell(playlist_name)
        if clean_playlist_name:
            names_by_path[Path(path).resolve()] = clean_playlist_name
    return names_by_path


def playlist_name_for_master_path(
    playlist_master_path: Path,
    playlist_names_by_master_path: Mapping[Path, str],
) -> str:
    return playlist_names_by_master_path.get(playlist_master_path.resolve(), playlist_master_path.parent.name)


def validate_playlist_fieldnames(path: Path, fieldnames: Sequence[str]) -> None:
    missing_columns = [column for column in TUNEMYMUSIC_COLUMNS if column not in fieldnames]
    if missing_columns:
        raise ValueError(f"{path}: missing playlist CSV columns: {', '.join(missing_columns)}")


def existing_final_playlist_items(playlist_name: str, existing_items: Sequence[SpotifyPlaylistItem]) -> tuple[FinalPlaylistItem, ...]:
    return tuple(
        FinalPlaylistItem(
            playlist_name=playlist_name,
            position=index,
            status="existing",
            spotify_uri=item.uri,
            track_name=item.name,
            artist_names=item.artists,
            album_name=item.album_name,
            source_track=None,
        )
        for index, item in enumerate(existing_items, start=1)
    )


def spotify_playlist_item_identity_key(target_playlist_name: str, item: SpotifyPlaylistItem) -> str:
    return spotify_track_identity_key(
        target_playlist_name=target_playlist_name,
        artist_names=item.artists,
        album_name=item.album_name,
        track_name=item.name,
    )


def incomplete_spotify_playlist_item_uris(existing_items: Sequence[SpotifyPlaylistItem]) -> set[str]:
    return {
        item.uri
        for item in existing_items
        if item.uri and not spotify_playlist_item_has_identity_metadata(item)
    }


def spotify_playlist_item_has_identity_metadata(item: SpotifyPlaylistItem) -> bool:
    return bool(
        clean_cell(item.name)
        and clean_cell(item.album_name)
        and normalized_identity_artists(item.artists)
    )


def track_match_decision_identity_key(target_playlist_name: str, decision: TrackMatchDecision) -> str:
    candidate = decision.candidate
    if candidate:
        return spotify_candidate_identity_key(target_playlist_name, candidate)
    return spotify_track_identity_key(
        target_playlist_name=target_playlist_name,
        artist_names=(decision.track.artist_name,),
        album_name=decision.track.album_name,
        track_name=decision.track.track_name,
    )


def publish_decision_identity_key(decision: PlaylistPublishDecision) -> str:
    candidate = decision.candidate
    if candidate:
        return spotify_candidate_identity_key(decision.target_playlist_name, candidate)
    return spotify_track_identity_key(
        target_playlist_name=decision.target_playlist_name,
        artist_names=(decision.track.artist_name,),
        album_name=decision.track.album_name,
        track_name=decision.track.track_name,
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
            normalize_music_text(target_playlist_name),
            " ".join(normalized_artists),
            normalize_music_text(album_name),
            normalize_music_text(track_name),
        )
    )


def normalized_identity_artists(artist_names: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            normalized_artist
            for normalized_artist in (normalize_music_text(artist_name) for artist_name in artist_names)
            if normalized_artist
        )
    )


def final_playlist_item_from_decision(position: int, decision: PlaylistPublishDecision) -> FinalPlaylistItem:
    candidate = decision.candidate
    return FinalPlaylistItem(
        playlist_name=decision.target_playlist_name,
        position=position,
        status=decision.status,
        spotify_uri=decision.spotify_uri,
        track_name=candidate.name if candidate else decision.track.track_name,
        artist_names=candidate.artists if candidate else (decision.track.artist_name,),
        album_name=candidate.album_name if candidate else decision.track.album_name,
        source_track=decision.track,
    )


def reindex_final_items(final_items: Sequence[FinalPlaylistItem]) -> tuple[FinalPlaylistItem, ...]:
    positions_by_playlist: dict[str, int] = {}
    reindexed_items: list[FinalPlaylistItem] = []
    for item in final_items:
        next_position = positions_by_playlist.get(item.playlist_name, 0) + 1
        positions_by_playlist[item.playlist_name] = next_position
        reindexed_items.append(
            FinalPlaylistItem(
                playlist_name=item.playlist_name,
                position=next_position,
                status=item.status,
                spotify_uri=item.spotify_uri,
                track_name=item.track_name,
                artist_names=item.artist_names,
                album_name=item.album_name,
                source_track=item.source_track,
            )
        )
    return tuple(reindexed_items)


def build_publish_summary(
    decisions: tuple[PlaylistPublishDecision, ...],
    final_items: tuple[FinalPlaylistItem, ...],
    playlist_contexts: tuple[PlaylistPublishContext, ...],
    report_path: Path,
    apply: bool,
    publisher_sync_mode: str,
    cache_hit_count: int,
    search_count: int,
    run_status: str = "complete",
) -> SpotifyPublishSummary:
    playlist_names = {context.target_playlist_name for context in playlist_contexts}
    return SpotifyPublishSummary(
        playlist_count=len(playlist_names),
        track_count=len(decisions),
        run_status=run_status,
        cache_hit_count=cache_hit_count,
        search_count=search_count,
        matched_count=sum(1 for decision in decisions if decision.spotify_uri),
        ambiguous_count=sum(1 for decision in decisions if decision.status == AMBIGUOUS),
        unmatched_count=sum(1 for decision in decisions if decision.status == UNMATCHED),
        error_count=sum(1 for decision in decisions if decision.status == ERROR),
        already_present_count=sum(1 for decision in decisions if decision.status == ALREADY_PRESENT),
        would_add_count=sum(1 for decision in decisions if decision.status == WOULD_ADD),
        added_count=sum(1 for decision in decisions if decision.status == ADDED),
        would_include_count=sum(1 for decision in decisions if decision.status == WOULD_INCLUDE),
        included_count=sum(1 for decision in decisions if decision.status == INCLUDED),
        duplicate_in_source_count=sum(1 for decision in decisions if decision.status == DUPLICATE_IN_SOURCE),
        report_path=report_path,
        apply=apply,
        publisher_sync_mode=publisher_sync_mode,
        decisions=decisions,
        final_items=final_items,
        playlist_contexts=playlist_contexts,
    )


def write_publish_summary(
    decisions: tuple[PlaylistPublishDecision, ...],
    final_items: tuple[FinalPlaylistItem, ...],
    playlist_contexts: tuple[PlaylistPublishContext, ...],
    report_path: Path,
    apply: bool,
    publisher_sync_mode: str,
    cache_hit_count: int,
    search_count: int,
    run_status: str = "complete",
) -> SpotifyPublishSummary:
    summary = build_publish_summary(
        decisions=decisions,
        final_items=final_items,
        playlist_contexts=playlist_contexts,
        report_path=report_path,
        apply=apply,
        publisher_sync_mode=publisher_sync_mode,
        cache_hit_count=cache_hit_count,
        search_count=search_count,
        run_status=run_status,
    )
    write_publish_report(report_path, summary)
    return summary


def apply_planned_playlist_writes(
    spotify_client: SpotifyClient,
    access_token: str,
    decisions: tuple[PlaylistPublishDecision, ...],
    final_items: tuple[FinalPlaylistItem, ...],
    playlist_contexts: tuple[PlaylistPublishContext, ...],
    planned_write_uris_by_playlist: Mapping[str, tuple[str, ...]],
    spotify_playlists: Sequence[SpotifyPlaylist],
    publisher_sync_mode: str,
    report_path: Path,
    cache_path: Path,
    match_cache: Mapping[str, Mapping[str, object]],
    current_user_id: str,
    cache_hit_count: int,
    search_count: int,
) -> tuple[
    tuple[PlaylistPublishDecision, ...],
    tuple[FinalPlaylistItem, ...],
    tuple[PlaylistPublishContext, ...],
    tuple[SpotifyPlaylist, ...],
]:
    current_decisions = tuple(decisions)
    current_final_items = tuple(final_items)
    current_contexts = list(playlist_contexts)
    current_playlists = tuple(spotify_playlists)
    for index, context in enumerate(current_contexts):
        try:
            applied_context = apply_playlist_writes(
                spotify_client=spotify_client,
                access_token=access_token,
                context=context,
                planned_write_uris=planned_write_uris_by_playlist.get(context.target_playlist_name, ()),
                publisher_sync_mode=publisher_sync_mode,
            )
        except (SpotifyApiError, ValueError) as error:
            current_contexts[index] = replace(
                context,
                info_message=f"{context.info_message}; publishing failed: {display_report_value(error)}",
            )
            save_spotify_track_match_cache(cache_path, match_cache)
            write_publish_summary(
                decisions=current_decisions,
                final_items=tuple(reindex_final_items(current_final_items)),
                playlist_contexts=tuple(current_contexts),
                report_path=report_path,
                apply=True,
                publisher_sync_mode=publisher_sync_mode,
                cache_hit_count=cache_hit_count,
                search_count=search_count,
                run_status=f"failed - publishing stopped while writing {context.target_playlist_name}",
            )
            raise
        current_contexts[index] = applied_context
        current_decisions = mark_playlist_decisions_applied(current_decisions, context.target_playlist_name)
        current_final_items = mark_final_items_applied(current_final_items, context.target_playlist_name)
        current_playlists = update_spotify_playlist_inventory(current_playlists, applied_context, current_user_id)
        save_spotify_track_match_cache(cache_path, match_cache)
        write_publish_summary(
            decisions=current_decisions,
            final_items=tuple(reindex_final_items(current_final_items)),
            playlist_contexts=tuple(current_contexts),
            report_path=report_path,
            apply=True,
            publisher_sync_mode=publisher_sync_mode,
            cache_hit_count=cache_hit_count,
            search_count=search_count,
        )
    return current_decisions, current_final_items, tuple(current_contexts), current_playlists


def mark_playlist_decisions_applied(
    decisions: Sequence[PlaylistPublishDecision],
    target_playlist_name: str,
) -> tuple[PlaylistPublishDecision, ...]:
    return tuple(
        replace(decision, status=applied_publish_status(decision.status))
        if decision.target_playlist_name == target_playlist_name
        else decision
        for decision in decisions
    )


def mark_final_items_applied(
    final_items: Sequence[FinalPlaylistItem],
    target_playlist_name: str,
) -> tuple[FinalPlaylistItem, ...]:
    return tuple(
        replace(item, status=applied_publish_status(item.status))
        if item.playlist_name == target_playlist_name
        else item
        for item in final_items
    )


def applied_publish_status(status: str) -> str:
    if status == WOULD_ADD:
        return ADDED
    if status == WOULD_INCLUDE:
        return INCLUDED
    return status


def update_spotify_playlist_inventory(
    spotify_playlists: Sequence[SpotifyPlaylist],
    context: PlaylistPublishContext,
    current_user_id: str,
) -> tuple[SpotifyPlaylist, ...]:
    if not context.playlist_id:
        return tuple(spotify_playlists)
    return tuple(
        playlist
        for playlist in spotify_playlists
        if playlist.name != context.target_playlist_name
    ) + (
        SpotifyPlaylist(
            playlist_id=context.playlist_id,
            name=context.target_playlist_name,
            url="",
            owner_id=current_user_id,
            public=False,
            collaborative=False,
        ),
    )


def write_publish_report(path: Path, summary: SpotifyPublishSummary) -> None:
    title = "Spotify playlist publish report\n" if summary.apply else "Spotify playlist publish dry-run report\n"
    lines = format_report_title(title)
    lines.extend(
        format_report_section(
            "Summary",
            [
                f"- Run status: {summary.run_status}",
                f"- Publisher sync mode: {summary.publisher_sync_mode}",
                f"- Playlists: {summary.playlist_count}",
                f"- Tracks: {summary.track_count}",
                f"- Cache hits: {summary.cache_hit_count}",
                f"- Spotify searches: {summary.search_count}",
                f"- Cached matched tracks: {cached_matched_publish_decision_count(summary.decisions)}",
                f"- Cached ambiguous tracks: {count_publish_decisions(summary.decisions, MATCH_SOURCE_CACHE, {AMBIGUOUS})}",
                f"- Cached unmatched tracks: {count_publish_decisions(summary.decisions, MATCH_SOURCE_CACHE, {UNMATCHED})}",
                f"- Searched matched tracks: {searched_matched_publish_decision_count(summary.decisions)}",
                f"- Searched ambiguous tracks: {count_publish_decisions(summary.decisions, MATCH_SOURCE_SEARCH, {AMBIGUOUS})}",
                f"- Searched unmatched tracks: {count_publish_decisions(summary.decisions, MATCH_SOURCE_SEARCH, {UNMATCHED})}",
                f"- Searched error tracks: {count_publish_decisions(summary.decisions, MATCH_SOURCE_SEARCH, {ERROR})}",
                f"- Matched tracks: {summary.matched_count}",
                f"- Already-present tracks: {summary.already_present_count}",
                f"- Would add tracks: {summary.would_add_count}",
                f"- Added tracks: {summary.added_count}",
                f"- Tracks that would be included in replacement: {summary.would_include_count}",
                f"- Tracks included in replacement: {summary.included_count}",
                f"- Duplicate source tracks skipped: {summary.duplicate_in_source_count}",
                f"- Ambiguous tracks: {summary.ambiguous_count}",
                f"- Unmatched tracks: {summary.unmatched_count}",
                f"- Search errors: {summary.error_count}",
            ],
        )
    )
    lines.extend(
        format_report_section(
            "Playlist checks",
            [f"- {context.info_message}" for context in summary.playlist_contexts] or ["- None"],
        )
    )
    lines.extend(
        format_report_section(
            "Already-present tracks",
            format_publish_decisions(summary.decisions, {ALREADY_PRESENT}),
        )
    )
    lines.extend(
        format_report_section(
            "Tracks that would be added",
            format_publish_decisions(summary.decisions, {WOULD_ADD}),
        )
    )
    lines.extend(
        format_report_section(
            "Tracks added",
            format_publish_decisions(summary.decisions, {ADDED}),
        )
    )
    lines.extend(
        format_report_section(
            "Tracks that would be included in replacement",
            format_publish_decisions(summary.decisions, {WOULD_INCLUDE}),
        )
    )
    lines.extend(
        format_report_section(
            "Tracks included in replacement",
            format_publish_decisions(summary.decisions, {INCLUDED}),
        )
    )
    lines.extend(
        format_report_section(
            "Duplicate source tracks skipped",
            format_publish_decisions(summary.decisions, {DUPLICATE_IN_SOURCE}),
        )
    )
    lines.extend(
        format_report_section(
            "Ambiguous tracks needing review",
            flatten_report_details(format_publish_review_details(decision) for decision in summary.decisions if decision.status == AMBIGUOUS),
        )
    )
    lines.extend(
        format_report_section(
            "Unmatched tracks needing review",
            flatten_report_details(format_publish_review_details(decision) for decision in summary.decisions if decision.status == UNMATCHED),
        )
    )
    lines.extend(
        format_report_section(
            "Search errors",
            flatten_report_details(format_publish_review_details(decision) for decision in summary.decisions if decision.status == ERROR),
        )
    )
    lines.extend(
        format_report_section(
            "Final planned playlist state",
            [format_final_playlist_item(item) for item in summary.final_items] or ["- None"],
        )
    )
    lines.extend(
        format_report_section(
            "Track publish decisions",
            [format_publish_decision(decision) for decision in summary.decisions] or ["- None"],
        )
    )
    write_text_report(path, lines)


def count_publish_decisions(
    decisions: Sequence[PlaylistPublishDecision],
    match_source: str,
    statuses: set[str],
) -> int:
    return sum(1 for decision in decisions if decision.match_source == match_source and decision.status in statuses)


def cached_matched_publish_decision_count(decisions: Sequence[PlaylistPublishDecision]) -> int:
    return sum(1 for decision in decisions if decision.match_source == MATCH_SOURCE_CACHE and bool(decision.spotify_uri))


def searched_matched_publish_decision_count(decisions: Sequence[PlaylistPublishDecision]) -> int:
    return sum(1 for decision in decisions if decision.match_source == MATCH_SOURCE_SEARCH and bool(decision.spotify_uri))


def format_publish_decisions(decisions: Sequence[PlaylistPublishDecision], statuses: set[str]) -> list[str]:
    return [format_publish_decision(decision) for decision in decisions if decision.status in statuses] or ["- None"]


def format_publish_decision(decision: PlaylistPublishDecision) -> str:
    track = decision.track
    return (
        f"- {decision.target_playlist_name} | {track.release_id} | {track.track_number} | "
        f"{track.artist_name} | {track.track_name} | {decision.status} | "
        f"{decision.spotify_uri or 'no Spotify URI'} | {display_report_value(decision.reason)}"
    )


def format_publish_review_details(decision: PlaylistPublishDecision) -> list[str]:
    return [
        f"- {decision.target_playlist_name} | {format_track_context(decision.track)}",
        f"  Search query: {format_search_query(decision.track)}",
        f"  Why: {display_report_value(decision.reason)}",
    ]


def format_final_playlist_item(item: FinalPlaylistItem) -> str:
    return (
        f"- {item.playlist_name} | {item.position} | {item.status} | "
        f"{format_artist_names(item.artist_names)} | {display_report_value(item.track_name)} | "
        f"{display_report_value(item.album_name)} | {item.spotify_uri or 'no Spotify URI'}"
    )


def format_artist_names(artist_names: Sequence[str]) -> str:
    return display_report_value(", ".join(artist_names))


def build_summary(decisions: tuple[TrackMatchDecision, ...], report_path: Path) -> SpotifyDryRunSummary:
    playlist_names = {decision.track.playlist_name for decision in decisions}
    return SpotifyDryRunSummary(
        playlist_count=len(playlist_names),
        track_count=len(decisions),
        matched_count=sum(1 for decision in decisions if decision.status == MATCHED),
        ambiguous_count=sum(1 for decision in decisions if decision.status == AMBIGUOUS),
        unmatched_count=sum(1 for decision in decisions if decision.status == UNMATCHED),
        error_count=sum(1 for decision in decisions if decision.status == ERROR),
        report_path=report_path,
        decisions=decisions,
    )


def write_dry_run_report(path: Path, summary: SpotifyDryRunSummary) -> None:
    lines = format_report_title("Spotify playlist dry-run report")
    lines.extend(
        format_report_section(
            "Summary",
            [
                f"- Playlists: {summary.playlist_count}",
                f"- Tracks: {summary.track_count}",
                f"- Matched tracks: {summary.matched_count}",
                f"- Ambiguous tracks: {summary.ambiguous_count}",
                f"- Unmatched tracks: {summary.unmatched_count}",
                f"- Search errors: {summary.error_count}",
            ],
        )
    )
    lines.extend(
        format_report_section(
            "Ambiguous tracks needing review",
            flatten_report_details(format_ambiguous_track_details(decision) for decision in summary.decisions if decision.status == AMBIGUOUS),
        )
    )
    lines.extend(
        format_report_section(
            "Unmatched tracks needing review",
            flatten_report_details(format_unmatched_track_details(decision) for decision in summary.decisions if decision.status == UNMATCHED),
        )
    )
    lines.extend(
        format_report_section(
            "Search errors",
            flatten_report_details(format_search_error_details(decision) for decision in summary.decisions if decision.status == ERROR),
        )
    )
    lines.extend(
        format_report_section(
            "Track match decisions",
            [format_match_decision(decision) for decision in summary.decisions] or ["- None"],
        )
    )
    write_text_report(path, lines)


def format_match_decision(decision: TrackMatchDecision) -> str:
    track = decision.track
    return (
        f"- {track.playlist_name} | {track.release_id} | {track.track_number} | "
        f"{track.artist_name} | {track.track_name} | {decision.status} | "
        f"{decision.spotify_uri or 'no Spotify URI'} | {display_report_value(decision.reason)}"
    )


def flatten_report_details(detail_groups: Iterable[list[str]]) -> list[str]:
    lines: list[str] = []
    for detail_group in detail_groups:
        if lines:
            lines.append("")
        lines.extend(detail_group)
    return lines or ["- None"]


def format_ambiguous_track_details(decision: TrackMatchDecision) -> list[str]:
    lines = [
        f"- {format_track_context(decision.track)}",
        f"  Search query: {format_search_query(decision.track)}",
        f"  Why: {display_report_value(decision.reason)}",
    ]
    if decision.review_candidates:
        lines.append("  Matching Spotify candidates:")
        lines.extend(f"    - {format_spotify_candidate(candidate)}" for candidate in decision.review_candidates)
    else:
        lines.append("  Matching Spotify candidates: none recorded")
    return lines


def format_unmatched_track_details(decision: TrackMatchDecision) -> list[str]:
    lines = [
        f"- {format_track_context(decision.track)}",
        f"  Search query: {format_search_query(decision.track)}",
        f"  Why: {display_report_value(decision.reason)}",
    ]
    if not decision.review_candidates:
        lines.append("  Spotify returned 0 candidates.")
        return lines

    closest_candidate = decision.review_candidates[0]
    lines.append(f"  Spotify returned {len(decision.review_candidates)} candidate(s).")
    lines.append(f"  Closest Spotify result: {format_spotify_candidate(closest_candidate)}")
    lines.append("  Comparison:")
    lines.extend(format_candidate_comparison(decision.track, closest_candidate))
    return lines


def format_search_error_details(decision: TrackMatchDecision) -> list[str]:
    return [
        f"- {format_track_context(decision.track)}",
        f"  Search query: {format_search_query(decision.track)}",
        f"  Error: {display_report_value(decision.reason)}",
    ]


def format_track_context(track: PlaylistTrack) -> str:
    return (
        f"{display_report_value(track.playlist_name)} | {display_report_value(track.release_id)} | "
        f"{display_report_value(track.track_number)} | {display_report_value(track.artist_name)} | "
        f"{display_report_value(track.track_name)} | {display_report_value(track.album_name)}"
    )


def format_search_query(track: PlaylistTrack) -> str:
    return display_report_value(build_spotify_track_search_query(track))


def format_spotify_candidate(candidate: SpotifyTrackCandidate) -> str:
    return (
        f"{candidate.uri} | {format_spotify_artists(candidate)} | "
        f"{display_report_value(candidate.name)} | {display_report_value(candidate.album_name)}"
    )


def format_spotify_artists(candidate: SpotifyTrackCandidate) -> str:
    return display_report_value(", ".join(candidate.artists))


def format_candidate_comparison(track: PlaylistTrack, candidate: SpotifyTrackCandidate) -> list[str]:
    return [
        format_field_comparison(
            "Track name",
            track.track_name,
            candidate.name,
            normalize_music_text(track.track_name) == normalize_music_text(candidate.name),
        ),
        format_field_comparison(
            "Artist",
            track.artist_name,
            ", ".join(candidate.artists),
            normalized_artist_in_candidates(track.artist_name, candidate.artists),
        ),
        format_field_comparison(
            "Album",
            track.album_name,
            candidate.album_name,
            normalize_music_text(track.album_name) == normalize_music_text(candidate.album_name),
        ),
    ]


def format_field_comparison(label: str, discogs_value: str, spotify_value: str, matches: bool) -> str:
    status = "matches" if matches else "different"
    return (
        f"    {label}: {status} "
        f"(Discogs: {display_report_value(discogs_value)}; Spotify: {display_report_value(spotify_value)})"
    )


def display_report_value(value: object) -> str:
    text = " ".join(str(value or "").split())
    return text if text else "(blank)"


def default_report_path() -> Path:
    return script_report_path(__file__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH, help="Local env file containing Spotify app settings. Defaults to .env.")
    parser.add_argument("--playlist-output-dir", type=Path, default=DEFAULT_PLAYLIST_OUTPUT_DIRECTORY, help="Directory containing per-playlist CSV folders.")
    parser.add_argument("--report", type=Path, help="Publish report path. Defaults to reports/<timestamp>_publish_playlist.txt.")
    parser.add_argument("--token-cache", type=Path, default=DEFAULT_TOKEN_CACHE_PATH, help="Spotify token cache path.")
    parser.add_argument("--match-cache", type=Path, default=DEFAULT_MATCH_CACHE_PATH, help="Spotify track match cache path.")
    parser.add_argument("--publisher-config", type=Path, default=DEFAULT_PUBLISHER_CONFIG_PATH, help="Publisher JSON config. Defaults to config/publisher.json.")
    parser.add_argument("--debug-log", type=Path, help="Write sanitized Spotify publisher debug logs to this path.")
    parser.add_argument("--reauthorize", action="store_true", help="Force a fresh Spotify login before running the publisher.")
    parser.add_argument("--access-token", help=argparse.SUPPRESS)
    parser.add_argument("--playlists", nargs="+", help="Playlist names, folder names, folder paths, or master CSV paths to publish. Omit to process every playlist.")
    parser.add_argument("--search-limit", type=int, default=10, help="Spotify search result limit per track. Defaults to 10.")
    parser.add_argument("--publisher-sync-mode", choices=PUBLISHER_SYNC_MODES, default=APPEND_SYNC_MODE, help="Publisher sync mode. append adds missing tracks; replace replaces playlist contents. Defaults to append.")
    parser.add_argument("--refresh-match-cache", action="store_true", help="Recheck every playlist row with Spotify and update the local track match cache.")
    parser.add_argument("--publishing-dry-run", action="store_true", dest="dry_run", help="Preview Spotify playlist changes without creating or updating playlists.")
    parser.add_argument("--no-progress", action="store_false", dest="progress", help="Disable terminal progress output.")
    args = parser.parse_args(argv)
    if args.search_limit < 1 or args.search_limit > 10:
        parser.error("--search-limit must be between 1 and 10")
    args.apply = not args.dry_run
    args.report = args.report or default_report_path()
    return args


def run_spotify_publish_from_args(
    args: argparse.Namespace,
    playlist_master_paths: Sequence[Path] | None = None,
    playlist_names_by_master_path: Mapping[Path, str] | None = None,
    publisher_config: PublisherConfig | None = None,
    debug_log: DebugLog | None = None,
    spotify_client: SpotifyClient | None = None,
) -> SpotifyPublishSummary:
    if playlist_master_paths is not None:
        selected_master_paths: tuple[Path, ...] | None = tuple(playlist_master_paths)
    elif args.playlists:
        selected_master_paths = resolve_playlist_master_paths(
            args.playlist_output_dir,
            args.playlists,
            allow_all_selector=False,
        )
    else:
        selected_master_paths = None
    if debug_log:
        if selected_master_paths is not None:
            debug_log(f"resolved_playlist_masters count={len(selected_master_paths)}")
        debug_log("loading_spotify_settings")
    settings = load_spotify_settings(args.env_file, token_cache_path=args.token_cache)
    resolved_publisher_config = publisher_config or load_or_create_publisher_config(args.publisher_config)
    if debug_log:
        debug_log("spotify_settings_loaded")
        debug_log("publisher_config_loaded")
        debug_log("getting_spotify_access_token")
    access_token = args.access_token or get_access_token_for_run(settings, force_reauthorize=args.reauthorize)
    if debug_log:
        debug_log("spotify_access_token_ready")
    progress_label = "Updating Spotify playlists" if args.apply else "Planning Spotify playlists"
    progress = ProgressReporter(label=progress_label) if getattr(args, "progress", False) else None
    return publish_spotify_playlists(
        playlist_output_directory=args.playlist_output_dir,
        report_path=args.report,
        spotify_client=spotify_client or SpotifyClient(debug_log=debug_log),
        access_token=access_token,
        search_limit=args.search_limit,
        progress=progress,
        playlist_master_paths=selected_master_paths,
        playlist_names_by_master_path=playlist_names_by_master_path,
        debug_log=debug_log,
        match_cache_path=args.match_cache,
        publisher_config=resolved_publisher_config,
        apply=args.apply,
        publisher_sync_mode=args.publisher_sync_mode,
        info_log=print,
        refresh_match_cache=args.refresh_match_cache,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        debug_log = build_debug_logger(args.debug_log)
        if debug_log:
            debug_log("start spotify_publish")
            debug_log(
                f"parsed_args playlist_selectors={len(args.playlists or ())} search_limit={args.search_limit} "
                f"progress={args.progress} apply={args.apply} sync_mode={args.publisher_sync_mode} "
                f"refresh_match_cache={args.refresh_match_cache}"
            )
        summary = run_spotify_publish_from_args(args, debug_log=debug_log)
        if debug_log:
            debug_log(
                "completed "
                f"track_count={summary.track_count} matched={summary.matched_count} cache_hits={summary.cache_hit_count} "
                f"searches={summary.search_count} already_present={summary.already_present_count} "
                f"would_add={summary.would_add_count} added={summary.added_count} "
                f"ambiguous={summary.ambiguous_count} unmatched={summary.unmatched_count} errors={summary.error_count}"
            )
    except (FileNotFoundError, NotADirectoryError, ValueError, csv.Error, SpotifyApiError) as error:
        print(f"Error: {error}", file=sys.stderr)
        if isinstance(error, SpotifyRateLimitDeferredError) and "args" in locals() and args.report.exists():
            print(f"Spotify publish report: {args.report}", file=sys.stderr)
        return 1
    print(f"Spotify publish report: {summary.report_path}")
    print(f"Tracks: {summary.track_count}")
    print(f"Cache hits: {summary.cache_hit_count}")
    print(f"Spotify searches: {summary.search_count}")
    print(f"Matched: {summary.matched_count}")
    print(f"Already present: {summary.already_present_count}")
    print(f"Would add: {summary.would_add_count}")
    print(f"Added: {summary.added_count}")
    print(f"Would include in replacement: {summary.would_include_count}")
    print(f"Included in replacement: {summary.included_count}")
    print(f"Duplicate source tracks skipped: {summary.duplicate_in_source_count}")
    print(f"Ambiguous: {summary.ambiguous_count}")
    print(f"Unmatched: {summary.unmatched_count}")
    print(f"Search errors: {summary.error_count}")
    return 0


def get_access_token_for_run(settings, force_reauthorize: bool = False) -> str:
    if force_reauthorize:
        return authorize_spotify_interactively(settings).access_token
    return get_spotify_access_token(
        settings=settings,
        required_scopes=DEFAULT_AUTHORIZE_SCOPES,
    )


def clean_cell(value: object) -> str:
    return str(value or "").strip()


if __name__ == "__main__":
    raise SystemExit(main())
