"""Spotify PKCE authorization helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from collections.abc import Sequence

from publishers.spotify.client import HttpRequest, HttpResponse, Transport, urllib_transport
from publishers.spotify.env import SpotifySettings
from publishers.spotify.token_cache import SpotifyToken
from shared.text import clean_cell


SPOTIFY_ACCOUNTS_ROOT = "https://accounts.spotify.com"
DEFAULT_AUTHORIZE_SCOPES: tuple[str, ...] = ()


class SpotifyTokenRequestError(ValueError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        self.error_code = token_error_code(body)
        super().__init__(f"Spotify token request failed with status {status}: {body}")


def build_authorization_url(
    settings: SpotifySettings,
    state: str,
    code_challenge: str,
    scopes: Sequence[str] = DEFAULT_AUTHORIZE_SCOPES,
) -> str:
    params = {
        "client_id": settings.client_id,
        "response_type": "code",
        "redirect_uri": settings.redirect_uri,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    return f"{SPOTIFY_ACCOUNTS_ROOT}/authorize?{urllib.parse.urlencode(params)}"


def make_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def make_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def exchange_authorization_code(
    settings: SpotifySettings,
    code: str,
    code_verifier: str,
    transport: Transport = urllib_transport,
    now: int | None = None,
) -> SpotifyToken:
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.redirect_uri,
            "client_id": settings.client_id,
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")
    response = transport(
        HttpRequest(
            method="POST",
            url=f"{SPOTIFY_ACCOUNTS_ROOT}/api/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body=body,
        )
    )
    return token_from_response(response, existing_refresh_token="", now=now)


def refresh_access_token(
    settings: SpotifySettings,
    refresh_token: str,
    transport: Transport = urllib_transport,
    now: int | None = None,
) -> SpotifyToken:
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.client_id,
        }
    ).encode("utf-8")
    response = transport(
        HttpRequest(
            method="POST",
            url=f"{SPOTIFY_ACCOUNTS_ROOT}/api/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body=body,
        )
    )
    return token_from_response(response, existing_refresh_token=refresh_token, now=now)


def token_from_response(response: HttpResponse, existing_refresh_token: str, now: int | None = None) -> SpotifyToken:
    if response.status != 200:
        raise SpotifyTokenRequestError(response.status, response.body)
    payload = json.loads(response.body or "{}")
    if not isinstance(payload, dict):
        raise ValueError("Spotify token response must be a JSON object")
    access_token = clean_cell(payload.get("access_token"))
    if not access_token:
        raise ValueError("Spotify token response did not include access_token")
    expires_in = payload.get("expires_in", 3600)
    if isinstance(expires_in, bool) or not isinstance(expires_in, int):
        raise ValueError("Spotify token response expires_in must be an integer")
    refresh_token = clean_cell(payload.get("refresh_token")) or existing_refresh_token
    return SpotifyToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=int(now if now is not None else time.time()) + expires_in,
        scope=clean_cell(payload.get("scope")),
        token_type=clean_cell(payload.get("token_type")) or "Bearer",
    )


def token_error_code(body: str) -> str:
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return clean_cell(payload.get("error"))
