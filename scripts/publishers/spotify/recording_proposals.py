"""Check raw Spotify recording evidence and export proposals for manual import."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from publishers.spotify.client import parse_search_track_items
from publishers.spotify.manual_matches import (
    MANUAL_MATCH_OVERRIDE_RECORD_TYPE,
    MANUAL_MATCH_OVERRIDE_SCHEMA_VERSION,
    ManualMatchOverride,
    import_manual_match_overrides,
)
from publishers.spotify.matching import (
    PlaylistTrack,
    SpotifyTrackCandidate,
    normalize_recording_evidence_text,
    normalize_spotify_isrc,
)
from publishers.spotify.musicbrainz_recordings import comparable_durations, duration_ms


@dataclass(frozen=True)
class SpotifyRecordingHit:
    track: SpotifyTrackCandidate
    duration_ms: int
    artist_ids: tuple[str, ...]


def spotify_id_valid(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]+", value))


def track_id(uri: str) -> str:
    identifier = uri.removeprefix("spotify:track:")
    return identifier if identifier != uri and spotify_id_valid(identifier) else ""


def artist_ids(item: Mapping[str, object]) -> tuple[str, ...]:
    artists = item.get("artists")
    return tuple(
        artist["id"] if isinstance(artist, dict) and isinstance(artist.get("id"), str) else ""
        for artist in artists
    ) if isinstance(artists, list) else ()


def unavailable(item: Mapping[str, object]) -> bool:
    album = item.get("album")
    return any(
        item.get(field) not in allowed for field, allowed in (
            ("restrictions", (None, {})),
            ("linked_from", (None, {})),
        )
    ) or (
        ("is_local" in item and item["is_local"] is not False)
        or (isinstance(album, dict) and album.get("restrictions") not in (None, {}))
    )


def conflicting_observation(
    hit: SpotifyRecordingHit, observation: SpotifyTrackCandidate, duration: int | None, ids: tuple[str, ...],
    observations_conflict: Callable[[SpotifyTrackCandidate, SpotifyTrackCandidate], bool],
) -> bool:
    return (
        observations_conflict(hit.track, observation)
        or (observation.is_playable is False)
        or (duration is not None and duration != hit.duration_ms)
        or (len(ids) == len(hit.artist_ids) and any(
            left and right and left != right for left, right in zip(hit.artist_ids, ids, strict=True)
        ))
    )


def check_spotify_recordings(
    items: Sequence[object], isrc: str, durations: tuple[int, int], candidate_uris: set[str],
    cached_hits: Sequence[SpotifyTrackCandidate], catalog_observations: Sequence[object],
    matches_source: Callable[[SpotifyTrackCandidate], bool],
    observations_conflict: Callable[[SpotifyTrackCandidate, SpotifyTrackCandidate], bool],
) -> tuple[tuple[SpotifyRecordingHit, ...], str]:
    by_uri: dict[str, list[SpotifyRecordingHit]] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "track":
            return (), "Spotify returned a malformed ISRC search track."
        parsed = parse_search_track_items((item,))
        duration = duration_ms(item.get("duration_ms"))
        if not parsed or duration is None or not parsed[0].has_complete_recording_metadata:
            return (), "Spotify returned incomplete ISRC search track metadata."
        track = parsed[0]
        identifier = track_id(track.uri)
        if not identifier or item.get("uri") != track.uri or item.get("id") != identifier or not spotify_id_valid(track.album_id):
            return (), "Spotify returned invalid track or album identifiers."
        if item.get("is_playable") is not True or unavailable(item):
            return (), "Spotify did not confirm unrestricted playability for every ISRC result."
        if track.isrc != isrc or not matches_source(track) or not comparable_durations(*durations, duration):
            return (), "An ISRC result contradicts the source code, title, version, credits or duration."
        ids = artist_ids(item)
        if any(artist_id and not spotify_id_valid(artist_id) for artist_id in ids):
            return (), "Spotify returned an invalid artist identifier."
        by_uri.setdefault(track.uri, []).append(SpotifyRecordingHit(track, duration, ids))

    # Keep duplicates through validation. The first result may hide a later disagreement.
    for variants in by_uri.values():
        if any(conflicting_observation(left, right.track, right.duration_ms, right.artist_ids, observations_conflict)
               for left, right in combinations(variants, 2)):
            return (), "Spotify returned contradictory ISRC observations for one URI."
    all_hits = tuple(variants[0] for variants in by_uri.values())
    if len({hit.track.album_id for hit in all_hits}) != len(all_hits):
        return (), "More than one ISRC result uses the same Spotify album ID."

    for observation in cached_hits:
        if observation.isrc == isrc and not matches_source(observation):
            return (), "A cached observation for this ISRC contradicts the source recording."
        if any(conflicting_observation(hit, observation, None, (), observations_conflict)
               for hit in by_uri.get(observation.uri, ())):
            return (), "A cached observation contradicts Spotify's ISRC evidence for one URI."

    for item in catalog_observations:
        if not isinstance(item, dict):
            continue
        parsed = parse_search_track_items((item,))
        if not parsed:
            continue
        observation = parsed[0]
        variants = by_uri.get(observation.uri, ())
        duration = duration_ms(item.get("duration_ms"))
        if observation.isrc == isrc and (not variants or not matches_source(observation)):
            return (), "Another Spotify search disagrees with the complete Spotify ISRC results."
        external_ids = item.get("external_ids")
        if variants and (
            unavailable(item)
            or item.get("type", "track") != "track"
            or ("id" in item and item["id"] != track_id(observation.uri))
            or ("external_ids" in item and not isinstance(external_ids, dict))
            or (isinstance(external_ids, dict) and "isrc" in external_ids and not normalize_spotify_isrc(external_ids["isrc"]))
            or ("duration_ms" in item and duration is None)
            or any(conflicting_observation(hit, observation, duration, artist_ids(item), observations_conflict)
                   for hit in variants)
        ):
            return (), "Another search contradicts Spotify's ISRC evidence for one URI."

    eligible = tuple(hit for hit in all_hits if hit.track.uri in candidate_uris)
    if not eligible:
        return (), "The complete Spotify ISRC result does not verify an unresolved candidate."
    return eligible, ""


def ordered_editions(source: PlaylistTrack, hits: Sequence[SpotifyRecordingHit]) -> tuple[SpotifyRecordingHit, ...]:
    album = normalize_recording_evidence_text(source.album_name)
    return tuple(sorted(hits, key=lambda hit: (
        normalize_recording_evidence_text(hit.track.album_name) != album, hit.track.uri,
    )))


def proposal_destination(path: Path, *, protected: Sequence[Path]) -> Path:
    if path.suffix.lower() != ".json":
        raise ValueError("--proposals must name a new .json file.")
    if os.path.lexists(path):
        raise ValueError("--proposals already exists. Choose a new path.")
    destination = path.resolve()
    if any(destination == input_path.resolve() for input_path in protected):
        raise ValueError("--proposals names a protected input or cache path.")
    if not destination.parent.is_dir():
        raise ValueError("--proposals needs an existing parent directory.")
    return destination


def write_proposals(
    destination: Path, overrides: Sequence[ManualMatchOverride], playlist_root: Path, match_cache: Path,
) -> None:
    payload = {
        "schema_version": MANUAL_MATCH_OVERRIDE_SCHEMA_VERSION,
        "record_type": MANUAL_MATCH_OVERRIDE_RECORD_TYPE,
        "matches": [{
            "release_id": item.source_track.release_id,
            "track_number": item.source_track.track_number,
            "artist_name": item.source_track.artist_name,
            "album_name": item.source_track.album_name,
            "track_name": item.source_track.track_name,
            "spotify_uri": item.spotify_uri,
            "spotify_track_name": item.spotify_track_name,
            "spotify_artist_names": item.spotify_artist_names,
            "spotify_album_name": item.spotify_album_name,
            "spotify_album_id": item.spotify_album_id,
        } for item in overrides],
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=destination.parent, prefix=".spotify-proposals-", delete=False,
    ) as output:
        temporary = Path(output.name)
        try:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.close()
            import_manual_match_overrides(temporary, playlist_root, match_cache)
            os.link(temporary, destination, follow_symlinks=False)
        finally:
            temporary.unlink()
