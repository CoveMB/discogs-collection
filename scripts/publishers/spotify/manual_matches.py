#!/usr/bin/env python3
"""Validate and import explicit Spotify manual match overrides."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[2]
if str(SCRIPTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.match_cache import (  # noqa: E402
    MATCHER_VERSION,
    load_spotify_track_match_cache,
    save_spotify_track_match_cache,
    spotify_track_match_key,
    spotify_url_from_uri,
    utc_timestamp,
)
from publishers.spotify.matching import (  # noqa: E402
    PlaylistTrack,
    build_spotify_track_search_query,
)
from publishers.spotify.publish_playlist import read_playlist_tracks  # noqa: E402
from shared.config_files import load_json_file, reject_unknown_keys  # noqa: E402
from shared.text import clean_cell  # noqa: E402
from shared.workflow_paths import (  # noqa: E402
    DEFAULT_PLAYLIST_OUTPUT_DIRECTORY,
    DEFAULT_SPOTIFY_MATCH_CACHE_PATH,
)


MANUAL_MATCH_OVERRIDE_SCHEMA_VERSION = 1
MANUAL_MATCH_OVERRIDE_RECORD_TYPE = "spotify_manual_match_overrides"
MANUAL_MATCH_REASON = "manually selected Spotify match"
MANUAL_MATCH_FIELDS = frozenset(
    (
        "release_id",
        "track_number",
        "artist_name",
        "album_name",
        "track_name",
        "spotify_uri",
        "spotify_track_name",
        "spotify_artist_names",
        "spotify_album_name",
        "spotify_album_id",
    )
)


@dataclass(frozen=True)
class ManualMatchOverride:
    source_track: PlaylistTrack
    spotify_uri: str
    spotify_track_name: str
    spotify_artist_names: tuple[str, ...]
    spotify_album_name: str
    spotify_album_id: str = ""


@dataclass(frozen=True)
class ManualMatchImportSummary:
    planned_count: int
    applied_count: int


def import_manual_match_overrides(
    overrides_path: Path,
    playlist_output_directory: Path,
    match_cache_path: Path,
    *,
    apply: bool = False,
    replace_existing: bool = False,
    timestamp: str | None = None,
) -> ManualMatchImportSummary:
    source_tracks_by_key = {
        spotify_track_match_key(track): track
        for track in read_playlist_tracks(playlist_output_directory)
    }
    overrides = load_manual_match_overrides(
        overrides_path,
        source_tracks_by_key=source_tracks_by_key,
    )
    match_cache = load_spotify_track_match_cache(match_cache_path)
    validate_existing_matches(
        overrides,
        match_cache,
        replace_existing=replace_existing,
    )
    if not apply:
        return ManualMatchImportSummary(
            planned_count=len(overrides),
            applied_count=0,
        )

    matched_at = timestamp or utc_timestamp()
    for override in overrides:
        track = override.source_track
        record: dict[str, object] = {
            "release_id": track.release_id,
            "track_number": track.track_number,
            "artist_name": track.artist_name,
            "album_name": track.album_name,
            "track_name": track.track_name,
            "search_query": build_spotify_track_search_query(track),
            "search_queries": [],
            "match_status": "manual",
            "match_reason": MANUAL_MATCH_REASON,
            "matcher_version": MATCHER_VERSION,
            "spotify_uri": override.spotify_uri,
            "spotify_url": spotify_url_from_uri(override.spotify_uri),
            "spotify_track_name": override.spotify_track_name,
            "spotify_artist_names": list(override.spotify_artist_names),
            "spotify_album_name": override.spotify_album_name,
            "matched_at": matched_at,
            "last_seen_at": matched_at,
        }
        if override.spotify_album_id:
            record["spotify_album_id"] = override.spotify_album_id
        match_cache[spotify_track_match_key(track)] = record
    save_spotify_track_match_cache(match_cache_path, match_cache)
    return ManualMatchImportSummary(
        planned_count=len(overrides),
        applied_count=len(overrides),
    )


def load_manual_match_overrides(
    path: Path,
    *,
    source_tracks_by_key: Mapping[str, PlaylistTrack],
) -> tuple[ManualMatchOverride, ...]:
    payload = load_json_file(path, malformed_label="Spotify manual match overrides")
    if not isinstance(payload, Mapping):
        raise ValueError("Spotify manual match overrides must be a JSON object")
    if payload.get("schema_version") != MANUAL_MATCH_OVERRIDE_SCHEMA_VERSION:
        raise ValueError(
            f"Spotify manual match overrides schema_version must be {MANUAL_MATCH_OVERRIDE_SCHEMA_VERSION}"
        )
    if payload.get("record_type") != MANUAL_MATCH_OVERRIDE_RECORD_TYPE:
        raise ValueError(
            f"Spotify manual match overrides record_type must be {MANUAL_MATCH_OVERRIDE_RECORD_TYPE}"
        )
    reject_unknown_keys(
        payload,
        allowed_keys=frozenset(("schema_version", "record_type", "matches")),
        config_label="Spotify manual match overrides",
    )
    raw_matches = payload.get("matches")
    if not isinstance(raw_matches, list):
        raise ValueError("Spotify manual match overrides matches must be a list")

    overrides: list[ManualMatchOverride] = []
    seen_keys: set[str] = set()
    for index, raw_match in enumerate(raw_matches, start=1):
        if not isinstance(raw_match, Mapping):
            raise ValueError(f"Spotify manual match override {index} must be a JSON object")
        reject_unknown_keys(
            raw_match,
            allowed_keys=MANUAL_MATCH_FIELDS,
            config_label=f"Spotify manual match override {index}",
        )
        source_track = PlaylistTrack(
            playlist_name="",
            release_id=required_text(raw_match, "release_id", index),
            album_name=required_text(raw_match, "album_name", index),
            track_number=required_text(raw_match, "track_number", index),
            track_name=required_text(raw_match, "track_name", index),
            artist_name=required_text(raw_match, "artist_name", index),
            spotify_search_query="",
        )
        source_key = spotify_track_match_key(source_track)
        matched_source_track = source_tracks_by_key.get(source_key)
        if matched_source_track is None:
            raise ValueError(
                f"Spotify manual match override {index} does not match a generated playlist row"
            )
        if source_key in seen_keys:
            raise ValueError(
                f"Spotify manual match override {index} duplicates source track {source_key}"
            )
        seen_keys.add(source_key)

        spotify_uri = required_text(raw_match, "spotify_uri", index)
        if not spotify_uri.startswith("spotify:track:"):
            raise ValueError(
                f"Spotify manual match override {index} spotify_uri must start with spotify:track:"
            )
        spotify_artist_names = required_text_sequence(
            raw_match,
            "spotify_artist_names",
            index,
        )
        overrides.append(
            ManualMatchOverride(
                source_track=matched_source_track,
                spotify_uri=spotify_uri,
                spotify_track_name=required_text(raw_match, "spotify_track_name", index),
                spotify_artist_names=spotify_artist_names,
                spotify_album_name=required_text(raw_match, "spotify_album_name", index),
                spotify_album_id=clean_cell(raw_match.get("spotify_album_id")),
            )
        )
    return tuple(overrides)


def required_text(
    payload: Mapping[object, object],
    field_name: str,
    index: int,
) -> str:
    value = clean_cell(payload.get(field_name))
    if not value:
        raise ValueError(
            f"Spotify manual match override {index} {field_name} cannot be blank"
        )
    return value


def required_text_sequence(
    payload: Mapping[object, object],
    field_name: str,
    index: int,
) -> tuple[str, ...]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise ValueError(
            f"Spotify manual match override {index} {field_name} must be a list"
        )
    values = tuple(clean_cell(item) for item in value)
    if not values or any(not item for item in values):
        raise ValueError(
            f"Spotify manual match override {index} {field_name} cannot contain blank values"
        )
    return values


def validate_existing_matches(
    overrides: Sequence[ManualMatchOverride],
    match_cache: Mapping[str, Mapping[str, object]],
    *,
    replace_existing: bool,
) -> None:
    for override in overrides:
        source_key = spotify_track_match_key(override.source_track)
        existing_record = match_cache.get(source_key)
        if existing_record is None:
            continue
        existing_status = clean_cell(existing_record.get("match_status"))
        existing_uri = clean_cell(existing_record.get("spotify_uri"))
        if (
            existing_status in {"matched", "manual"}
            and existing_uri
            and existing_uri != override.spotify_uri
            and not replace_existing
        ):
            raise ValueError(
                f"{source_key} already has a different {existing_status} Spotify URI; "
                "use --replace-existing to replace it"
            )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate or import explicit Spotify manual match overrides.",
    )
    parser.add_argument("--overrides", type=Path, required=True, help="Manual match override JSON path.")
    parser.add_argument(
        "--playlist-output-directory",
        type=Path,
        default=DEFAULT_PLAYLIST_OUTPUT_DIRECTORY,
        help="Playlist master directory used to validate source identities.",
    )
    parser.add_argument(
        "--match-cache",
        type=Path,
        default=DEFAULT_SPOTIFY_MATCH_CACHE_PATH,
        help="Spotify match cache path.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write validated manual matches to the local match cache. The default is preview only.",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Allow a manual match to replace a different matched or manual Spotify URI.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = import_manual_match_overrides(
            overrides_path=args.overrides,
            playlist_output_directory=args.playlist_output_directory,
            match_cache_path=args.match_cache,
            apply=args.apply,
            replace_existing=args.replace_existing,
        )
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    action = "Applied" if args.apply else "Would apply"
    print(f"{action} manual Spotify matches: {summary.planned_count}")
    if not args.apply:
        print("No cache changes were written. Re-run with --apply after reviewing the override file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
