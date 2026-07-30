import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from shared.workflow_paths import (  # noqa: E402
    DEFAULT_COLLECTION_DIRECTORY,
    DEFAULT_CONFIGURED_RELEASE_PLAYLIST_DIRECTORY,
    DEFAULT_ENRICHED_MASTER_PATH,
    DEFAULT_ON_THE_FLY_PLAYLIST_DIRECTORY,
    DEFAULT_PLAYLIST_OUTPUT_DIRECTORY,
    DEFAULT_SPOTIFY_MATCH_CACHE_PATH,
    DEFAULT_SPOTIFY_PUBLISH_STATE_CACHE_PATH,
    DEFAULT_TRACKLIST_CACHE_PATH,
)


class WorkflowPathsTests(unittest.TestCase):
    def test_shared_collection_workflow_paths(self):
        self.assertEqual(DEFAULT_COLLECTION_DIRECTORY, Path("collection"))
        self.assertEqual(DEFAULT_ENRICHED_MASTER_PATH, Path("collection/enriched-collection.csv"))
        self.assertEqual(DEFAULT_PLAYLIST_OUTPUT_DIRECTORY, Path("collection/playlists"))
        self.assertEqual(
            DEFAULT_CONFIGURED_RELEASE_PLAYLIST_DIRECTORY,
            Path("collection/playlists/release-playlists"),
        )
        self.assertEqual(DEFAULT_ON_THE_FLY_PLAYLIST_DIRECTORY, Path("collection/playlists/on-the-fly"))
        self.assertEqual(DEFAULT_TRACKLIST_CACHE_PATH, Path("collection/cache/playlist-tracks.cache.json"))
        self.assertEqual(DEFAULT_SPOTIFY_MATCH_CACHE_PATH, Path("collection/cache/spotify-track-matches.cache.json"))
        self.assertEqual(DEFAULT_SPOTIFY_PUBLISH_STATE_CACHE_PATH, Path("collection/cache/spotify-publish-state.cache.json"))


if __name__ == "__main__":
    unittest.main()
