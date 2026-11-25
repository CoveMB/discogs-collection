#!/usr/bin/env python3
"""Validate and import explicit Spotify manual match overrides."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path


SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[2]
if str(SCRIPTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.manual_override_map import read_override_json, read_publisher_track_map  # noqa: E402
from publishers.spotify.env import DEFAULT_ENV_PATH, DEFAULT_TOKEN_CACHE_PATH  # noqa: E402
from publishers.spotify.match_cache import (  # noqa: E402
    MATCHER_VERSION,
    cached_manual_source_conflicts,
    clear_manual_collision_cache_entry,
    compare_cached_manual_source,
    compare_manual_source,
    load_spotify_track_match_cache,
    save_spotify_track_match_cache,
    source_bound_cached_candidates,
    spotify_track_match_key,
    spotify_url_from_uri,
    track_match_cache_entry,
    utc_timestamp,
)
from publishers.spotify.matching import (  # noqa: E402
    MANUAL_URI_ONLY_MATCH_STRATEGY,
    PlaylistTrack,
    SpotifyTrackCandidate,
    build_spotify_track_search_query,
    spotify_recording_metadata_from_values,
    spotify_track_uri_is_structurally_valid,
)
from publishers.spotify.publish_playlist import default_publish_state_cache_path, read_playlist_tracks  # noqa: E402
from shared.config_files import reject_unknown_keys  # noqa: E402
from shared.publisher_config import DEFAULT_PUBLISHER_CONFIG_PATH, SPOTIFY_PUBLISHER  # noqa: E402
from shared.text import clean_cell  # noqa: E402
from shared.workflow_paths import (  # noqa: E402
    DEFAULT_PLAYLIST_OUTPUT_DIRECTORY,
    DEFAULT_PROCESSING_CACHE_PATH,
    DEFAULT_SEEN_TERMS_CACHE_PATH,
    DEFAULT_SPOTIFY_MATCH_CACHE_PATH,
    DEFAULT_SPOTIFY_PUBLISH_STATE_CACHE_PATH,
    DEFAULT_TRACKLIST_CACHE_PATH,
)


MANUAL_MATCH_OVERRIDE_SCHEMA_VERSION = 1
PREPARED_MAP_SCHEMA_VERSION = 2
MANUAL_MATCH_OVERRIDE_RECORD_TYPE = "spotify_manual_match_overrides"
MANUAL_MATCH_REASON = "manually selected Spotify match"
SOURCE_IDENTITY_FIELDS = ("release_id", "track_number", "artist_name", "album_name", "track_name")
TARGET_DESCRIPTION_FIELDS = ("spotify_track_name", "spotify_artist_names", "spotify_album_name", "spotify_album_id")
MANUAL_MATCH_FIELDS = frozenset((*SOURCE_IDENTITY_FIELDS, "spotify_uri", *TARGET_DESCRIPTION_FIELDS))


@dataclass(frozen=True)
class ManualMatchOverride:
    source_track: PlaylistTrack
    spotify_uri: str
    spotify_track_name: str = ""
    spotify_artist_names: tuple[str, ...] = ()
    spotify_album_name: str = ""
    spotify_album_id: str = ""
    uri_only: bool = False


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
    tracks = read_playlist_tracks(playlist_output_directory)
    overrides, prepared = load_manual_match_overrides(overrides_path, tracks)
    match_cache = load_spotify_track_match_cache(match_cache_path)
    validate_existing_matches(
        overrides,
        match_cache,
        replace_existing=replace_existing,
    )
    if prepared:
        overrides = tuple(
            with_compatible_target_labels(override, match_cache, row_label=f"Spotify manual match override {index}")
            for index, override in enumerate(overrides, start=1)
        )
    if not apply or (prepared and not overrides):
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
            "matched_at": matched_at,
            "last_seen_at": matched_at,
        }
        if override.uri_only:
            record["match_strategy"] = MANUAL_URI_ONLY_MATCH_STRATEGY
        record.update(target_description(override))
        match_cache[spotify_track_match_key(track)] = record
        clear_manual_collision_cache_entry(track, match_cache)
    save_spotify_track_match_cache(match_cache_path, match_cache)
    return ManualMatchImportSummary(
        planned_count=len(overrides),
        applied_count=len(overrides),
    )


def tracks_by_import_key(tracks: Sequence[PlaylistTrack]) -> dict[str, list[PlaylistTrack]]:
    by_key: dict[str, list[PlaylistTrack]] = {}
    for track in tracks:
        by_key.setdefault(spotify_track_match_key(track), []).append(track)
    return by_key


def tracks_by_position(tracks: Sequence[PlaylistTrack]) -> dict[tuple[str, str], list[PlaylistTrack]]:
    by_position: dict[tuple[str, str], list[PlaylistTrack]] = {}
    for track in tracks:
        by_position.setdefault((track.release_id, track.track_number), []).append(track)
    return by_position


def matches_source_identity(track: PlaylistTrack, expected: PlaylistTrack) -> bool:
    return compare_manual_source(
        track,
        release_id=expected.release_id,
        track_number=expected.track_number,
        title=expected.track_name,
        artist=expected.artist_name,
        album=expected.album_name,
    ) is True


def resolve_source_position(
    release_id: str, track_number: str, sources_by_position: Mapping[tuple[str, str], Sequence[PlaylistTrack]],
) -> PlaylistTrack:
    candidates = sources_by_position.get((release_id, track_number), ())
    position = f"release {release_id}, track {track_number}"
    if not candidates:
        raise ValueError(f"has no generated playlist row for {position}")
    source = candidates[0]
    if not all(matches_source_identity(candidate, source) for candidate in candidates):
        raise ValueError(f"has conflicting or incomplete generated playlist rows for {position}")
    return source


def load_manual_match_overrides(
    path: Path, tracks: Sequence[PlaylistTrack],
) -> tuple[tuple[ManualMatchOverride, ...], bool]:
    payload = read_override_json(path, label="Spotify manual match overrides")
    if not isinstance(payload, Mapping):
        raise ValueError("Spotify manual match overrides must be a JSON object")
    version = payload.get("schema_version")
    prepared = type(version) is int and version == PREPARED_MAP_SCHEMA_VERSION
    if not prepared and version != MANUAL_MATCH_OVERRIDE_SCHEMA_VERSION:
        raise ValueError(
            f"Spotify manual match overrides schema_version must be "
            f"{MANUAL_MATCH_OVERRIDE_SCHEMA_VERSION} or {PREPARED_MAP_SCHEMA_VERSION}"
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

    source_tracks_by_key = tracks_by_import_key(tracks)
    sources_by_position = tracks_by_position(tracks) if prepared else {}
    overrides: list[ManualMatchOverride] = []
    seen_keys: set[str] = set()
    for index, row in enumerate(raw_matches, start=1):
        label = f"Spotify manual match override {index}"
        if not isinstance(row, Mapping):
            raise ValueError(f"{label} must be a JSON object")
        reject_unknown_keys(row, allowed_keys=MANUAL_MATCH_FIELDS, config_label=label)
        frozen_source = PlaylistTrack(
            playlist_name="",
            release_id=required_text(row, "release_id", index, strict=prepared),
            album_name=required_text(row, "album_name", index, strict=prepared),
            track_number=required_text(row, "track_number", index, strict=prepared),
            track_name=required_text(row, "track_name", index, strict=prepared),
            artist_name=required_text(row, "artist_name", index, strict=prepared),
            spotify_search_query="",
        )
        source = frozen_source
        if prepared:
            try:
                source = resolve_source_position(frozen_source.release_id, frozen_source.track_number, sources_by_position)
            except ValueError as error:
                raise ValueError(f"{label} {error}") from error
            if not matches_source_identity(source, frozen_source):
                raise ValueError(f"{label} does not match the current generated playlist row at the reviewed position")

        source_key = spotify_track_match_key(source)
        source_candidates = source_tracks_by_key.get(source_key, ())
        matching_sources = [candidate for candidate in source_candidates if matches_source_identity(candidate, source)]
        if not matching_sources:
            raise ValueError(f"{label} does not match a generated playlist row")
        if len(matching_sources) != len(source_candidates):
            raise ValueError(f"{label} has conflicting generated playlist rows for its cache key")
        if source_key in seen_keys:
            raise ValueError(f"{label} duplicates source track {source_key}")
        seen_keys.add(source_key)

        spotify_uri = required_text(row, "spotify_uri", index, strict=prepared)
        if not spotify_track_uri_is_structurally_valid(spotify_uri):
            raise ValueError(
                f"{label} spotify_uri must be spotify:track:<identifier> "
                "with one nonempty identifier containing no whitespace or extra separators"
            )
        if prepared and not any(field in row for field in TARGET_DESCRIPTION_FIELDS):
            overrides.append(ManualMatchOverride(matching_sources[0], spotify_uri, uri_only=True))
            continue
        album_id = (
            required_text(row, "spotify_album_id", index, strict=True)
            if prepared and "spotify_album_id" in row else clean_cell(row.get("spotify_album_id"))
        )
        overrides.append(ManualMatchOverride(
            source_track=matching_sources[0],
            spotify_uri=spotify_uri,
            spotify_track_name=required_text(row, "spotify_track_name", index, strict=prepared),
            spotify_artist_names=required_text_sequence(row, "spotify_artist_names", index, strict=prepared),
            spotify_album_name=required_text(row, "spotify_album_name", index, strict=prepared),
            spotify_album_id=album_id,
        ))
    return tuple(overrides), prepared


def required_text(
    payload: Mapping[object, object],
    field_name: str,
    index: int,
    *,
    strict: bool,
) -> str:
    raw_value = payload.get(field_name)
    if strict and not isinstance(raw_value, str):
        raise ValueError(f"Spotify manual match override {index} {field_name} must be a nonempty string")
    value = clean_cell(raw_value)
    if not value:
        raise ValueError(
            f"Spotify manual match override {index} {field_name} cannot be blank"
        )
    return value


def required_text_sequence(
    payload: Mapping[object, object],
    field_name: str,
    index: int,
    *,
    strict: bool,
) -> tuple[str, ...]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise ValueError(
            f"Spotify manual match override {index} {field_name} must be a list"
        )
    if strict and not all(isinstance(item, str) for item in value):
        raise ValueError(f"Spotify manual match override {index} {field_name} must contain strings")
    values = tuple(clean_cell(item) for item in value)
    if not values or any(not item for item in values):
        raise ValueError(
            f"Spotify manual match override {index} {field_name} cannot contain blank values"
        )
    return values


def known_target_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def explicit_manual_target(record: Mapping[str, object], uri: str) -> SpotifyTrackCandidate:
    artists = record.get("spotify_artist_names")
    artist_names = (
        tuple(artist.strip() for artist in artists)
        if isinstance(artists, list) and all(isinstance(artist, str) and artist.strip() for artist in artists)
        else ()
    )
    return SpotifyTrackCandidate(
        uri=uri,
        name=known_target_text(record.get("spotify_track_name")),
        artists=artist_names,
        album_name=known_target_text(record.get("spotify_album_name")),
        album_id=known_target_text(record.get("spotify_album_id")),
    )


def with_compatible_target_labels(
    override: ManualMatchOverride,
    match_cache: Mapping[str, Mapping[str, object]],
    *,
    row_label: str,
) -> ManualMatchOverride:
    track = override.source_track
    _, record = track_match_cache_entry(track, match_cache)
    if record is None or compare_cached_manual_source(track, record) is not True:
        return override

    candidates = source_bound_cached_candidates(record)
    if record.get("match_status") == "manual" and record.get("spotify_uri") == override.spotify_uri:
        candidates = (explicit_manual_target(record, override.spotify_uri), *candidates)
    for candidate in candidates:
        if candidate.uri != override.spotify_uri:
            continue
        metadata = spotify_recording_metadata_from_values(
            title=override.spotify_track_name,
            artist_names=override.spotify_artist_names,
            album_name=override.spotify_album_name,
            album_id=override.spotify_album_id,
        )
        if metadata.conflicts_with(candidate.recording_metadata()):
            raise ValueError(
                f"{row_label} has conflicting Spotify target descriptions for {override.spotify_uri} "
                f"(release {track.release_id}, track {track.track_number})"
            )
        override = replace(
            override,
            spotify_track_name=override.spotify_track_name or candidate.name,
            spotify_artist_names=override.spotify_artist_names or candidate.artists,
            spotify_album_name=override.spotify_album_name or candidate.album_name,
            spotify_album_id=override.spotify_album_id or candidate.album_id,
        )
    return override


def validate_existing_matches(
    overrides: Sequence[ManualMatchOverride],
    match_cache: Mapping[str, Mapping[str, object]],
    *,
    replace_existing: bool,
) -> None:
    if replace_existing:
        return
    for override in overrides:
        source_key = spotify_track_match_key(override.source_track)
        existing_record = match_cache.get(source_key)
        if existing_record is None:
            continue
        existing_status = clean_cell(existing_record.get("match_status"))
        if existing_status == "manual" and cached_manual_source_conflicts(override.source_track, existing_record):
            raise ValueError(
                f"{source_key} already has a manual Spotify match for a different source track; "
                "use --replace-existing to bind it to this source"
            )
        existing_uri = clean_cell(existing_record.get("spotify_uri"))
        if existing_status in {"matched", "manual"} and existing_uri and existing_uri != override.spotify_uri:
            raise ValueError(
                f"{source_key} already has a different {existing_status} Spotify URI; "
                "use --replace-existing to replace it"
            )


def target_description(override: ManualMatchOverride, *, include_empty: bool = False) -> dict[str, object]:
    fields: dict[str, object] = {
        "spotify_track_name": override.spotify_track_name,
        "spotify_artist_names": list(override.spotify_artist_names),
        "spotify_album_name": override.spotify_album_name,
        "spotify_album_id": override.spotify_album_id,
    }
    return fields if include_empty else {name: value for name, value in fields.items() if value}


def proposal_destination(
    path: Path,
    *,
    playlist_root: Path,
    match_cache: Path,
    protected: Sequence[Path] = (),
    option: str = "--proposals",
) -> Path:
    if path.suffix.lower() != ".json":
        raise ValueError(f"{option} must name a new .json file.")
    if os.path.lexists(path):
        raise ValueError(f"{option} already exists. Choose a new path.")
    protected_paths = (
        *protected,
        match_cache,
        default_publish_state_cache_path(match_cache),
        DEFAULT_SPOTIFY_MATCH_CACHE_PATH,
        DEFAULT_SPOTIFY_PUBLISH_STATE_CACHE_PATH,
        DEFAULT_PROCESSING_CACHE_PATH,
        DEFAULT_SEEN_TERMS_CACHE_PATH,
        DEFAULT_TRACKLIST_CACHE_PATH,
        DEFAULT_TOKEN_CACHE_PATH,
        DEFAULT_ENV_PATH,
        DEFAULT_PUBLISHER_CONFIG_PATH,
    )
    try:
        destination = path.resolve()
        protected_path = any(destination == item.resolve() for item in protected_paths)
        managed_folder = any(
            destination.is_relative_to(root.resolve())
            for root in (playlist_root, DEFAULT_PLAYLIST_OUTPUT_DIRECTORY)
        )
    except RuntimeError as error:
        raise ValueError(f"{option} could not resolve an output or protected path: {error}") from error
    if protected_path or managed_folder:
        raise ValueError(f"{option} names a protected input, cache path, or managed playlist folder.")
    if not destination.parent.is_dir():
        raise ValueError(f"{option} needs an existing parent directory.")
    return destination


def write_proposals(
    destination: Path,
    overrides: Sequence[ManualMatchOverride],
    playlist_root: Path,
    match_cache: Path,
    *,
    schema_version: int = MANUAL_MATCH_OVERRIDE_SCHEMA_VERSION,
    replace_existing: bool = False,
) -> None:
    legacy = schema_version == MANUAL_MATCH_OVERRIDE_SCHEMA_VERSION
    rows: list[dict[str, object]] = []
    for override in overrides:
        track = override.source_track
        row: dict[str, object] = {field: getattr(track, field) for field in SOURCE_IDENTITY_FIELDS}
        row["spotify_uri"] = override.spotify_uri
        if legacy or (override.spotify_track_name and override.spotify_artist_names and override.spotify_album_name):
            row.update(target_description(override, include_empty=legacy))
        rows.append(row)
    payload = {
        "schema_version": schema_version,
        "record_type": MANUAL_MATCH_OVERRIDE_RECORD_TYPE,
        "matches": rows,
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=destination.parent, prefix=".spotify-proposals-", delete=False,
    ) as output:
        temporary = Path(output.name)
        try:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.close()
            import_manual_match_overrides(temporary, playlist_root, match_cache, replace_existing=replace_existing)
            os.link(temporary, destination, follow_symlinks=False)
        finally:
            temporary.unlink(missing_ok=True)


def prepare_manual_match_map(
    draft: Path, output: Path, playlist_root: Path, match_cache_path: Path, *, replace_existing: bool,
) -> None:
    destination = proposal_destination(
        output, playlist_root=playlist_root, match_cache=match_cache_path, protected=(draft,), option="--output",
    )
    selections, skipped = read_publisher_track_map(draft, SPOTIFY_PUBLISHER)
    sources = tracks_by_position(read_playlist_tracks(playlist_root))
    match_cache = load_spotify_track_match_cache(match_cache_path)
    overrides: list[ManualMatchOverride] = []
    for (release_id, track_number), spotify_uri in selections.items():
        label = f"Spotify selection for release {release_id}, track {track_number}"
        if not spotify_track_uri_is_structurally_valid(spotify_uri):
            raise ValueError(f"{label} must use a Spotify track URI (spotify:track:<identifier>)")
        try:
            track = resolve_source_position(release_id, track_number, sources)
        except ValueError as error:
            raise ValueError(f"{label} {error}") from error
        overrides.append(with_compatible_target_labels(ManualMatchOverride(track, spotify_uri), match_cache, row_label=label))
    write_proposals(
        destination, overrides, playlist_root, match_cache_path,
        schema_version=PREPARED_MAP_SCHEMA_VERSION, replace_existing=replace_existing,
    )

    print(f"Prepared {len(overrides)} Spotify matches in {destination}")
    if skipped:
        print("Skipped publishers: " + ", ".join(repr(name) for name in skipped))
    for override in overrides:
        track = override.source_track
        print(f"- Discogs release {track.release_id}, track {track.track_number} | "
              f"{track.artist_name!r} | {track.track_name!r} | {track.album_name!r}")
        details = (", ".join(override.spotify_artist_names), override.spotify_track_name, override.spotify_album_name)
        labels = " | ".join(repr(label) if label else "(unknown)" for label in details)
        album_id = f" | album ID {override.spotify_album_id!r}" if override.spotify_album_id else ""
        print(f"  Spotify {override.spotify_uri!r} | {labels}{album_id}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare, validate, or import explicit Spotify manual match overrides.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--overrides", type=Path, help="Reviewed manual match override JSON path.")
    mode.add_argument("--prepare-map", type=Path, help="Compact publisher track map to prepare for review.")
    parser.add_argument("--output", type=Path, help="New review JSON path for --prepare-map.")
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
        help="Allow replacing a matched or manual Spotify URI or binding a manual match to a different source.",
    )
    args = parser.parse_args(argv)
    if args.prepare_map is not None:
        if args.output is None:
            parser.error("--prepare-map requires --output")
        if args.apply:
            parser.error("--apply requires a reviewed --overrides file")
    elif args.output is not None:
        parser.error("--output requires --prepare-map")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.prepare_map is not None:
            prepare_manual_match_map(
                args.prepare_map, args.output, args.playlist_output_directory, args.match_cache,
                replace_existing=args.replace_existing,
            )
            return 0
        summary = import_manual_match_overrides(
            overrides_path=args.overrides,
            playlist_output_directory=args.playlist_output_directory,
            match_cache_path=args.match_cache,
            apply=args.apply,
            replace_existing=args.replace_existing,
        )
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    action = "Applied" if args.apply else "Would apply"
    print(f"{action} manual Spotify matches: {summary.planned_count}")
    if not args.apply:
        print("No cache changes were written. Re-run with --apply after reviewing the override file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
