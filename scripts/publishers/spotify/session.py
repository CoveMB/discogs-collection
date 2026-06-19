"""Reusable Spotify session management."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import replace

from publishers.spotify.auth import SpotifyTokenRequestError, refresh_access_token
from publishers.spotify.authorization_flow import authorize_spotify_interactively
from publishers.spotify.client import Transport, urllib_transport
from publishers.spotify.env import SpotifySettings
from publishers.spotify.token_cache import SpotifyToken, discard_spotify_token, load_spotify_token, save_spotify_token


AuthorizeInteractively = Callable[[SpotifySettings], SpotifyToken]


def get_spotify_access_token(
    settings: SpotifySettings,
    required_scopes: Sequence[str] = (),
    now: int | None = None,
    token_transport: Transport = urllib_transport,
    authorize_interactively: AuthorizeInteractively = authorize_spotify_interactively,
) -> str:
    current_time = int(now if now is not None else time.time())
    token = load_existing_token(settings)
    if token and token_has_required_scopes(token, required_scopes):
        if not token.is_expired(now=current_time):
            return token.access_token
        if token.refresh_token:
            try:
                refreshed_token = refresh_access_token(
                    settings=settings,
                    refresh_token=token.refresh_token,
                    transport=token_transport,
                    now=current_time,
                )
            except SpotifyTokenRequestError as error:
                if error.error_code == "invalid_grant":
                    discard_spotify_token(settings.token_cache_path)
                refreshed_token = None
            except ValueError:
                refreshed_token = None
            if refreshed_token:
                if not refreshed_token.scope and token.scope:
                    refreshed_token = replace(refreshed_token, scope=token.scope)
                if token_has_required_scopes(refreshed_token, required_scopes):
                    save_spotify_token(settings.token_cache_path, refreshed_token)
                    return refreshed_token.access_token

    authorized_token = authorize_interactively(settings)
    if not token_has_required_scopes(authorized_token, required_scopes):
        missing_scopes = sorted(set(required_scopes) - token_scopes(authorized_token))
        raise ValueError(f"Spotify authorization did not grant required scopes: {', '.join(missing_scopes)}")
    save_spotify_token(settings.token_cache_path, authorized_token)
    return authorized_token.access_token


def load_existing_token(settings: SpotifySettings) -> SpotifyToken | None:
    try:
        return load_spotify_token(settings.token_cache_path)
    except (json.JSONDecodeError, ValueError):
        return None


def token_has_required_scopes(token: SpotifyToken, required_scopes: Sequence[str]) -> bool:
    return set(required_scopes).issubset(token_scopes(token))


def token_scopes(token: SpotifyToken) -> set[str]:
    return {scope.strip() for scope in token.scope.split() if scope.strip()}
