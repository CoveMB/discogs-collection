"""Provider-neutral playlist dedupe planning."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPlaylist:
    provider: str
    playlist_id: str
    name: str


@dataclass(frozen=True)
class ProviderPlaylistItem:
    playlist_id: str
    playlist_name: str
    uri: str
    position: int
    added_at: str = ""
    name: str = ""
    artists: tuple[str, ...] = ()
    album_name: str = ""


@dataclass(frozen=True)
class DedupeRemoval:
    playlist: ProviderPlaylist
    item: ProviderPlaylistItem
    kept_item: ProviderPlaylistItem
    reason: str


@dataclass(frozen=True)
class PlaylistDedupePlan:
    playlist: ProviderPlaylist
    item_count: int
    unique_uri_count: int
    duplicate_count: int
    removals: tuple[DedupeRemoval, ...]


def plan_playlist_dedupe(
    playlists: Sequence[ProviderPlaylist],
    items_by_playlist_id: Mapping[str, Sequence[ProviderPlaylistItem]],
) -> tuple[PlaylistDedupePlan, ...]:
    return tuple(
        plan_single_playlist_dedupe(
            playlist=playlist,
            items=tuple(items_by_playlist_id.get(playlist.playlist_id, ())),
        )
        for playlist in playlists
    )


def plan_single_playlist_dedupe(
    playlist: ProviderPlaylist,
    items: Sequence[ProviderPlaylistItem],
) -> PlaylistDedupePlan:
    kept_by_uri: dict[str, ProviderPlaylistItem] = {}
    removals: list[DedupeRemoval] = []
    items_by_uri: dict[str, list[ProviderPlaylistItem]] = {}
    for item in items:
        uri = normalized_uri(item.uri)
        if not uri:
            continue
        items_by_uri.setdefault(uri, []).append(item)

    for uri, duplicate_candidates in items_by_uri.items():
        kept_item = select_playlist_item_to_keep(duplicate_candidates)
        kept_by_uri[uri] = kept_item
        for item in sorted(duplicate_candidates, key=lambda candidate: candidate.position):
            if item is kept_item:
                continue
            removals.append(
                DedupeRemoval(
                    playlist=playlist,
                    item=item,
                    kept_item=kept_item,
                    reason="same track URI already kept from an earlier added item",
                )
            )

    removals.sort(key=lambda removal: (removal.playlist.name, removal.item.position))
    return PlaylistDedupePlan(
        playlist=playlist,
        item_count=len(items),
        unique_uri_count=len(kept_by_uri),
        duplicate_count=len(removals),
        removals=tuple(removals),
    )


def select_playlist_item_to_keep(items: Sequence[ProviderPlaylistItem]) -> ProviderPlaylistItem:
    parsed_items = tuple(
        (item, parse_spotify_timestamp(item.added_at))
        for item in items
    )
    if parsed_items and all(parsed_added_at is not None for _, parsed_added_at in parsed_items):
        return min(
            parsed_items,
            key=lambda item_with_added_at: (item_with_added_at[1], item_with_added_at[0].position),
        )[0]
    return min(items, key=lambda item: item.position)


def parse_spotify_timestamp(value: str) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalized_uri(value: str) -> str:
    return str(value or "").strip()
