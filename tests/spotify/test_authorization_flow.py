import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.authorization_flow import authorize_spotify, parse_loopback_redirect_uri  # noqa: E402
from publishers.spotify.client import HttpResponse  # noqa: E402
from publishers.spotify.env import SpotifySettings  # noqa: E402


class SpotifyAuthorizationFlowTests(unittest.TestCase):
    def test_parse_loopback_redirect_uri_accepts_loopback_ip_literals(self):
        ipv4_callback = parse_loopback_redirect_uri("http://127.0.0.1:8765/callback")
        ipv6_callback = parse_loopback_redirect_uri("http://[::1]:8765/callback")

        self.assertEqual(ipv4_callback.host, "127.0.0.1")
        self.assertEqual(ipv4_callback.port, 8765)
        self.assertEqual(ipv4_callback.path, "/callback")
        self.assertEqual(ipv6_callback.host, "::1")
        self.assertEqual(ipv6_callback.port, 8765)
        self.assertEqual(ipv6_callback.path, "/callback")

    def test_parse_loopback_redirect_uri_rejects_non_loopback_http_hosts(self):
        invalid_redirect_uris = (
            "http://localhost:8765/callback",
            "http://0.0.0.0:8765/callback",
            "http://192.168.1.10:8765/callback",
            "https://127.0.0.1:8765/callback",
        )
        for redirect_uri in invalid_redirect_uris:
            with self.subTest(redirect_uri=redirect_uri):
                with self.assertRaises(ValueError):
                    parse_loopback_redirect_uri(redirect_uri)

    def test_authorize_spotify_exchanges_callback_code_and_saves_token_cache(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_cache_path = Path(temporary_directory) / "config" / "cache" / "spotify-token.cache.json"
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
                token_cache_path=token_cache_path,
            )
            authorization_urls = []
            token_requests = []

            def provide_code(authorization_url, expected_state):
                authorization_urls.append((authorization_url, expected_state))
                return "authorization-code"

            def transport(request):
                token_requests.append(request)
                return HttpResponse(
                    status=200,
                    headers={},
                    body=(
                        '{"access_token":"access-token","refresh_token":"refresh-token",'
                        '"expires_in":3600,"scope":"","token_type":"Bearer"}'
                    ),
                )

            token = authorize_spotify(
                settings=settings,
                code_provider=provide_code,
                token_transport=transport,
                now=1000,
            )

            self.assertEqual(token.access_token, "access-token")
            self.assertEqual(token.expires_at, 4600)
            self.assertEqual(len(authorization_urls), 1)
            self.assertIn("https://accounts.spotify.com/authorize?", authorization_urls[0][0])
            self.assertEqual(json.loads(token_cache_path.read_text(encoding="utf-8"))["access_token"], "access-token")
            self.assertIn("code=authorization-code", token_requests[0].body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
