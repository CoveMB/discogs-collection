import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_playlist_exporter as exporter  # noqa: E402
import discogs_tracklists as tracklists  # noqa: E402


class DiscogsTracklistStructureTests(unittest.TestCase):
    def test_exporter_reexports_tracklist_infrastructure_from_tracklist_module(self):
        self.assertIs(exporter.DiscogsTrack, tracklists.DiscogsTrack)
        self.assertIs(exporter.ReleaseTracklistLookup, tracklists.ReleaseTracklistLookup)
        self.assertIs(exporter.release_tracklist_from_payload, tracklists.release_tracklist_from_payload)
        self.assertIs(exporter.make_cached_tracklist_lookup, tracklists.make_cached_tracklist_lookup)
        self.assertIs(exporter.load_tracklist_cache, tracklists.load_tracklist_cache)
        self.assertIs(exporter.save_tracklist_cache, tracklists.save_tracklist_cache)

    def test_release_tracklist_from_payload_handles_nested_discogs_tracks(self):
        row = {"release_id": "444", "Artist": "Row Artist", "Title": "Row Album", "Released": "1998"}
        payload = {
            "artists": [{"name": "Payload Artist"}],
            "title": "Payload Album",
            "year": 2002,
            "tracklist": [
                {
                    "position": "A",
                    "type_": "index",
                    "title": "Suite",
                    "sub_tracks": [
                        {
                            "position": "A1",
                            "type_": "track",
                            "title": "Part One",
                            "artists": [{"name": "Track Artist"}],
                        },
                    ],
                },
                {"position": "B1", "type_": "track", "title": "Part Two"},
                {"position": "", "type_": "heading", "title": "Bonus"},
            ],
        }

        lookup = tracklists.release_tracklist_from_payload("444", payload, row)

        self.assertEqual(lookup.artist_name, "Payload Artist")
        self.assertEqual(lookup.album_name, "Payload Album")
        self.assertEqual(lookup.record_year, "2002")
        self.assertEqual(
            lookup.tracks,
            (
                tracklists.DiscogsTrack(position="A1", title="Part One", artist_name="Track Artist"),
                tracklists.DiscogsTrack(position="B1", title="Part Two", artist_name="Payload Artist"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
