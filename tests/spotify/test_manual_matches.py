import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.manual_matches import import_manual_match_overrides
from publishers.spotify.match_cache import (
    MATCHER_VERSION,
    cached_track_match,
    load_spotify_track_match_cache,
)
from publishers.spotify.matching import PlaylistTrack
from shared.tunemymusic import TUNEMYMUSIC_COLUMNS


def write_playlist_master(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(TUNEMYMUSIC_COLUMNS))
        writer.writeheader()
        writer.writerow(
            {
                "Release Id": "release-1",
                "Album Name": "Source Album",
                "Track Number": "1",
                "Track Name": "Source Track",
                "Artist Name": "Source Artist",
                "Spotify Search Query": "Source Artist Source Track Source Album",
            }
        )


def write_override_file(path: Path, *, spotify_uri: str = "spotify:track:manual-one") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_type": "spotify_manual_match_overrides",
                "matches": [
                    {
                        "release_id": "release-1",
                        "track_number": "1",
                        "artist_name": "Source Artist",
                        "album_name": "Source Album",
                        "track_name": "Source Track",
                        "spotify_uri": spotify_uri,
                        "spotify_track_name": "Spotify Track",
                        "spotify_artist_names": ["Spotify Artist"],
                        "spotify_album_name": "Spotify Album",
                        "spotify_album_id": "spotify-album-one",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class SpotifyManualMatchTests(unittest.TestCase):
    def test_preview_validates_override_without_writing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
            overrides_path = directory / "manual-matches.json"
            write_override_file(overrides_path)
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"

            summary = import_manual_match_overrides(
                overrides_path=overrides_path,
                playlist_output_directory=playlist_directory,
                match_cache_path=match_cache_path,
                apply=False,
            )
            cache_exists_after_preview = match_cache_path.exists()

        self.assertEqual(summary.planned_count, 1)
        self.assertEqual(summary.applied_count, 0)
        self.assertFalse(cache_exists_after_preview)

    def test_apply_writes_authoritative_manual_cache_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
            overrides_path = directory / "manual-matches.json"
            write_override_file(overrides_path)
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"

            summary = import_manual_match_overrides(
                overrides_path=overrides_path,
                playlist_output_directory=playlist_directory,
                match_cache_path=match_cache_path,
                apply=True,
                timestamp="2026-07-17T00:00:00Z",
            )
            payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            records = load_spotify_track_match_cache(match_cache_path)
            track = PlaylistTrack(
                playlist_name="Techno",
                release_id="release-1",
                album_name="Source Album",
                track_number="1",
                track_name="Source Track",
                artist_name="Source Artist",
                spotify_search_query="Source Artist Source Track Source Album",
            )
            cached = cached_track_match(track, records)

        self.assertEqual(summary.applied_count, 1)
        record = payload["matches"]["release-1|1|source artist|source album|source track"]
        self.assertEqual(record["match_status"], "manual")
        self.assertEqual(record["match_reason"], "manually selected Spotify match")
        self.assertEqual(record["spotify_uri"], "spotify:track:manual-one")
        self.assertEqual(record["spotify_album_id"], "spotify-album-one")
        self.assertEqual(record["matched_at"], "2026-07-17T00:00:00Z")
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.decision.spotify_uri, "spotify:track:manual-one")

    def test_rejects_override_for_unknown_source_track(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
            overrides_path = directory / "manual-matches.json"
            write_override_file(overrides_path)
            payload = json.loads(overrides_path.read_text(encoding="utf-8"))
            payload["matches"][0]["track_name"] = "Unknown Track"
            overrides_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "does not match a generated playlist row"):
                import_manual_match_overrides(
                    overrides_path=overrides_path,
                    playlist_output_directory=playlist_directory,
                    match_cache_path=directory / "matches.json",
                    apply=False,
                )

    def test_rejects_non_track_spotify_uri(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
            overrides_path = directory / "manual-matches.json"
            write_override_file(overrides_path, spotify_uri="https://open.spotify.com/track/manual-one")

            with self.assertRaisesRegex(ValueError, "spotify_uri must start with spotify:track:"):
                import_manual_match_overrides(
                    overrides_path=overrides_path,
                    playlist_output_directory=playlist_directory,
                    match_cache_path=directory / "matches.json",
                    apply=False,
                )

    def test_replacing_different_matched_record_requires_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
            overrides_path = directory / "manual-matches.json"
            write_override_file(overrides_path)
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": {
                            "release-1|1|source artist|source album|source track": {
                                "release_id": "release-1",
                                "track_number": "1",
                                "artist_name": "Source Artist",
                                "album_name": "Source Album",
                                "track_name": "Source Track",
                                "match_status": "matched",
                                "match_reason": "track, artist, and album matched",
                                "matcher_version": MATCHER_VERSION,
                                "spotify_uri": "spotify:track:existing",
                                "spotify_track_name": "Source Track",
                                "spotify_artist_names": ["Source Artist"],
                                "spotify_album_name": "Source Album",
                                "matched_at": "2026-07-17T00:00:00Z",
                                "last_seen_at": "2026-07-17T00:00:00Z",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "use --replace-existing"):
                import_manual_match_overrides(
                    overrides_path=overrides_path,
                    playlist_output_directory=playlist_directory,
                    match_cache_path=match_cache_path,
                    apply=True,
                )

            summary = import_manual_match_overrides(
                overrides_path=overrides_path,
                playlist_output_directory=playlist_directory,
                match_cache_path=match_cache_path,
                apply=True,
                replace_existing=True,
            )

        self.assertEqual(summary.applied_count, 1)


if __name__ == "__main__":
    unittest.main()
