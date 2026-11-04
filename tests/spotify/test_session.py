import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.client import HttpResponse  # noqa: E402
from publishers.spotify.env import SpotifySettings  # noqa: E402
from publishers.spotify.session import get_spotify_access_token  # noqa: E402
from publishers.spotify.token_cache import SpotifyToken, load_spotify_token, save_spotify_token  # noqa: E402


def load_required_spotify_token(path: Path) -> SpotifyToken:
    token = load_spotify_token(path)
    if token is None:
        raise AssertionError(f"Spotify token was not saved to {path}")
    return token


class RenewableSpotifySessionTests(unittest.TestCase):
    def test_refresh_rotation_is_saved_before_use_and_valid_token_is_reused(self):
        from publishers.spotify.session import SpotifySession

        with tempfile.TemporaryDirectory() as directory:
            settings = SpotifySettings("client", "http://localhost/callback", Path(directory) / "token.json")
            token = SpotifyToken("old-secret", "refresh-secret", 1061, "playlist-modify-private", "Bearer")
            save_spotify_token(settings.token_cache_path, token)
            now = [1000.0]
            calls = []

            def transport(request):
                calls.append(request)
                return HttpResponse(200, {}, '{"access_token":"new-secret","refresh_token":"rotated-secret","expires_in":3600}')

            session = SpotifySession(settings, token, ("playlist-modify-private",), wall_time=lambda: now[0], token_transport=transport)
            self.assertEqual(session.get_access_token(), "old-secret")
            now[0] = 1002
            self.assertEqual(session.get_access_token(), "new-secret")
            saved = load_required_spotify_token(settings.token_cache_path)
            self.assertEqual(saved.refresh_token, "rotated-secret")
            self.assertEqual(saved.scope, "playlist-modify-private")
            self.assertEqual(session.get_access_token(), saved.access_token)
            self.assertEqual(len(calls), 1)
            self.assertEqual(session.refresh_count, 1)

    def test_terminal_refresh_failures_are_sanitized_sticky_and_never_authorize(self):
        from publishers.spotify.client import SpotifyAuthenticationError
        from publishers.spotify.session import SpotifySession

        for kind in ("missing", "scope", "invalid_grant", "save", "transport", "oauth_quota"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                settings = SpotifySettings("client", "http://localhost/callback", Path(directory) / "token.json")
                token = SpotifyToken("old-secret", "" if kind == "missing" else "refresh-secret", 1000, "playlist-modify-private", "Bearer")
                save_spotify_token(settings.token_cache_path, token)
                calls = []

                def transport(request):
                    calls.append(request)
                    if kind == "invalid_grant":
                        return HttpResponse(400, {}, '{"error":"invalid_grant","description":"response-secret"}')
                    if kind == "transport":
                        raise OSError("transport-secret")
                    if kind == "oauth_quota":
                        return HttpResponse(429, {"Retry-After": "1"}, "response-secret")
                    return HttpResponse(200, {}, '{"access_token":"new-secret","scope":"' + ("other" if kind == "scope" else "playlist-modify-private") + '"}')

                session = SpotifySession(settings, token, ("playlist-modify-private",), wall_time=lambda: 1000, token_transport=transport)
                with patch("publishers.spotify.session.authorize_spotify_interactively") as browser:
                    with patch("publishers.spotify.session.save_spotify_token", side_effect=OSError("save-secret")) if kind == "save" else nullcontext():
                        with self.assertRaises(SpotifyAuthenticationError) as caught:
                            session.get_access_token()
                    with self.assertRaises(SpotifyAuthenticationError):
                        session.get_access_token()
                    browser.assert_not_called()
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(len(calls), 0 if kind == "missing" else 1)
                if kind == "invalid_grant":
                    self.assertFalse(settings.token_cache_path.exists())
                else:
                    self.assertEqual(load_required_spotify_token(settings.token_cache_path), token)

    def test_invalid_grant_preserves_a_replaced_cache_record(self):
        from publishers.spotify.client import SpotifyAuthenticationError
        from publishers.spotify.session import SpotifySession

        with tempfile.TemporaryDirectory() as directory:
            settings = SpotifySettings("client", "http://localhost/callback", Path(directory) / "token.json")
            old = SpotifyToken("old", "old-refresh", 1000, "", "Bearer")
            replacement = SpotifyToken("other", "other-refresh", 5000, "", "Bearer")
            save_spotify_token(settings.token_cache_path, replacement)
            session = SpotifySession(settings, old, wall_time=lambda: 1000, token_transport=lambda request: HttpResponse(400, {}, '{"error":"invalid_grant"}'))
            with self.assertRaises(SpotifyAuthenticationError):
                session.get_access_token()
            self.assertEqual(load_required_spotify_token(settings.token_cache_path), replacement)


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
            saved_token = load_required_spotify_token(token_cache_path)
            self.assertEqual(saved_token.access_token, "refreshed-access-token")
            self.assertEqual(saved_token.refresh_token, "refresh-token")

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
            self.assertEqual(load_required_spotify_token(token_cache_path).scope, "playlist-modify-private")

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
            self.assertEqual(load_required_spotify_token(token_cache_path).access_token, "new-access-token")

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
            self.assertEqual(load_required_spotify_token(token_cache_path).access_token, "new-access-token")

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
            self.assertEqual(load_required_spotify_token(token_cache_path).access_token, "new-access-token")

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
            self.assertEqual(load_required_spotify_token(token_cache_path).access_token, "rescoped-access-token")


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
