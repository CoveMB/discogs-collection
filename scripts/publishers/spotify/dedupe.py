"""Spotify managed playlist dedupe orchestration."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from publishers.dedupe import (
    DedupeRemoval,
    PlaylistDedupePlan,
    ProviderPlaylist,
    ProviderPlaylistItem,
    plan_playlist_dedupe,
)
from publishers.spotify.client import SpotifyApiError, SpotifyClient, SpotifyPlaylist, SpotifyPlaylistItem
from shared.playlist_selection import normalize_playlist_selectors as normalize_playlist_selector_values
from shared.progress import ProgressReporter
from shared.publisher_config import (
    SPOTIFY_PUBLISHER,
    PublisherConfig,
    publisher_local_name_from_target,
    validate_publisher_naming_is_safe,
)
from shared.reports import format_report_section, format_report_title, script_report_path, write_text_report
from shared.text import clean_cell, display_report_value


MANAGED_PLAYLIST_PROVIDER = SPOTIFY_PUBLISHER

InfoLog = Callable[[str], None]


@dataclass(frozen=True)
class SkippedSpotifyPlaylist:
    playlist: SpotifyPlaylist
    reason: str


@dataclass(frozen=True)
class SpotifyDedupeSummary:
    provider_playlist_count: int
    eligible_playlist_count: int
    skipped_playlist_count: int
    track_count: int
    duplicate_count: int
    removed_count: int
    apply: bool
    report_path: Path
    run_status: str
    plans: tuple[PlaylistDedupePlan, ...]
    skipped_playlists: tuple[SkippedSpotifyPlaylist, ...]
    playlist_selectors: tuple[str, ...] = ()
    selected_playlist_names: tuple[str, ...] = ()
    unselected_eligible_playlist_count: int = 0

    @property
    def playlist_selector(self) -> str:
        return ", ".join(self.playlist_selectors)

    @property
    def selected_playlist_name(self) -> str:
        return ", ".join(self.selected_playlist_names)


def dedupe_spotify_managed_playlists(
    spotify_client: SpotifyClient,
    access_token: str,
    report_path: Path,
    publisher_config: PublisherConfig,
    apply: bool = False,
    progress: ProgressReporter | None = None,
    info_log: InfoLog | None = None,
    playlist_selectors: Sequence[str] | None = None,
    playlist_selector: str | None = None,
) -> SpotifyDedupeSummary:
    validate_publisher_naming_is_safe(publisher_config)
    normalized_playlist_selectors = normalize_playlist_selectors(
        playlist_selectors=playlist_selectors,
        playlist_selector=playlist_selector,
    )
    current_user_id = spotify_client.get_current_user_id(access_token=access_token)
    spotify_playlists = spotify_client.list_current_user_playlists(access_token=access_token)
    eligible_playlists, skipped_playlists = split_spotify_playlists_for_dedupe(
        spotify_playlists,
        current_user_id=current_user_id,
        publisher_config=publisher_config,
    )
    all_eligible_playlist_count = len(eligible_playlists)
    selected_playlist_names: tuple[str, ...] = ()
    if normalized_playlist_selectors:
        eligible_playlists = select_spotify_playlists_for_dedupe(
            eligible_playlists=eligible_playlists,
            skipped_playlists=skipped_playlists,
            playlist_selectors=normalized_playlist_selectors,
            publisher_config=publisher_config,
        )
        selected_playlist_names = tuple(playlist.name for playlist in eligible_playlists)

    items_by_playlist_id: dict[str, tuple[SpotifyPlaylistItem, ...]] = {}
    if progress:
        progress.start(len(eligible_playlists))
    try:
        for index, playlist in enumerate(eligible_playlists, start=1):
            emit_info(info_log, f"Checking Spotify playlist {playlist.name}")
            items_by_playlist_id[playlist.playlist_id] = spotify_client.get_playlist_items(
                access_token=access_token,
                playlist_id=playlist.playlist_id,
            )
            if progress:
                progress.update(index)
    finally:
        if progress:
            progress.finish()

    provider_playlists = tuple(provider_playlist_from_spotify(playlist) for playlist in eligible_playlists)
    provider_items_by_playlist_id = {
        playlist.playlist_id: tuple(
            provider_item_from_spotify(playlist, item)
            for item in items_by_playlist_id[playlist.playlist_id]
        )
        for playlist in eligible_playlists
    }
    plans = plan_playlist_dedupe(provider_playlists, provider_items_by_playlist_id)
    initial_summary = build_spotify_dedupe_summary(
        provider_playlist_count=len(spotify_playlists),
        eligible_playlist_count=len(eligible_playlists),
        skipped_playlists=skipped_playlists,
        plans=plans,
        report_path=report_path,
        apply=apply,
        removed_count=0,
        run_status="planned",
        playlist_selectors=normalized_playlist_selectors,
        selected_playlist_names=selected_playlist_names,
        unselected_eligible_playlist_count=all_eligible_playlist_count - len(eligible_playlists),
    )
    write_spotify_dedupe_report(report_path, initial_summary)

    removed_count = 0
    run_status = "dry run complete"
    if apply:
        def record_removed(count: int) -> None:
            nonlocal removed_count
            removed_count += count

        try:
            apply_spotify_dedupe_removals(
                spotify_client=spotify_client,
                access_token=access_token,
                eligible_playlists=eligible_playlists,
                info_log=info_log,
                record_removed=record_removed,
            )
            run_status = "applied"
        except (SpotifyApiError, ValueError):
            failed_summary = build_spotify_dedupe_summary(
                provider_playlist_count=len(spotify_playlists),
                eligible_playlist_count=len(eligible_playlists),
                skipped_playlists=skipped_playlists,
                plans=plans,
                report_path=report_path,
                apply=apply,
                removed_count=removed_count,
                run_status="failed during apply",
                playlist_selectors=normalized_playlist_selectors,
                selected_playlist_names=selected_playlist_names,
                unselected_eligible_playlist_count=all_eligible_playlist_count - len(eligible_playlists),
            )
            write_spotify_dedupe_report(report_path, failed_summary)
            raise

    summary = build_spotify_dedupe_summary(
        provider_playlist_count=len(spotify_playlists),
        eligible_playlist_count=len(eligible_playlists),
        skipped_playlists=skipped_playlists,
        plans=plans,
        report_path=report_path,
        apply=apply,
        removed_count=removed_count,
        run_status=run_status,
        playlist_selectors=normalized_playlist_selectors,
        selected_playlist_names=selected_playlist_names,
        unselected_eligible_playlist_count=all_eligible_playlist_count - len(eligible_playlists),
    )
    write_spotify_dedupe_report(report_path, summary)
    return summary


def apply_spotify_dedupe_removals(
    spotify_client: SpotifyClient,
    access_token: str,
    eligible_playlists: Sequence[SpotifyPlaylist],
    info_log: InfoLog | None,
    record_removed: Callable[[int], None] | None = None,
) -> int:
    removed_count = 0
    for playlist in eligible_playlists:
        current_items = spotify_client.get_playlist_items(access_token=access_token, playlist_id=playlist.playlist_id)
        plan = plan_playlist_dedupe(
            (provider_playlist_from_spotify(playlist),),
            {
                playlist.playlist_id: tuple(
                    provider_item_from_spotify(playlist, item)
                    for item in current_items
                )
            },
        )[0]
        if not plan.removals:
            continue
        final_uris = deduped_playlist_uris(current_items, plan.removals)
        emit_info(
            info_log,
            f"Replacing {playlist.name} with {len(final_uris)} deduped item(s)",
        )
        replace_spotify_playlist_items(
            spotify_client=spotify_client,
            access_token=access_token,
            playlist_id=playlist.playlist_id,
            uris=final_uris,
        )
        refreshed_items = spotify_client.get_playlist_items(access_token=access_token, playlist_id=playlist.playlist_id)
        verify_spotify_playlist_uris(playlist.name, final_uris, refreshed_items)
        removed_count += len(plan.removals)
        if record_removed:
            record_removed(len(plan.removals))
    return removed_count


def split_spotify_playlists_for_dedupe(
    playlists: Sequence[SpotifyPlaylist],
    current_user_id: str,
    publisher_config: PublisherConfig,
) -> tuple[tuple[SpotifyPlaylist, ...], tuple[SkippedSpotifyPlaylist, ...]]:
    eligible: list[SpotifyPlaylist] = []
    skipped: list[SkippedSpotifyPlaylist] = []
    for playlist in playlists:
        reason = spotify_playlist_skip_reason(playlist, current_user_id, publisher_config)
        if reason:
            skipped.append(SkippedSpotifyPlaylist(playlist=playlist, reason=reason))
        else:
            eligible.append(playlist)
    return tuple(eligible), tuple(skipped)


def select_spotify_playlists_for_dedupe(
    eligible_playlists: Sequence[SpotifyPlaylist],
    skipped_playlists: Sequence[SkippedSpotifyPlaylist],
    playlist_selectors: Sequence[str],
    publisher_config: PublisherConfig,
) -> tuple[SpotifyPlaylist, ...]:
    selected_playlists: list[SpotifyPlaylist] = []
    selected_playlist_ids: set[str] = set()
    for playlist_selector in playlist_selectors:
        playlist = select_one_spotify_playlist_for_dedupe(
            eligible_playlists=eligible_playlists,
            skipped_playlists=skipped_playlists,
            playlist_selector=playlist_selector,
            publisher_config=publisher_config,
        )
        if playlist.playlist_id in selected_playlist_ids:
            continue
        selected_playlist_ids.add(playlist.playlist_id)
        selected_playlists.append(playlist)
    return tuple(selected_playlists)


def select_one_spotify_playlist_for_dedupe(
    eligible_playlists: Sequence[SpotifyPlaylist],
    skipped_playlists: Sequence[SkippedSpotifyPlaylist],
    playlist_selector: str,
    publisher_config: PublisherConfig,
) -> SpotifyPlaylist:
    eligible_matches = tuple(
        playlist
        for playlist in eligible_playlists
        if spotify_playlist_matches_selector(playlist, playlist_selector, publisher_config)
    )
    if len(eligible_matches) == 1:
        return eligible_matches[0]
    if len(eligible_matches) > 1:
        matched_names = ", ".join(
            f"{playlist.name} ({playlist.playlist_id})"
            for playlist in eligible_matches
        )
        raise ValueError(f"playlist selector {playlist_selector!r} is ambiguous; matched eligible playlists: {matched_names}")

    skipped_matches = tuple(
        skipped
        for skipped in skipped_playlists
        if spotify_playlist_matches_selector(skipped.playlist, playlist_selector, publisher_config)
    )
    if skipped_matches:
        matched_names = ", ".join(
            f"{skipped.playlist.name} ({skipped.reason})"
            for skipped in skipped_matches
        )
        raise ValueError(f"playlist selector {playlist_selector!r} matched only skipped playlists: {matched_names}")

    raise ValueError(f"no eligible Spotify playlist matched playlist selector: {playlist_selector}")


def spotify_playlist_matches_selector(
    playlist: SpotifyPlaylist,
    playlist_selector: str,
    publisher_config: PublisherConfig,
) -> bool:
    normalized_selector = selector_match_key(playlist_selector)
    local_name = publisher_local_name_from_target(playlist.name, publisher_config)
    candidate_values = (playlist.playlist_id, playlist.name, local_name or "")
    return any(selector_match_key(candidate) == normalized_selector for candidate in candidate_values)


def normalize_playlist_selectors(
    playlist_selectors: Sequence[str] | None = None,
    playlist_selector: str | None = None,
) -> tuple[str, ...]:
    if playlist_selectors is not None and playlist_selector is not None:
        raise ValueError("playlist_selector cannot be used with playlist_selectors")
    selector_values: Sequence[str] | None
    if playlist_selector is not None:
        selector_values = (playlist_selector,)
    else:
        selector_values = playlist_selectors
    return normalize_playlist_selector_values(
        selector_values,
        blank_error="playlist selector cannot be blank",
        all_error="playlist selector 'all' is not allowed; omit --playlists to process every eligible playlist",
    )


def selector_match_key(value: str) -> str:
    return clean_cell(value).casefold()


def spotify_playlist_skip_reason(
    playlist: SpotifyPlaylist,
    current_user_id: str,
    publisher_config: PublisherConfig,
) -> str:
    if playlist.owner_id != current_user_id:
        return "not_owned_by_current_user"
    if playlist.public is not False:
        return "public_playlist" if playlist.public is True else "not_private_playlist"
    if playlist.collaborative:
        return "collaborative_playlist"
    if publisher_local_name_from_target(playlist.name, publisher_config) is None:
        return "not_publisher_managed"
    return ""


def provider_playlist_from_spotify(playlist: SpotifyPlaylist) -> ProviderPlaylist:
    return ProviderPlaylist(
        provider=MANAGED_PLAYLIST_PROVIDER,
        playlist_id=playlist.playlist_id,
        name=playlist.name,
    )


def provider_item_from_spotify(playlist: SpotifyPlaylist, item: SpotifyPlaylistItem) -> ProviderPlaylistItem:
    return ProviderPlaylistItem(
        playlist_id=playlist.playlist_id,
        playlist_name=playlist.name,
        uri=item.uri,
        position=item.position,
        added_at=item.added_at,
        name=item.name,
        artists=item.artists,
        album_name=item.album_name,
    )


def deduped_playlist_uris(
    current_items: Sequence[SpotifyPlaylistItem],
    removals: Sequence[DedupeRemoval],
) -> tuple[str, ...]:
    positions_to_remove = {removal.item.position for removal in removals}
    return tuple(
        item.uri
        for item in current_items
        if item.position not in positions_to_remove and item.uri
    )


def replace_spotify_playlist_items(
    spotify_client: SpotifyClient,
    access_token: str,
    playlist_id: str,
    uris: Sequence[str],
) -> None:
    first_batch = tuple(uris[:100])
    remaining_uris = tuple(uris[100:])
    spotify_client.replace_playlist_items(
        access_token=access_token,
        playlist_id=playlist_id,
        uris=first_batch,
    )
    if remaining_uris:
        spotify_client.add_playlist_items(
            access_token=access_token,
            playlist_id=playlist_id,
            uris=remaining_uris,
        )


def verify_spotify_playlist_uris(
    playlist_name: str,
    expected_uris: Sequence[str],
    actual_items: Sequence[SpotifyPlaylistItem],
) -> None:
    actual_uris = tuple(item.uri for item in actual_items if item.uri)
    if tuple(expected_uris) != actual_uris:
        raise ValueError(
            f"{playlist_name}: Spotify playlist replacement verification failed; "
            f"expected {len(expected_uris)} item(s), found {len(actual_uris)}"
        )


def build_spotify_dedupe_summary(
    provider_playlist_count: int,
    eligible_playlist_count: int,
    skipped_playlists: Sequence[SkippedSpotifyPlaylist],
    plans: Sequence[PlaylistDedupePlan],
    report_path: Path,
    apply: bool,
    removed_count: int,
    run_status: str,
    playlist_selectors: Sequence[str] = (),
    selected_playlist_names: Sequence[str] = (),
    unselected_eligible_playlist_count: int = 0,
) -> SpotifyDedupeSummary:
    return SpotifyDedupeSummary(
        provider_playlist_count=provider_playlist_count,
        eligible_playlist_count=eligible_playlist_count,
        skipped_playlist_count=len(skipped_playlists),
        track_count=sum(plan.item_count for plan in plans),
        duplicate_count=sum(plan.duplicate_count for plan in plans),
        removed_count=removed_count,
        apply=apply,
        report_path=report_path,
        run_status=run_status,
        plans=tuple(plans),
        skipped_playlists=tuple(skipped_playlists),
        playlist_selectors=tuple(playlist_selectors),
        selected_playlist_names=tuple(selected_playlist_names),
        unselected_eligible_playlist_count=unselected_eligible_playlist_count,
    )


def write_spotify_dedupe_report(path: Path, summary: SpotifyDedupeSummary) -> None:
    title = "Spotify managed playlist dedupe report" if summary.apply else "Spotify managed playlist dedupe dry-run report"
    lines = format_report_title(title)
    lines.extend(
        format_report_section(
            "Summary",
            [
                f"- Run status: {summary.run_status}",
                f"- Apply mode: {summary.apply}",
                f"- Provider playlists fetched: {summary.provider_playlist_count}",
                f"- Eligible playlists: {summary.eligible_playlist_count}",
                f"- Skipped playlists: {summary.skipped_playlist_count}",
                f"- Tracks checked: {summary.track_count}",
                f"- Duplicate tracks planned for removal: {summary.duplicate_count}",
                f"- Duplicate tracks removed: {summary.removed_count}",
                *format_playlist_selector_summary(summary),
            ],
        )
    )
    lines.extend(
        format_report_section(
            "Duplicate removals",
            format_dedupe_removals(summary.plans),
        )
    )
    lines.extend(
        format_report_section(
            "Skipped playlists",
            format_skipped_playlists(summary.skipped_playlists),
        )
    )
    write_text_report(path, lines)


def format_dedupe_removals(plans: Sequence[PlaylistDedupePlan]) -> list[str]:
    lines: list[str] = []
    for plan in plans:
        for removal in plan.removals:
            item = removal.item
            kept_item = removal.kept_item
            lines.append(
                f"- {plan.playlist.name} | remove | {item.position + 1} | {item.uri} | "
                f"{display_report_value(item.name)} | kept position {kept_item.position + 1}"
            )
    return lines or ["- None"]


def format_playlist_selector_summary(summary: SpotifyDedupeSummary) -> list[str]:
    if not summary.playlist_selectors:
        return []
    return [
        f"- Playlist selectors: {', '.join(summary.playlist_selectors)}",
        f"- Selected playlists: {', '.join(summary.selected_playlist_names)}",
        f"- Other eligible playlists not checked: {summary.unselected_eligible_playlist_count}",
    ]


def format_skipped_playlists(skipped_playlists: Sequence[SkippedSpotifyPlaylist]) -> list[str]:
    return [
        f"- {skipped.playlist.name} | skipped | {skipped.reason}"
        for skipped in skipped_playlists
    ] or ["- None"]


def default_report_path() -> Path:
    return script_report_path(__file__)


def emit_info(info_log: InfoLog | None, message: str) -> None:
    if info_log:
        info_log(message)
