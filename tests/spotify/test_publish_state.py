import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.matching import PlaylistTrack  # noqa: E402
from publishers.spotify.publish_state import (  # noqa: E402
    load_spotify_publish_state,
    record_spotify_publish_state_track,
    save_spotify_publish_state,
    spotify_publish_state_has_track,
)


class SpotifyPublishStateTests(unittest.TestCase):
    def test_missing_publish_state_cache_loads_empty(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "collection" / "cache" / "spotify-publish-state.cache.json"

            self.assertEqual(load_spotify_publish_state(path), {})

    def test_saves_and_loads_publish_state_records(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "collection" / "cache" / "spotify-publish-state.cache.json"
            state = {}
            track = PlaylistTrack(
                playlist_name="House",
                release_id="111",
                album_name="Alpha Album",
                track_number="1",
                track_name="Alpha One",
                artist_name="Alpha Artist",
                spotify_search_query="Alpha Artist Alpha One Alpha Album",
            )

            record_spotify_publish_state_track(
                state=state,
                playlist_name="Discogs - House",
                identity_key="discogs house|alpha artist|alpha album|alpha one",
                spotify_uri="spotify:track:alpha",
                source_position=3,
                track=track,
                timestamp="2026-07-09T00:00:00Z",
                event="published",
            )
            save_spotify_publish_state(path, state)
            loaded = load_spotify_publish_state(path)

        self.assertTrue(
            spotify_publish_state_has_track(
                loaded,
                "Discogs - House",
                "discogs house|alpha artist|alpha album|alpha one",
            )
        )
        record = loaded["Discogs - House"]["discogs house|alpha artist|alpha album|alpha one"]
        self.assertEqual(record["spotify_uri"], "spotify:track:alpha")
        self.assertEqual(record["last_source_position"], 3)
        self.assertEqual(record["release_id"], "111")
        self.assertEqual(record["last_published_at"], "2026-07-09T00:00:00Z")

    def test_rejects_unsupported_publish_state_cache(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "state.json"
            path.write_text(json.dumps({"schema_version": 999, "record_type": "old"}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unsupported Spotify publish state cache format"):
                load_spotify_publish_state(path)


if __name__ == "__main__":
    unittest.main()
