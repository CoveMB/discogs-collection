import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.token_cache import SpotifyToken, load_spotify_token, save_spotify_token  # noqa: E402


class SpotifyTokenCacheTests(unittest.TestCase):
    def test_saves_and_loads_spotify_token_cache(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config" / "cache" / "spotify-token.cache.json"
            token = SpotifyToken(
                access_token="access-token",
                refresh_token="refresh-token",
                expires_at=12345,
                scope="",
                token_type="Bearer",
            )

            save_spotify_token(path, token)
            loaded = load_spotify_token(path)

            self.assertEqual(loaded, token)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {
                    "schema_version": 1,
                    "record_type": "spotify_oauth_token",
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_at": 12345,
                    "scope": "",
                    "token_type": "Bearer",
                },
            )

    def test_missing_spotify_token_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config" / "cache" / "spotify-token.cache.json"

            self.assertIsNone(load_spotify_token(path))


if __name__ == "__main__":
    unittest.main()
