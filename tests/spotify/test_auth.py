import sys
import unittest
import urllib.parse
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.auth import build_authorization_url  # noqa: E402
from publishers.spotify.env import SpotifySettings  # noqa: E402


class SpotifyAuthTests(unittest.TestCase):
    def test_builds_pkce_authorization_url_from_env_settings(self):
        settings = SpotifySettings(
            client_id="client-id",
            redirect_uri="http://127.0.0.1:8765/callback",
        )

        url = build_authorization_url(
            settings=settings,
            state="state-value",
            code_challenge="challenge-value",
            scopes=("playlist-modify-private",),
        )

        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "accounts.spotify.com")
        self.assertEqual(parsed.path, "/authorize")
        self.assertEqual(query["client_id"], ["client-id"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["redirect_uri"], ["http://127.0.0.1:8765/callback"])
        self.assertEqual(query["state"], ["state-value"])
        self.assertEqual(query["code_challenge"], ["challenge-value"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["scope"], ["playlist-modify-private"])


if __name__ == "__main__":
    unittest.main()
