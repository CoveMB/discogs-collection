"""Application service for Spotify authorization."""

from __future__ import annotations

import http.server
import ipaddress
import sys
import urllib.parse
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from publishers.spotify.auth import (
    build_authorization_url,
    exchange_authorization_code,
    make_code_challenge,
    make_code_verifier,
)
from publishers.spotify.client import Transport, urllib_transport
from publishers.spotify.env import SpotifySettings
from publishers.spotify.token_cache import SpotifyToken, save_spotify_token


DEFAULT_AUTHORIZE_SCOPES = ("playlist-read-private", "playlist-modify-private", "user-read-private")
AuthorizationCodeProvider = Callable[[str, str], str]


@dataclass(frozen=True)
class LoopbackRedirect:
    host: str
    port: int
    path: str


def authorize_spotify(
    settings: SpotifySettings,
    code_provider: AuthorizationCodeProvider,
    token_transport: Transport = urllib_transport,
    scopes: Sequence[str] = DEFAULT_AUTHORIZE_SCOPES,
    now: int | None = None,
) -> SpotifyToken:
    state = make_code_verifier()
    code_verifier = make_code_verifier()
    authorization_url = build_authorization_url(
        settings=settings,
        state=state,
        code_challenge=make_code_challenge(code_verifier),
        scopes=scopes,
    )
    code = code_provider(authorization_url, state)
    token = exchange_authorization_code(
        settings=settings,
        code=code,
        code_verifier=code_verifier,
        transport=token_transport,
        now=now,
    )
    save_spotify_token(settings.token_cache_path, token)
    return token


def authorize_spotify_interactively(settings: SpotifySettings) -> SpotifyToken:
    return authorize_spotify(
        settings=settings,
        code_provider=LoopbackAuthorizationCodeProvider(settings.redirect_uri),
    )


class LoopbackAuthorizationCodeProvider:
    def __init__(self, redirect_uri: str):
        self.redirect_uri = redirect_uri

    def __call__(self, authorization_url: str, expected_state: str) -> str:
        callback = wait_for_loopback_callback(
            redirect_uri=self.redirect_uri,
            authorization_url=authorization_url,
            expected_state=expected_state,
        )
        return callback.code


class CallbackRequestHandler(http.server.BaseHTTPRequestHandler):
    code = ""
    error = ""
    expected_state = ""
    callback_path = ""

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name.
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path != self.callback_path:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Unknown Spotify callback path.")
            return

        query = urllib.parse.parse_qs(parsed_path.query)
        state = first_query_value(query, "state")
        handler_class = type(self)
        if state != self.expected_state:
            handler_class.error = "Spotify callback state did not match."
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Spotify callback state did not match. You can close this tab.")
            return

        handler_class.code = first_query_value(query, "code")
        handler_class.error = first_query_value(query, "error")
        if handler_class.error:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Spotify authorization failed. You can close this tab.")
            return
        if not handler_class.code:
            handler_class.error = "Spotify callback did not include a code."
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Spotify callback did not include a code. You can close this tab.")
            return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Spotify authorization received. You can close this tab.")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature.
        return


def wait_for_loopback_callback(
    redirect_uri: str,
    authorization_url: str,
    expected_state: str,
) -> type[CallbackRequestHandler]:
    callback = parse_loopback_redirect_uri(redirect_uri)

    handler_class = type(
        "SpotifyCallbackRequestHandler",
        (CallbackRequestHandler,),
        {
            "code": "",
            "error": "",
            "expected_state": expected_state,
            "callback_path": callback.path,
        },
    )
    server = http.server.HTTPServer((callback.host, callback.port), handler_class)
    print("Open this Spotify authorization URL if your browser does not open automatically:", file=sys.stderr)
    print(authorization_url, file=sys.stderr)
    webbrowser.open(authorization_url)
    server.handle_request()
    server.server_close()
    if handler_class.error:
        raise ValueError(handler_class.error)
    if not handler_class.code:
        raise ValueError("Spotify authorization did not return a code")
    return handler_class


def parse_loopback_redirect_uri(redirect_uri: str) -> LoopbackRedirect:
    parsed_redirect_uri = urllib.parse.urlparse(redirect_uri)
    if parsed_redirect_uri.scheme != "http":
        raise ValueError("Spotify redirect URI must use http for the local loopback callback")
    if not parsed_redirect_uri.hostname or parsed_redirect_uri.port is None:
        raise ValueError("Spotify redirect URI must include a loopback host and port")
    try:
        host_address = ipaddress.ip_address(parsed_redirect_uri.hostname)
    except ValueError as error:
        raise ValueError("Spotify redirect URI host must be a loopback IP literal") from error
    if not host_address.is_loopback:
        raise ValueError("Spotify redirect URI host must be a loopback IP literal")
    return LoopbackRedirect(
        host=parsed_redirect_uri.hostname,
        port=parsed_redirect_uri.port,
        path=parsed_redirect_uri.path or "/callback",
    )


def first_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key, [])
    return values[0] if values else ""
