#!/usr/bin/env python3
"""Spotify playlist publishing from generated playlist CSVs."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast


SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[2]
if str(SCRIPTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.authorization_flow import DEFAULT_AUTHORIZE_SCOPES, authorize_spotify_interactively  # noqa: E402
from publishers.spotify.client import (  # noqa: E402
    SpotifyAlbumLookupError,
    SpotifyApiError,
    SpotifyClient,
    SpotifyPlaylist,
    SpotifyPlaylistItem,
    SpotifyPlaylistPlanningClient,
    SpotifyPlaylistPublishClient,
    SpotifyRateLimitDeferredError,
    SpotifyRateLimitRetriesExhaustedError,
    SpotifyTrackSearchClient,
)
from publishers.spotify.env import DEFAULT_ENV_PATH, DEFAULT_TOKEN_CACHE_PATH, load_spotify_settings  # noqa: E402
from publishers.spotify.match_cache import (  # noqa: E402
    cache_track_match,
    cached_track_match,
    load_spotify_track_match_cache,
    save_spotify_track_match_cache,
    spotify_track_match_key,
    utc_timestamp,
)
from publishers.spotify.matching import (  # noqa: E402
    AMBIGUOUS,
    CONSTRAINED_TYPO_MATCH_REASON_PREFIX,
    ERROR,
    FEATURED_ARTIST_MATCH_REASON_PREFIX,
    LEADING_IN_TITLE_MATCH_REASON_PREFIX,
    MATCHED,
    ORIGINAL_MIX_MATCH_REASON_PREFIX,
    SPOTIFY_FEATURED_ARTIST_MATCH_REASON_PREFIX,
    UNMATCHED,
    PlaylistTrack,
    TrackMatchDecision,
    SpotifyTrackCandidate,
    build_spotify_track_search_queries,
    choose_best_track_match,
    normalize_music_text,
    track_match_error,
)
from publishers.spotify.publish_reports import (  # noqa: E402
    build_publish_summary,
    build_summary,
    cached_matched_publish_decision_count,
    count_publish_decisions,
    display_report_value,
    flatten_report_details,
    format_ambiguous_track_details,
    format_artist_names,
    format_candidate_comparison,
    format_field_comparison,
    format_final_playlist_item,
    format_match_decision,
    format_publish_decision,
    format_publish_decisions,
    format_publish_review_details,
    format_search_error_details,
    format_search_query,
    format_spotify_artists,
    format_spotify_candidate,
    format_track_context,
    format_unmatched_track_details,
    searched_matched_publish_decision_count,
    write_dry_run_report,
    write_publish_report,
)
from publishers.spotify.publish_state import (  # noqa: E402
    SpotifyPublishState,
    load_spotify_publish_state,
    record_spotify_publish_state_track,
    save_spotify_publish_state,
    spotify_publish_state_has_track,
)
from publishers.spotify.publish_types import (  # noqa: E402
    ADDED,
    ALREADY_PRESENT,
    APPEND_SYNC_MODE,
    DEFAULT_MAX_NEW_SEARCHES_PER_RUN,
    DUPLICATE_IN_SOURCE,
    INCLUDED,
    MATCH_SOURCE_ALBUM,
    MATCH_SOURCE_CACHE,
    MATCH_SOURCE_SEARCH,
    PUBLISHER_SYNC_MODES,
    REPLACE_SYNC_MODE,
    UNLIMITED_NEW_SEARCHES_PER_RUN,
    WOULD_ADD,
    WOULD_INCLUDE,
    FinalPlaylistItem,
    InfoLog,
    PlaylistPublishContext,
    PlaylistPublishDecision,
    SpotifyDryRunSummary,
    SpotifyPublishSummary,
    SpotifyTrackSearchResult,
)
from publishers.spotify.release_matching import (  # noqa: E402
    ALBUM_EXACT_TRACK_MATCH_REASON,
    ALBUM_POSITION_MATCH_REASON,
    SpotifyAlbumLookupClient,
    release_is_eligible_for_album_lookup,
    resolve_release_with_album,
)
from publishers.spotify.session import get_spotify_access_token  # noqa: E402
from shared.cli import EXPECTED_CLI_ERRORS, print_cli_error, print_cli_summary  # noqa: E402
from shared.cli_args import append_cli_option  # noqa: E402
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
    publisher_playlist_name,
)
from shared.reports import script_report_path  # noqa: E402
from shared.text import clean_cell  # noqa: E402
from shared.tunemymusic import TUNEMYMUSIC_COLUMNS, missing_tunemymusic_columns  # noqa: E402
from shared.workflow_paths import (  # noqa: E402
    DEFAULT_PLAYLIST_OUTPUT_DIRECTORY as SHARED_PLAYLIST_OUTPUT_DIRECTORY,
    DEFAULT_SPOTIFY_MATCH_CACHE_PATH,
    DEFAULT_SPOTIFY_PUBLISH_STATE_CACHE_PATH,
)


DEFAULT_PLAYLIST_OUTPUT_DIRECTORY = SHARED_PLAYLIST_OUTPUT_DIRECTORY
DEFAULT_MATCH_CACHE_PATH = DEFAULT_SPOTIFY_MATCH_CACHE_PATH
DEFAULT_PUBLISH_STATE_CACHE_PATH = DEFAULT_SPOTIFY_PUBLISH_STATE_CACHE_PATH
PLAYLIST_DESCRIPTION = "Generated from Discogs collection"


@dataclass(frozen=True)
class PendingAppendTrack:
    decision_index: int
    identity_key: str
    spotify_uri: str
    source_position: int
    known_to_publisher: bool
    final_item: FinalPlaylistItem


@dataclass(frozen=True)
class AppendWriteOperation:
    decision_index: int
    identity_key: str
    spotify_uri: str
    source_position: int
    position: int | None
    known_to_publisher: bool
    final_item: FinalPlaylistItem


@dataclass(frozen=True)
class AppendPlaylistPlan:
    final_items: tuple[FinalPlaylistItem, ...]
    write_operations: tuple[AppendWriteOperation, ...]


def search_spotify_track(
    track: PlaylistTrack,
    spotify_client: SpotifyTrackSearchClient,
    access_token: str,
    search_limit: int,
) -> SpotifyTrackSearchResult:
    search_queries = build_spotify_track_search_queries(track)
    searched_queries: list[str] = []
    candidates: list[SpotifyTrackCandidate] = []
    query_errors: list[str] = []
    search_count = 0
    successful_search_count = 0

    for query in search_queries:
        searched_queries.append(query)
        search_count += 1
        try:
            query_candidates = spotify_client.search_tracks(
                access_token=access_token,
                query=query,
                limit=search_limit,
            )
        except (SpotifyRateLimitDeferredError, SpotifyRateLimitRetriesExhaustedError):
            raise
        except SpotifyApiError as error:
            query_errors.append(str(error))
            if is_query_specific_spotify_search_error(error):
                continue
            return SpotifyTrackSearchResult(
                decision=track_match_error(track, str(error), search_queries=tuple(searched_queries)),
                search_count=search_count,
            )
        successful_search_count += 1
        candidates.extend(query_candidates)
        decision = choose_best_track_match(
            track,
            tuple(candidates),
            search_queries=tuple(searched_queries),
            allow_leading_in_title_variant=False,
            allow_constrained_typo_fallback=False,
        )
        if decision.status in {MATCHED, AMBIGUOUS}:
            return SpotifyTrackSearchResult(decision=decision, search_count=search_count)

    if successful_search_count == 0 and query_errors:
        return SpotifyTrackSearchResult(
            decision=track_match_error(track, "; ".join(query_errors), search_queries=tuple(searched_queries)),
            search_count=search_count,
        )
    return SpotifyTrackSearchResult(
        decision=choose_best_track_match(track, tuple(candidates), search_queries=tuple(searched_queries)),
        search_count=search_count,
    )


def is_query_specific_spotify_search_error(error: SpotifyApiError) -> bool:
    return "Query exceeds maximum length of 250 characters" in str(error)


def dry_run_spotify_playlist_publish(
    playlist_output_directory: Path,
    report_path: Path,
    spotify_client: SpotifyTrackSearchClient,
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
                    search_result = search_spotify_track(
                        track=track,
                        spotify_client=spotify_client,
                        access_token=access_token,
                        search_limit=search_limit,
                    )
                    decision = search_result.decision
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
    spotify_client: SpotifyPlaylistPlanningClient,
    access_token: str,
    search_limit: int = 10,
    progress: ProgressReporter | None = None,
    playlist_selectors: Sequence[str] | None = None,
    playlist_master_paths: Sequence[Path] | None = None,
    playlist_names_by_master_path: Mapping[Path, str] | None = None,
    debug_log: DebugLog | None = None,
    match_cache_path: Path = DEFAULT_MATCH_CACHE_PATH,
    publish_state_cache_path: Path | None = None,
    publisher_config: PublisherConfig | None = None,
    apply: bool = True,
    publisher_sync_mode: str = APPEND_SYNC_MODE,
    info_log: InfoLog | None = None,
    refresh_match_cache: bool = False,
    max_new_searches_per_run: int = DEFAULT_MAX_NEW_SEARCHES_PER_RUN,
) -> SpotifyPublishSummary:
    if publisher_sync_mode not in PUBLISHER_SYNC_MODES:
        raise ValueError(f"publisher_sync_mode must be one of: {', '.join(PUBLISHER_SYNC_MODES)}")
    if max_new_searches_per_run < 0:
        raise ValueError("max_new_searches_per_run must be non-negative")
    if playlist_selectors is not None and playlist_master_paths is not None:
        raise ValueError("playlist_selectors and playlist_master_paths cannot both be provided")
    # Planning-only clients are valid for dry runs. Write methods are called only when apply is true.
    write_client = cast(SpotifyPlaylistPublishClient, spotify_client)
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
    resolved_publish_state_cache_path = publish_state_cache_path or default_publish_state_cache_path(match_cache_path)
    match_cache = load_spotify_track_match_cache(match_cache_path)
    publish_state = load_spotify_publish_state(resolved_publish_state_cache_path)
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
    release_tracks_by_id = group_unique_tracks_by_release_id(tracks_by_master_path)
    album_lookup_client = (
        spotify_client
        if isinstance(spotify_client, SpotifyAlbumLookupClient)
        else None
    )
    attempted_album_release_ids: set[str] = set()
    album_decisions_by_track_key: dict[str, TrackMatchDecision] = {}
    preexisting_matched_cache_keys = (
        set()
        if refresh_match_cache
        else existing_matched_cache_keys(release_tracks_by_id, match_cache)
    )
    total_tracks = sum(len(tracks) for tracks in tracks_by_master_path.values())
    search_count = 0
    searched_row_count = 0
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
            context_index = len(playlist_contexts)
            playlist_contexts.append(context)
            planned_write_uris: list[str] = []
            append_candidates: list[PendingAppendTrack] = []
            source_positions_by_identity = source_identity_positions_by_identity(target_playlist_name, playlist_tracks)
            source_tracks_by_identity = source_tracks_by_identity_key(target_playlist_name, playlist_tracks)
            seen_source_identity_keys: set[str] = set()
            existing_identity_keys = {
                spotify_playlist_item_identity_key(target_playlist_name, item)
                for item in existing_items
            }
            existing_incomplete_spotify_uris = incomplete_spotify_playlist_item_uris(existing_items)
            for source_position, track in enumerate(playlist_tracks, start=1):
                track_cache_key = spotify_track_match_key(track)
                cached_match = None
                if not refresh_match_cache:
                    cached_match = cached_track_match(track, match_cache, seen_at=timestamp)
                album_decision = album_decisions_by_track_key.get(track_cache_key)
                cached_match_is_authoritative = (
                    cached_match is not None
                    and track_cache_key in preexisting_matched_cache_keys
                )
                if (
                    not cached_match_is_authoritative
                    and album_decision is None
                    and album_lookup_client is not None
                    and bool(track.release_id)
                    and track.release_id not in attempted_album_release_ids
                    and not new_search_budget_reached(search_count, max_new_searches_per_run)
                ):
                    release_tracks = release_tracks_by_id.get(track.release_id)
                    if release_tracks and release_is_eligible_for_album_lookup(release_tracks):
                        attempted_album_release_ids.add(track.release_id)
                        if refresh_match_cache or release_has_uncached_track(
                            release_tracks,
                            match_cache,
                        ):
                            album_result = resolve_release_with_album(
                                source_tracks=release_tracks,
                                spotify_client=album_lookup_client,
                                access_token=access_token,
                                search_limit=search_limit,
                            )
                            search_count += album_result.search_count
                            for resolved_decision in album_result.decisions:
                                resolved_key = spotify_track_match_key(resolved_decision.track)
                                if resolved_key not in preexisting_matched_cache_keys:
                                    album_decisions_by_track_key[resolved_key] = resolved_decision
                                    cache_track_match(
                                        match_cache,
                                        resolved_decision,
                                        matched_at=timestamp,
                                    )
                            album_decision = album_decisions_by_track_key.get(track_cache_key)
                if (
                    not cached_match_is_authoritative
                    and album_decision is None
                    and cached_match is None
                    and new_search_budget_reached(search_count, max_new_searches_per_run)
                ):
                    if publisher_sync_mode == APPEND_SYNC_MODE:
                        final_items, playlist_contexts, spotify_playlists = finalize_append_playlist(
                            spotify_client=write_client,
                            access_token=access_token,
                            context_index=context_index,
                            playlist_contexts=playlist_contexts,
                            existing_items=existing_items,
                            append_candidates=append_candidates,
                            source_positions_by_identity=source_positions_by_identity,
                            source_tracks_by_identity=source_tracks_by_identity,
                            decisions=decisions,
                            final_items=final_items,
                            spotify_playlists=spotify_playlists,
                            current_user_id=current_user_id,
                            publish_state=publish_state,
                            publish_state_cache_path=resolved_publish_state_cache_path,
                            report_path=report_path,
                            match_cache_path=match_cache_path,
                            match_cache=match_cache,
                            apply=apply,
                            cache_hit_count=cache_hit_count,
                            search_count=search_count,
                            searched_row_count=searched_row_count,
                            timestamp=timestamp,
                            run_status=search_budget_run_status(search_count),
                        )
                    save_spotify_track_match_cache(match_cache_path, match_cache)
                    if apply and publisher_sync_mode == APPEND_SYNC_MODE:
                        save_spotify_publish_state(resolved_publish_state_cache_path, publish_state)
                    return write_publish_summary(
                        decisions=tuple(decisions),
                        final_items=tuple(reindex_final_items(final_items)),
                        playlist_contexts=tuple(playlist_contexts),
                        report_path=report_path,
                        apply=apply,
                        publisher_sync_mode=publisher_sync_mode,
                        cache_hit_count=cache_hit_count,
                        search_count=search_count,
                        searched_row_count=searched_row_count,
                        run_status=search_budget_run_status(search_count),
                    )
                try:
                    processed_track_count += 1
                    if cached_match:
                        if album_decision is not None and not cached_match_is_authoritative:
                            decision = album_decision
                            match_source = MATCH_SOURCE_ALBUM
                            track_search_count = 0
                        else:
                            decision = cached_match.decision
                            match_source = MATCH_SOURCE_CACHE
                            track_search_count = 0
                            cache_hit_count += 1
                    elif album_decision is not None:
                        decision = album_decision
                        match_source = MATCH_SOURCE_ALBUM
                        track_search_count = 0
                    else:
                        search_result = search_spotify_track(
                            track=track,
                            spotify_client=spotify_client,
                            access_token=access_token,
                            search_limit=search_limit,
                        )
                        decision = search_result.decision
                        match_source = MATCH_SOURCE_SEARCH
                        track_search_count = search_result.search_count
                        search_count += track_search_count
                        if track_search_count:
                            searched_row_count += 1
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
                    decision_index = len(decisions)
                    decisions.append(publish_decision)
                    identity_key = publish_decision_identity_key(publish_decision)
                    if identity_key:
                        source_positions_by_identity.setdefault(identity_key, source_position)
                        source_tracks_by_identity.setdefault(identity_key, track)
                    if publish_decision.status in {WOULD_ADD, ADDED, WOULD_INCLUDE, INCLUDED}:
                        seen_source_identity_keys.add(identity_key)
                        if publisher_sync_mode == APPEND_SYNC_MODE:
                            known_to_publisher = spotify_publish_state_has_track(
                                publish_state,
                                target_playlist_name,
                                identity_key,
                            )
                            publish_decision = replace(
                                publish_decision,
                                reason=publish_reason_with_match_details(
                                    append_publish_reason(known_to_publisher),
                                    decision,
                                ),
                            )
                            decisions[decision_index] = publish_decision
                            append_candidates.append(
                                PendingAppendTrack(
                                    decision_index=decision_index,
                                    identity_key=identity_key,
                                    spotify_uri=publish_decision.spotify_uri,
                                    source_position=source_position,
                                    known_to_publisher=known_to_publisher,
                                    final_item=final_playlist_item_from_decision(source_position, publish_decision),
                                )
                            )
                        else:
                            planned_write_uris.append(publish_decision.spotify_uri)
                            final_items.append(final_playlist_item_from_decision(len(final_items) + 1, publish_decision))
                    elif identity_key and publish_decision.status in {ALREADY_PRESENT, DUPLICATE_IN_SOURCE}:
                        seen_source_identity_keys.add(identity_key)
                    if match_source in {MATCH_SOURCE_ALBUM, MATCH_SOURCE_SEARCH}:
                        cache_track_match(match_cache, decision, matched_at=timestamp)
                    if publisher_sync_mode == APPEND_SYNC_MODE and apply and len(append_candidates) >= 100:
                        final_items, playlist_contexts, spotify_playlists = finalize_append_playlist(
                            spotify_client=write_client,
                            access_token=access_token,
                            context_index=context_index,
                            playlist_contexts=playlist_contexts,
                            existing_items=existing_items,
                            append_candidates=append_candidates,
                            source_positions_by_identity=source_positions_by_identity,
                            source_tracks_by_identity=source_tracks_by_identity,
                            decisions=decisions,
                            final_items=final_items,
                            spotify_playlists=spotify_playlists,
                            current_user_id=current_user_id,
                            publish_state=publish_state,
                            publish_state_cache_path=resolved_publish_state_cache_path,
                            report_path=report_path,
                            match_cache_path=match_cache_path,
                            match_cache=match_cache,
                            apply=apply,
                            cache_hit_count=cache_hit_count,
                            search_count=search_count,
                            searched_row_count=searched_row_count,
                            timestamp=timestamp,
                        )
                        context = playlist_contexts[context_index]
                        existing_items = spotify_playlist_items_from_final_items(
                            playlist_final_items(final_items, target_playlist_name)
                        )
                        append_candidates = []
                    if debug_log:
                        debug_log(f"publish_track_done index={processed_track_count} status={publish_decision.status} source={match_source}")
                    if should_stop_for_new_search_budget(
                        search_count=search_count,
                        max_new_searches_per_run=max_new_searches_per_run,
                        processed_track_count=processed_track_count,
                        total_tracks=total_tracks,
                    ):
                        if publisher_sync_mode == APPEND_SYNC_MODE:
                            final_items, playlist_contexts, spotify_playlists = finalize_append_playlist(
                                spotify_client=write_client,
                                access_token=access_token,
                                context_index=context_index,
                                playlist_contexts=playlist_contexts,
                                existing_items=existing_items,
                                append_candidates=append_candidates,
                                source_positions_by_identity=source_positions_by_identity,
                                source_tracks_by_identity=source_tracks_by_identity,
                                decisions=decisions,
                                final_items=final_items,
                                spotify_playlists=spotify_playlists,
                                current_user_id=current_user_id,
                                publish_state=publish_state,
                                publish_state_cache_path=resolved_publish_state_cache_path,
                                report_path=report_path,
                                match_cache_path=match_cache_path,
                                match_cache=match_cache,
                                apply=apply,
                                cache_hit_count=cache_hit_count,
                                search_count=search_count,
                                searched_row_count=searched_row_count,
                                timestamp=timestamp,
                                run_status=search_budget_run_status(search_count),
                            )
                        save_spotify_track_match_cache(match_cache_path, match_cache)
                        if apply and publisher_sync_mode == APPEND_SYNC_MODE:
                            save_spotify_publish_state(resolved_publish_state_cache_path, publish_state)
                        return write_publish_summary(
                            decisions=tuple(decisions),
                            final_items=tuple(reindex_final_items(final_items)),
                            playlist_contexts=tuple(playlist_contexts),
                            report_path=report_path,
                            apply=apply,
                            publisher_sync_mode=publisher_sync_mode,
                            cache_hit_count=cache_hit_count,
                            search_count=search_count,
                            searched_row_count=searched_row_count,
                            run_status=search_budget_run_status(search_count),
                        )
                finally:
                    if progress:
                        progress.update(processed_track_count)

            if publisher_sync_mode == APPEND_SYNC_MODE:
                final_items, playlist_contexts, spotify_playlists = finalize_append_playlist(
                    spotify_client=write_client,
                    access_token=access_token,
                    context_index=context_index,
                    playlist_contexts=playlist_contexts,
                    existing_items=existing_items,
                    append_candidates=append_candidates,
                    source_positions_by_identity=source_positions_by_identity,
                    source_tracks_by_identity=source_tracks_by_identity,
                    decisions=decisions,
                    final_items=final_items,
                    spotify_playlists=spotify_playlists,
                    current_user_id=current_user_id,
                    publish_state=publish_state,
                    publish_state_cache_path=resolved_publish_state_cache_path,
                    report_path=report_path,
                    match_cache_path=match_cache_path,
                    match_cache=match_cache,
                    apply=apply,
                    cache_hit_count=cache_hit_count,
                    search_count=search_count,
                    searched_row_count=searched_row_count,
                    timestamp=timestamp,
                )
            else:
                planned_write_uris_by_playlist[target_playlist_name] = tuple(planned_write_uris)
    except (SpotifyRateLimitDeferredError, SpotifyRateLimitRetriesExhaustedError):
        write_status = (
            "append checkpoints completed before the rate limit stop were written"
            if apply and publisher_sync_mode == APPEND_SYNC_MODE and any(decision.status == ADDED for decision in decisions)
            else "no playlist writes were attempted"
        )
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
            searched_row_count=searched_row_count,
            run_status=(
                f"aborted - Spotify rate limit stopped the run after {len(decisions)} of {total_tracks} tracks; "
                f"{write_status}"
            ),
        )
        raise
    except SpotifyAlbumLookupError as error:
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
            searched_row_count=searched_row_count,
            run_status=(
                f"aborted - Spotify API error after {len(decisions)} of {total_tracks} tracks: "
                f"{str(error).splitlines()[0]}"
            ),
        )
        raise
    finally:
        if progress:
            progress.finish()

    save_spotify_track_match_cache(match_cache_path, match_cache)
    if apply and publisher_sync_mode == APPEND_SYNC_MODE:
        save_spotify_publish_state(resolved_publish_state_cache_path, publish_state)
    summary = write_publish_summary(
        decisions=tuple(decisions),
        final_items=tuple(reindex_final_items(final_items)),
        playlist_contexts=tuple(playlist_contexts),
        report_path=report_path,
        apply=apply,
        publisher_sync_mode=publisher_sync_mode,
        cache_hit_count=cache_hit_count,
        search_count=search_count,
        searched_row_count=searched_row_count,
    )
    if apply and not (publisher_sync_mode == APPEND_SYNC_MODE):
        validate_replace_apply_decisions(tuple(decisions), publisher_sync_mode)
        applied_decisions, applied_final_items, applied_playlist_contexts, spotify_playlists = apply_planned_playlist_writes(
            spotify_client=write_client,
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
            searched_row_count=searched_row_count,
        )
        summary = write_publish_summary(
            decisions=applied_decisions,
            final_items=reindex_final_items(applied_final_items),
            playlist_contexts=applied_playlist_contexts,
            report_path=report_path,
            apply=apply,
            publisher_sync_mode=publisher_sync_mode,
            cache_hit_count=cache_hit_count,
            search_count=search_count,
            searched_row_count=searched_row_count,
        )
    return summary


def has_new_search_budget(max_new_searches_per_run: int) -> bool:
    return max_new_searches_per_run != UNLIMITED_NEW_SEARCHES_PER_RUN


def group_unique_tracks_by_release_id(
    tracks_by_master_path: Mapping[Path, Sequence[PlaylistTrack]],
) -> dict[str, tuple[PlaylistTrack, ...]]:
    grouped_tracks: dict[str, list[PlaylistTrack]] = {}
    seen_track_keys_by_release_id: dict[str, set[str]] = {}
    for playlist_tracks in tracks_by_master_path.values():
        for track in playlist_tracks:
            release_id = track.release_id.strip()
            if not release_id:
                continue
            track_key = spotify_track_match_key(track)
            seen_track_keys = seen_track_keys_by_release_id.setdefault(release_id, set())
            if track_key in seen_track_keys:
                continue
            seen_track_keys.add(track_key)
            grouped_tracks.setdefault(release_id, []).append(track)
    return {
        release_id: order_release_tracks(tracks)
        for release_id, tracks in grouped_tracks.items()
    }


def order_release_tracks(tracks: Sequence[PlaylistTrack]) -> tuple[PlaylistTrack, ...]:
    numbered_tracks: list[tuple[int, PlaylistTrack]] = []
    for track in tracks:
        try:
            track_number = int(track.track_number.strip())
        except ValueError:
            return tuple(tracks)
        numbered_tracks.append((track_number, track))
    if len({track_number for track_number, _ in numbered_tracks}) != len(numbered_tracks):
        return tuple(tracks)
    return tuple(track for _, track in sorted(numbered_tracks, key=lambda item: item[0]))


def existing_matched_cache_keys(
    release_tracks_by_id: Mapping[str, Sequence[PlaylistTrack]],
    match_cache: Mapping[str, Mapping[str, object]],
) -> set[str]:
    matched_keys: set[str] = set()
    for release_tracks in release_tracks_by_id.values():
        for track in release_tracks:
            cached_match = cached_track_match(track, match_cache)
            if cached_match and cached_match.decision.status == MATCHED:
                matched_keys.add(cached_match.cache_key)
    return matched_keys


def release_has_uncached_track(
    release_tracks: Sequence[PlaylistTrack],
    match_cache: Mapping[str, Mapping[str, object]],
) -> bool:
    return any(
        cached_track_match(track, match_cache) is None
        for track in release_tracks
    )


def new_search_budget_reached(search_count: int, max_new_searches_per_run: int) -> bool:
    return has_new_search_budget(max_new_searches_per_run) and search_count >= max_new_searches_per_run


def should_stop_for_new_search_budget(
    search_count: int,
    max_new_searches_per_run: int,
    processed_track_count: int,
    total_tracks: int,
) -> bool:
    return new_search_budget_reached(search_count, max_new_searches_per_run) and processed_track_count < total_tracks


def search_budget_run_status(search_count: int) -> str:
    return f"partial - new Spotify search budget reached after {search_count} searches"


def append_checkpoint_run_status(search_count: int) -> str:
    return f"checkpoint - append progress saved after {search_count} searches; run still in progress"


def finalize_append_playlist(
    spotify_client: SpotifyPlaylistPublishClient,
    access_token: str,
    context_index: int,
    playlist_contexts: list[PlaylistPublishContext],
    existing_items: Sequence[SpotifyPlaylistItem],
    append_candidates: Sequence[PendingAppendTrack],
    source_positions_by_identity: Mapping[str, int],
    source_tracks_by_identity: Mapping[str, PlaylistTrack],
    spotify_playlists: Sequence[SpotifyPlaylist],
    current_user_id: str,
    decisions: list[PlaylistPublishDecision],
    final_items: list[FinalPlaylistItem],
    report_path: Path,
    match_cache_path: Path,
    match_cache: Mapping[str, Mapping[str, object]],
    publish_state: SpotifyPublishState,
    publish_state_cache_path: Path,
    apply: bool,
    cache_hit_count: int,
    search_count: int,
    searched_row_count: int = 0,
    timestamp: str = "",
    run_status: str | None = None,
) -> tuple[list[FinalPlaylistItem], list[PlaylistPublishContext], tuple[SpotifyPlaylist, ...]]:
    context = playlist_contexts[context_index]
    observe_existing_publish_state_tracks(
        publish_state=publish_state,
        target_playlist_name=context.target_playlist_name,
        existing_items=existing_items,
        source_positions_by_identity=source_positions_by_identity,
        source_tracks_by_identity=source_tracks_by_identity,
        timestamp=timestamp,
    )
    plan = build_append_playlist_plan(
        target_playlist_name=context.target_playlist_name,
        existing_items=existing_items,
        append_candidates=append_candidates,
        source_positions_by_identity=source_positions_by_identity,
        previous_final_items=playlist_final_items(final_items, context.target_playlist_name),
    )
    replace_final_items_for_playlist(final_items, context.target_playlist_name, plan.final_items)
    if not apply:
        return final_items, playlist_contexts, tuple(spotify_playlists)
    if not plan.write_operations:
        save_spotify_publish_state(publish_state_cache_path, publish_state)
        return final_items, playlist_contexts, tuple(spotify_playlists)
    applied_context = ensure_spotify_playlist_for_writes(
        spotify_client=spotify_client,
        access_token=access_token,
        context=context,
        has_writes=bool(plan.write_operations),
    )
    playlist_contexts[context_index] = applied_context
    try:
        apply_append_write_operations(
            spotify_client=spotify_client,
            access_token=access_token,
            context=applied_context,
            operations=plan.write_operations,
            decisions=decisions,
            final_items=final_items,
            publish_state=publish_state,
            timestamp=timestamp,
        )
    except (SpotifyApiError, ValueError) as error:
        playlist_contexts[context_index] = replace(
            applied_context,
            info_message=f"{applied_context.info_message}; publishing failed: {display_report_value(error)}",
        )
        save_spotify_track_match_cache(match_cache_path, match_cache)
        save_spotify_publish_state(publish_state_cache_path, publish_state)
        write_publish_summary(
            decisions=tuple(decisions),
            final_items=tuple(reindex_final_items(final_items)),
            playlist_contexts=tuple(playlist_contexts),
            report_path=report_path,
            apply=True,
            publisher_sync_mode=APPEND_SYNC_MODE,
            cache_hit_count=cache_hit_count,
            search_count=search_count,
            searched_row_count=searched_row_count,
            run_status=f"failed - publishing stopped while writing {applied_context.target_playlist_name}",
        )
        raise
    updated_playlists = update_spotify_playlist_inventory(spotify_playlists, applied_context, current_user_id)
    save_spotify_track_match_cache(match_cache_path, match_cache)
    save_spotify_publish_state(publish_state_cache_path, publish_state)
    write_publish_summary(
        decisions=tuple(decisions),
        final_items=tuple(reindex_final_items(final_items)),
        playlist_contexts=tuple(playlist_contexts),
        report_path=report_path,
        apply=True,
        publisher_sync_mode=APPEND_SYNC_MODE,
        cache_hit_count=cache_hit_count,
        search_count=search_count,
        searched_row_count=searched_row_count,
        run_status=run_status or append_checkpoint_run_status(search_count),
    )
    return final_items, playlist_contexts, tuple(updated_playlists)


def build_append_playlist_plan(
    target_playlist_name: str,
    existing_items: Sequence[SpotifyPlaylistItem],
    append_candidates: Sequence[PendingAppendTrack],
    source_positions_by_identity: Mapping[str, int],
    previous_final_items: Sequence[FinalPlaylistItem] = (),
) -> AppendPlaylistPlan:
    previous_final_items_by_identity = {
        final_playlist_item_identity_key(item): item
        for item in previous_final_items
    }
    planned_items = [
        append_plan_item_from_existing(
            target_playlist_name,
            item,
            source_positions_by_identity,
            previous_final_items_by_identity,
        )
        for item in existing_items
    ]
    operations_by_identity: dict[str, AppendWriteOperation] = {}
    for candidate in sorted(append_candidates, key=lambda item: item.source_position):
        final_item = replace(candidate.final_item, status=WOULD_ADD)
        if candidate.known_to_publisher:
            insert_index = append_restore_insert_index(planned_items, candidate.source_position)
            planned_items.insert(
                insert_index,
                AppendPlanItem(
                    identity_key=candidate.identity_key,
                    source_position=candidate.source_position,
                    final_item=final_item,
                    pending_candidate=candidate,
                ),
            )
        else:
            planned_items.append(
                AppendPlanItem(
                    identity_key=candidate.identity_key,
                    source_position=None,
                    final_item=final_item,
                    pending_candidate=candidate,
                ),
            )
    final_items = tuple(item.final_item for item in planned_items)
    for index, item in enumerate(planned_items):
        candidate = item.pending_candidate
        if not candidate:
            continue
        operations_by_identity[candidate.identity_key] = AppendWriteOperation(
            decision_index=candidate.decision_index,
            identity_key=candidate.identity_key,
            spotify_uri=candidate.spotify_uri,
            source_position=candidate.source_position,
            position=index if candidate.known_to_publisher else None,
            known_to_publisher=candidate.known_to_publisher,
            final_item=item.final_item,
        )
    return AppendPlaylistPlan(
        final_items=final_items,
        write_operations=tuple(operations_by_identity.values()),
    )


@dataclass(frozen=True)
class AppendPlanItem:
    identity_key: str
    source_position: int | None
    final_item: FinalPlaylistItem
    pending_candidate: PendingAppendTrack | None = None


def append_plan_item_from_existing(
    target_playlist_name: str,
    item: SpotifyPlaylistItem,
    source_positions_by_identity: Mapping[str, int],
    previous_final_items_by_identity: Mapping[str, FinalPlaylistItem],
) -> AppendPlanItem:
    identity_key = spotify_playlist_item_identity_key(target_playlist_name, item)
    previous_final_item = previous_final_items_by_identity.get(identity_key)
    final_item = (
        previous_final_item
        if previous_final_item and previous_final_item.spotify_uri == item.uri
        else FinalPlaylistItem(
            playlist_name=target_playlist_name,
            position=0,
            status="existing",
            spotify_uri=item.uri,
            track_name=item.name,
            artist_names=item.artists,
            album_name=item.album_name,
            source_track=None,
        )
    )
    return AppendPlanItem(
        identity_key=identity_key,
        source_position=source_positions_by_identity.get(identity_key),
        final_item=final_item,
    )


def append_restore_insert_index(planned_items: Sequence[AppendPlanItem], source_position: int) -> int:
    for index, item in enumerate(planned_items):
        if item.source_position is not None and item.source_position > source_position:
            return index
    return len(planned_items)


def ensure_spotify_playlist_for_writes(
    spotify_client: SpotifyPlaylistPublishClient,
    access_token: str,
    context: PlaylistPublishContext,
    has_writes: bool,
) -> PlaylistPublishContext:
    if context.playlist_id or not has_writes:
        return context
    playlist = spotify_client.create_playlist(
        access_token=access_token,
        name=context.target_playlist_name,
        public=False,
        description=PLAYLIST_DESCRIPTION,
    )
    return replace(context, playlist_id=playlist.playlist_id)


def apply_append_write_operations(
    spotify_client: SpotifyPlaylistPublishClient,
    access_token: str,
    context: PlaylistPublishContext,
    operations: Sequence[AppendWriteOperation],
    decisions: list[PlaylistPublishDecision],
    final_items: list[FinalPlaylistItem],
    publish_state: SpotifyPublishState,
    timestamp: str,
) -> None:
    for operation_group in grouped_append_write_operations(operations):
        first_operation = operation_group[0]
        spotify_client.add_playlist_items(
            access_token=access_token,
            playlist_id=context.playlist_id,
            uris=tuple(operation.spotify_uri for operation in operation_group),
            position=first_operation.position,
        )
        for operation in operation_group:
            decisions[operation.decision_index] = replace(decisions[operation.decision_index], status=ADDED)
            mark_final_item_applied(final_items, operation)
            record_spotify_publish_state_track(
                state=publish_state,
                playlist_name=context.target_playlist_name,
                identity_key=operation.identity_key,
                spotify_uri=operation.spotify_uri,
                source_position=operation.source_position,
                track=decisions[operation.decision_index].track,
                timestamp=timestamp,
                event="published",
            )


def grouped_append_write_operations(operations: Sequence[AppendWriteOperation]) -> tuple[tuple[AppendWriteOperation, ...], ...]:
    groups: list[tuple[AppendWriteOperation, ...]] = []
    index = 0
    while index < len(operations):
        operation = operations[index]
        if operation.position is not None:
            groups.append((operation,))
            index += 1
            continue
        append_group = tuple(operations[index:index + 100])
        groups.append(append_group)
        index += len(append_group)
    return tuple(groups)


def mark_final_item_applied(final_items: list[FinalPlaylistItem], operation: AppendWriteOperation) -> None:
    for index, item in enumerate(final_items):
        if item.status != WOULD_ADD or item.spotify_uri != operation.spotify_uri:
            continue
        if final_playlist_item_identity_key(item) != operation.identity_key:
            continue
        final_items[index] = replace(item, status=ADDED)
        return


def final_playlist_item_identity_key(item: FinalPlaylistItem) -> str:
    return spotify_track_identity_key(
        target_playlist_name=item.playlist_name,
        artist_names=item.artist_names,
        album_name=item.album_name,
        track_name=item.track_name,
    )


def observe_existing_publish_state_tracks(
    publish_state: SpotifyPublishState,
    target_playlist_name: str,
    existing_items: Sequence[SpotifyPlaylistItem],
    source_positions_by_identity: Mapping[str, int],
    source_tracks_by_identity: Mapping[str, PlaylistTrack],
    timestamp: str,
) -> None:
    if not timestamp:
        return
    for item in existing_items:
        identity_key = spotify_playlist_item_identity_key(target_playlist_name, item)
        source_position = source_positions_by_identity.get(identity_key)
        if source_position is None:
            continue
        record_spotify_publish_state_track(
            state=publish_state,
            playlist_name=target_playlist_name,
            identity_key=identity_key,
            spotify_uri=item.uri,
            source_position=source_position,
            track=source_tracks_by_identity.get(identity_key),
            timestamp=timestamp,
            event="observed",
        )


def append_publish_reason(known_to_publisher: bool) -> str:
    if known_to_publisher:
        return "Spotify artist, album, and track will be restored at its source-order position"
    return "Spotify artist, album, and track is newly seen and will be appended to playlist"


def resolve_track_match(
    track: PlaylistTrack,
    spotify_client: SpotifyTrackSearchClient,
    access_token: str,
    search_limit: int,
    match_cache: dict[str, dict[str, object]],
    timestamp: str,
    refresh_match_cache: bool = False,
) -> tuple[TrackMatchDecision, str, int]:
    if not refresh_match_cache:
        cached_match = cached_track_match(track, match_cache, seen_at=timestamp)
        if cached_match:
            return cached_match.decision, MATCH_SOURCE_CACHE, 0
    search_result = search_spotify_track(
        track=track,
        spotify_client=spotify_client,
        access_token=access_token,
        search_limit=search_limit,
    )
    return search_result.decision, MATCH_SOURCE_SEARCH, search_result.search_count


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
            review_candidates=decision.review_candidates,
            search_queries=decision.search_queries,
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
    reason = publish_reason_with_match_details(reason, decision)
    return PlaylistPublishDecision(
        playlist_name=playlist_name,
        target_playlist_name=target_playlist_name,
        track=decision.track,
        status=status,
        spotify_uri=decision.spotify_uri,
        reason=reason,
        match_source=match_source,
        candidate=decision.candidate,
        review_candidates=decision.review_candidates,
        search_queries=decision.search_queries,
    )


def publish_reason_with_match_details(
    publish_reason: str,
    decision: TrackMatchDecision,
) -> str:
    if decision.reason.startswith(
        (
            FEATURED_ARTIST_MATCH_REASON_PREFIX,
            SPOTIFY_FEATURED_ARTIST_MATCH_REASON_PREFIX,
            LEADING_IN_TITLE_MATCH_REASON_PREFIX,
            CONSTRAINED_TYPO_MATCH_REASON_PREFIX,
            ALBUM_EXACT_TRACK_MATCH_REASON,
            ALBUM_POSITION_MATCH_REASON,
        )
    ):
        return "; ".join((publish_reason, decision.reason))
    return publish_reason_with_original_mix_match_details(publish_reason, decision)


def publish_reason_with_original_mix_match_details(
    publish_reason: str,
    decision: TrackMatchDecision,
) -> str:
    if not decision.reason.startswith(ORIGINAL_MIX_MATCH_REASON_PREFIX):
        return publish_reason
    details = [decision.reason]
    if decision.search_queries:
        details.append(f"search query: {decision.search_queries[-1]}")
    return "; ".join((publish_reason, *details))


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
    spotify_client: SpotifyPlaylistPlanningClient,
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
    spotify_client: SpotifyPlaylistPublishClient,
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
    missing_columns = missing_tunemymusic_columns(fieldnames)
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


def replace_final_items_for_playlist(
    final_items: list[FinalPlaylistItem],
    playlist_name: str,
    playlist_items: Sequence[FinalPlaylistItem],
) -> None:
    final_items[:] = [
        item
        for item in final_items
        if item.playlist_name != playlist_name
    ] + list(playlist_items)


def playlist_final_items(
    final_items: Sequence[FinalPlaylistItem],
    playlist_name: str,
) -> tuple[FinalPlaylistItem, ...]:
    return tuple(item for item in final_items if item.playlist_name == playlist_name)


def spotify_playlist_items_from_final_items(final_items: Sequence[FinalPlaylistItem]) -> tuple[SpotifyPlaylistItem, ...]:
    return tuple(
        SpotifyPlaylistItem(
            uri=item.spotify_uri,
            name=item.track_name,
            artists=item.artist_names,
            album_name=item.album_name,
            position=index,
        )
        for index, item in enumerate(final_items)
    )


def spotify_playlist_item_identity_key(target_playlist_name: str, item: SpotifyPlaylistItem) -> str:
    return spotify_track_identity_key(
        target_playlist_name=target_playlist_name,
        artist_names=item.artists,
        album_name=item.album_name,
        track_name=item.name,
    )


def source_identity_positions_by_identity(
    target_playlist_name: str,
    tracks: Sequence[PlaylistTrack],
) -> dict[str, int]:
    positions: dict[str, int] = {}
    for position, track in enumerate(tracks, start=1):
        identity_key = source_track_identity_key(target_playlist_name, track)
        if identity_key:
            positions.setdefault(identity_key, position)
    return positions


def source_tracks_by_identity_key(
    target_playlist_name: str,
    tracks: Sequence[PlaylistTrack],
) -> dict[str, PlaylistTrack]:
    tracks_by_identity: dict[str, PlaylistTrack] = {}
    for track in tracks:
        identity_key = source_track_identity_key(target_playlist_name, track)
        if identity_key:
            tracks_by_identity.setdefault(identity_key, track)
    return tracks_by_identity


def source_track_identity_key(target_playlist_name: str, track: PlaylistTrack) -> str:
    return spotify_track_identity_key(
        target_playlist_name=target_playlist_name,
        artist_names=(track.artist_name,),
        album_name=track.album_name,
        track_name=track.track_name,
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


def write_publish_summary(
    decisions: tuple[PlaylistPublishDecision, ...],
    final_items: tuple[FinalPlaylistItem, ...],
    playlist_contexts: tuple[PlaylistPublishContext, ...],
    report_path: Path,
    apply: bool,
    publisher_sync_mode: str,
    cache_hit_count: int,
    search_count: int,
    searched_row_count: int = 0,
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
        searched_row_count=searched_row_count,
        run_status=run_status,
    )
    write_publish_report(report_path, summary)
    return summary


def apply_planned_playlist_writes(
    spotify_client: SpotifyPlaylistPublishClient,
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
    searched_row_count: int = 0,
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
                searched_row_count=searched_row_count,
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
            searched_row_count=searched_row_count,
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


def default_report_path() -> Path:
    return script_report_path(__file__)


def default_publish_state_cache_path(match_cache_path: Path) -> Path:
    if match_cache_path == DEFAULT_MATCH_CACHE_PATH:
        return DEFAULT_PUBLISH_STATE_CACHE_PATH
    return match_cache_path.with_name(DEFAULT_PUBLISH_STATE_CACHE_PATH.name)


def build_spotify_publisher_argv(
    *,
    env_file: Path | None = None,
    playlist_output_dir: Path | None = None,
    report: Path | None = None,
    token_cache: Path | None = None,
    match_cache: Path | None = None,
    publish_state_cache: Path | None = None,
    publisher_config: Path | None = None,
    debug_log: Path | None = None,
    reauthorize: bool = False,
    playlists: Sequence[str] | None = None,
    search_limit: int | None = None,
    max_new_searches_per_run: int | None = None,
    publisher_sync_mode: str | None = None,
    refresh_match_cache: bool = False,
    dry_run: bool = False,
    no_progress: bool = False,
) -> list[str]:
    arguments: list[str] = []
    append_cli_option(arguments, "--env-file", env_file)
    append_cli_option(arguments, "--playlist-output-dir", playlist_output_dir)
    append_cli_option(arguments, "--report", report)
    append_cli_option(arguments, "--token-cache", token_cache)
    append_cli_option(arguments, "--match-cache", match_cache)
    append_cli_option(arguments, "--publish-state-cache", publish_state_cache)
    append_cli_option(arguments, "--publisher-config", publisher_config)
    append_cli_option(arguments, "--debug-log", debug_log)
    if reauthorize:
        arguments.append("--reauthorize")
    if playlists:
        arguments.append("--playlists")
        arguments.extend(str(playlist) for playlist in playlists)
    if no_progress:
        arguments.append("--no-progress")
    if dry_run:
        arguments.append("--publishing-dry-run")
    append_cli_option(arguments, "--search-limit", search_limit)
    append_cli_option(arguments, "--max-new-searches-per-run", max_new_searches_per_run)
    append_cli_option(arguments, "--publisher-sync-mode", publisher_sync_mode)
    if refresh_match_cache:
        arguments.append("--refresh-match-cache")
    return arguments


def build_spotify_publisher_namespace(
    *,
    env_file: Path = DEFAULT_ENV_PATH,
    playlist_output_dir: Path = DEFAULT_PLAYLIST_OUTPUT_DIRECTORY,
    report: Path | None = None,
    token_cache: Path = DEFAULT_TOKEN_CACHE_PATH,
    match_cache: Path = DEFAULT_MATCH_CACHE_PATH,
    publish_state_cache: Path = DEFAULT_PUBLISH_STATE_CACHE_PATH,
    publisher_config: Path = DEFAULT_PUBLISHER_CONFIG_PATH,
    debug_log: Path | None = None,
    reauthorize: bool = False,
    access_token: str | None = None,
    playlists: Sequence[str] | None = None,
    search_limit: int = 10,
    max_new_searches_per_run: int = DEFAULT_MAX_NEW_SEARCHES_PER_RUN,
    publisher_sync_mode: str = APPEND_SYNC_MODE,
    refresh_match_cache: bool = False,
    dry_run: bool = False,
    progress: bool = True,
) -> argparse.Namespace:
    return argparse.Namespace(
        env_file=env_file,
        playlist_output_dir=playlist_output_dir,
        report=report or default_report_path(),
        token_cache=token_cache,
        match_cache=match_cache,
        publish_state_cache=publish_state_cache,
        publisher_config=publisher_config,
        debug_log=debug_log,
        reauthorize=reauthorize,
        access_token=access_token,
        playlists=list(playlists) if playlists is not None else None,
        search_limit=search_limit,
        max_new_searches_per_run=max_new_searches_per_run,
        publisher_sync_mode=publisher_sync_mode,
        refresh_match_cache=refresh_match_cache,
        dry_run=dry_run,
        progress=progress,
        apply=not dry_run,
    )


def validate_spotify_publisher_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.search_limit < 1 or args.search_limit > 10:
        parser.error("--search-limit must be between 1 and 10")
    if args.max_new_searches_per_run < 0:
        parser.error("--max-new-searches-per-run must be non-negative")


def apply_spotify_publisher_arg_defaults(args: argparse.Namespace) -> None:
    args.apply = not args.dry_run
    args.report = args.report or default_report_path()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH, help="Local env file containing Spotify app settings. Defaults to .env.")
    parser.add_argument("--playlist-output-dir", type=Path, default=DEFAULT_PLAYLIST_OUTPUT_DIRECTORY, help="Directory containing per-playlist CSV folders.")
    parser.add_argument("--report", type=Path, help="Publish report path. Defaults to reports/<timestamp>_publish_playlist.txt.")
    parser.add_argument("--token-cache", type=Path, default=DEFAULT_TOKEN_CACHE_PATH, help="Spotify token cache path.")
    parser.add_argument("--match-cache", type=Path, default=DEFAULT_MATCH_CACHE_PATH, help="Spotify track match cache path.")
    parser.add_argument("--publish-state-cache", type=Path, default=DEFAULT_PUBLISH_STATE_CACHE_PATH, help="Spotify publish state cache path.")
    parser.add_argument("--publisher-config", type=Path, default=DEFAULT_PUBLISHER_CONFIG_PATH, help="Publisher JSON config. Defaults to config/publisher.json.")
    parser.add_argument("--debug-log", type=Path, help="Write sanitized Spotify publisher debug logs to this path.")
    parser.add_argument("--reauthorize", action="store_true", help="Force a fresh Spotify login before running the publisher.")
    parser.add_argument("--access-token", help=argparse.SUPPRESS)
    parser.add_argument("--playlists", nargs="+", help="Playlist names, folder names, folder paths, or master CSV paths to publish. Omit to process every playlist.")
    parser.add_argument("--search-limit", type=int, default=10, help="Spotify search result limit per album or track query. Defaults to 10.")
    parser.add_argument(
        "--max-new-searches-per-run",
        type=int,
        default=DEFAULT_MAX_NEW_SEARCHES_PER_RUN,
        help=(
            "Maximum uncached Spotify searches per run. "
            "Defaults to 500. Use 0 to search without a per-run cap."
        ),
    )
    parser.add_argument("--publisher-sync-mode", choices=PUBLISHER_SYNC_MODES, default=APPEND_SYNC_MODE, help="Publisher sync mode. append adds missing tracks; replace replaces playlist contents. Defaults to append.")
    parser.add_argument("--refresh-match-cache", action="store_true", help="Recheck every playlist row with Spotify and update the local track match cache.")
    parser.add_argument("--publishing-dry-run", action="store_true", dest="dry_run", help="Preview Spotify playlist changes without creating or updating playlists.")
    parser.add_argument("--no-progress", action="store_false", dest="progress", help="Disable terminal progress output.")
    args = parser.parse_args(argv)
    validate_spotify_publisher_args(parser, args)
    apply_spotify_publisher_arg_defaults(args)
    return args


def run_spotify_publish_from_args(
    args: argparse.Namespace,
    playlist_master_paths: Sequence[Path] | None = None,
    playlist_names_by_master_path: Mapping[Path, str] | None = None,
    publisher_config: PublisherConfig | None = None,
    debug_log: DebugLog | None = None,
    spotify_client: SpotifyPlaylistPlanningClient | None = None,
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
        publish_state_cache_path=args.publish_state_cache,
        publisher_config=resolved_publisher_config,
        apply=args.apply,
        publisher_sync_mode=args.publisher_sync_mode,
        info_log=print,
        refresh_match_cache=args.refresh_match_cache,
        max_new_searches_per_run=args.max_new_searches_per_run,
    )


def print_summary(summary: SpotifyPublishSummary) -> None:
    print_cli_summary(
        files=[
            f"Report: {summary.report_path}",
        ],
        processed=[
            f"Tracks: {summary.track_count}",
            f"Cache hits: {summary.cache_hit_count}",
            f"Spotify searches: {summary.search_count}",
            f"Rows searched with ladder: {summary.searched_row_count}",
            f"Matched: {summary.matched_count}",
            f"Already present: {summary.already_present_count}",
            f"Would add: {summary.would_add_count}",
            f"Added: {summary.added_count}",
            f"Would include in replacement: {summary.would_include_count}",
            f"Included in replacement: {summary.included_count}",
            f"Duplicate source tracks skipped: {summary.duplicate_in_source_count}",
            f"Ambiguous: {summary.ambiguous_count}",
            f"Unmatched: {summary.unmatched_count}",
            f"Search errors: {summary.error_count}",
        ],
    )


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    try:
        args = parse_args(argv)
        debug_log = build_debug_logger(args.debug_log)
        if debug_log:
            debug_log("start spotify_publish")
            debug_log(
                f"parsed_args playlist_selectors={len(args.playlists or ())} search_limit={args.search_limit} "
                f"max_new_searches_per_run={args.max_new_searches_per_run} "
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
    except (*EXPECTED_CLI_ERRORS, SpotifyApiError) as error:
        print_cli_error(error)
        rate_limit_stop = isinstance(error, (SpotifyRateLimitDeferredError, SpotifyRateLimitRetriesExhaustedError))
        if rate_limit_stop and args is not None and args.report.exists():
            print(f"Spotify publish report: {args.report}", file=sys.stderr)
        return 1
    print_summary(summary)
    return 0


def get_access_token_for_run(settings, force_reauthorize: bool = False) -> str:
    if force_reauthorize:
        return authorize_spotify_interactively(settings).access_token
    return get_spotify_access_token(
        settings=settings,
        required_scopes=DEFAULT_AUTHORIZE_SCOPES,
    )


if __name__ == "__main__":
    raise SystemExit(main())
