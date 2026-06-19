import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.client import HttpResponse  # noqa: E402
from publishers.spotify.env import SpotifySettings  # noqa: E402
from publishers.spotify.session import get_spotify_access_token  # noqa: E402
from publishers.spotify.token_cache import SpotifyToken, load_spotify_token, save_spotify_token  # noqa: E402


class SpotifySessionTests(unittest.TestCase):
    def test_returns_cached_access_token_when_valid_and_scoped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_cache_path = Path(temporary_directory) / "config" / "cache" / "spotify-token.cache.json"
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
                token_cache_path=token_cache_path,
            )
            save_spotify_token(
                token_cache_path,
                SpotifyToken(
                    access_token="cached-access-token",
                    refresh_token="refresh-token",
                    expires_at=5000,
                    scope="playlist-modify-private",
                    token_type="Bearer",
                ),
            )

            access_token = get_spotify_access_token(
                settings=settings,
                required_scopes=("playlist-modify-private",),
                now=1000,
                authorize_interactively=FailingAuthorizer(),
            )

            self.assertEqual(access_token, "cached-access-token")

    def test_refreshes_expired_cached_token_and_saves_replacement(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_cache_path = Path(temporary_directory) / "config" / "cache" / "spotify-token.cache.json"
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
                token_cache_path=token_cache_path,
            )
            save_spotify_token(
                token_cache_path,
                SpotifyToken(
                    access_token="expired-access-token",
                    refresh_token="refresh-token",
                    expires_at=1000,
                    scope="playlist-modify-private",
                    token_type="Bearer",
                ),
            )

            def token_transport(request):
                return HttpResponse(
                    status=200,
                    headers={},
                    body='{"access_token":"refreshed-access-token","expires_in":3600,"scope":"playlist-modify-private","token_type":"Bearer"}',
                )

            access_token = get_spotify_access_token(
                settings=settings,
                required_scopes=("playlist-modify-private",),
                now=2000,
                token_transport=token_transport,
                authorize_interactively=FailingAuthorizer(),
            )

            self.assertEqual(access_token, "refreshed-access-token")
            self.assertEqual(load_spotify_token(token_cache_path).access_token, "refreshed-access-token")
            self.assertEqual(load_spotify_token(token_cache_path).refresh_token, "refresh-token")

    def test_refresh_preserves_existing_scope_when_spotify_omits_scope(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_cache_path = Path(temporary_directory) / "config" / "cache" / "spotify-token.cache.json"
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
                token_cache_path=token_cache_path,
            )
            save_spotify_token(
                token_cache_path,
                SpotifyToken(
                    access_token="expired-access-token",
                    refresh_token="refresh-token",
                    expires_at=1000,
                    scope="playlist-modify-private",
                    token_type="Bearer",
                ),
            )

            def token_transport(request):
                return HttpResponse(
                    status=200,
                    headers={},
                    body='{"access_token":"refreshed-access-token","expires_in":3600,"token_type":"Bearer"}',
                )

            access_token = get_spotify_access_token(
                settings=settings,
                required_scopes=("playlist-modify-private",),
                now=2000,
                token_transport=token_transport,
                authorize_interactively=FailingAuthorizer(),
            )

            self.assertEqual(access_token, "refreshed-access-token")
            self.assertEqual(load_spotify_token(token_cache_path).scope, "playlist-modify-private")

    def test_authorizes_when_refresh_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_cache_path = Path(temporary_directory) / "config" / "cache" / "spotify-token.cache.json"
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
                token_cache_path=token_cache_path,
            )
            save_spotify_token(
                token_cache_path,
                SpotifyToken(
                    access_token="expired-access-token",
                    refresh_token="bad-refresh-token",
                    expires_at=1000,
                    scope="playlist-modify-private",
                    token_type="Bearer",
                ),
            )

            def token_transport(request):
                return HttpResponse(
                    status=400,
                    headers={},
                    body='{"error":"invalid_grant"}',
                )

            authorizer = RecordingAuthorizer(
                SpotifyToken(
                    access_token="new-access-token",
                    refresh_token="new-refresh-token",
                    expires_at=5000,
                    scope="playlist-modify-private",
                    token_type="Bearer",
                )
            )

            access_token = get_spotify_access_token(
                settings=settings,
                required_scopes=("playlist-modify-private",),
                now=2000,
                token_transport=token_transport,
                authorize_interactively=authorizer,
            )

            self.assertEqual(access_token, "new-access-token")
            self.assertEqual(authorizer.calls, [settings])
            self.assertEqual(load_spotify_token(token_cache_path).access_token, "new-access-token")

    def test_discards_rejected_refresh_token_before_reauthorizing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_cache_path = Path(temporary_directory) / "config" / "cache" / "spotify-token.cache.json"
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
                token_cache_path=token_cache_path,
            )
            save_spotify_token(
                token_cache_path,
                SpotifyToken(
                    access_token="expired-access-token",
                    refresh_token="bad-refresh-token",
                    expires_at=1000,
                    scope="playlist-modify-private",
                    token_type="Bearer",
                ),
            )

            def token_transport(request):
                return HttpResponse(
                    status=400,
                    headers={},
                    body='{"error":"invalid_grant"}',
                )

            authorizer = CacheDiscardAuthorizer(
                SpotifyToken(
                    access_token="new-access-token",
                    refresh_token="new-refresh-token",
                    expires_at=5000,
                    scope="playlist-modify-private",
                    token_type="Bearer",
                )
            )

            access_token = get_spotify_access_token(
                settings=settings,
                required_scopes=("playlist-modify-private",),
                now=2000,
                token_transport=token_transport,
                authorize_interactively=authorizer,
            )

            self.assertEqual(access_token, "new-access-token")
            self.assertEqual(authorizer.calls, [settings])
            self.assertEqual(load_spotify_token(token_cache_path).access_token, "new-access-token")

    def test_authorizes_when_token_cache_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_cache_path = Path(temporary_directory) / "config" / "cache" / "spotify-token.cache.json"
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
                token_cache_path=token_cache_path,
            )
            authorizer = RecordingAuthorizer(
                SpotifyToken(
                    access_token="new-access-token",
                    refresh_token="new-refresh-token",
                    expires_at=5000,
                    scope="playlist-modify-private",
                    token_type="Bearer",
                )
            )

            access_token = get_spotify_access_token(
                settings=settings,
                required_scopes=("playlist-modify-private",),
                now=1000,
                authorize_interactively=authorizer,
            )

            self.assertEqual(access_token, "new-access-token")
            self.assertEqual(authorizer.calls, [settings])
            self.assertEqual(load_spotify_token(token_cache_path).access_token, "new-access-token")

    def test_authorizes_when_cached_token_is_missing_required_scopes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_cache_path = Path(temporary_directory) / "config" / "cache" / "spotify-token.cache.json"
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
                token_cache_path=token_cache_path,
            )
            save_spotify_token(
                token_cache_path,
                SpotifyToken(
                    access_token="cached-access-token",
                    refresh_token="refresh-token",
                    expires_at=5000,
                    scope="",
                    token_type="Bearer",
                ),
            )
            authorizer = RecordingAuthorizer(
                SpotifyToken(
                    access_token="rescoped-access-token",
                    refresh_token="new-refresh-token",
                    expires_at=5000,
                    scope="playlist-modify-private",
                    token_type="Bearer",
                )
            )

            access_token = get_spotify_access_token(
                settings=settings,
                required_scopes=("playlist-modify-private",),
                now=1000,
                authorize_interactively=authorizer,
            )

            self.assertEqual(access_token, "rescoped-access-token")
            self.assertEqual(load_spotify_token(token_cache_path).access_token, "rescoped-access-token")


class RecordingAuthorizer:
    def __init__(self, token):
        self.token = token
        self.calls = []

    def __call__(self, settings):
        self.calls.append(settings)
        return self.token


class CacheDiscardAuthorizer(RecordingAuthorizer):
    def __call__(self, settings):
        self.calls.append(settings)
        if settings.token_cache_path.exists():
            raise AssertionError("rejected refresh token cache should be discarded before reauthorization")
        return self.token


class FailingAuthorizer:
    def __call__(self, settings):
        raise AssertionError("authorization should not be needed")


if __name__ == "__main__":
    unittest.main()
