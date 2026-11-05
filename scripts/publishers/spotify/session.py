"""Reusable Spotify session management."""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import replace

from publishers.spotify.auth import SpotifyTokenRequestError, refresh_access_token
from publishers.spotify.authorization_flow import authorize_spotify_interactively
from publishers.spotify.client import SpotifyAuthenticationError, Transport, urllib_transport
from publishers.spotify.env import SpotifySettings
from publishers.spotify.token_cache import SpotifyToken, discard_spotify_token, load_spotify_token, save_spotify_token


AuthorizeInteractively = Callable[[SpotifySettings], SpotifyToken]


class SpotifySession:
    """Renew an authorized session without interactive mid-run fallback."""

    def __init__(
        self,
        settings: SpotifySettings,
        token: SpotifyToken,
        required_scopes: Sequence[str] = (),
        wall_time: Callable[[], float] = time.time,
        token_transport: Transport = urllib_transport,
    ):
        self.settings = settings
        self.token = token
        self.required_scopes = tuple(required_scopes)
        self.wall_time = wall_time
        self.token_transport = token_transport
        self.refresh_count = 0
        self.authentication_error: SpotifyAuthenticationError | None = None

    def get_access_token(self, force_refresh: bool = False) -> str:
        if self.authentication_error is not None:
            raise self.authentication_error
        try:
            return self._get_access_token(force_refresh)
        except (ValueError, OSError, http.client.HTTPException) as error:
            if isinstance(error, SpotifyTokenRequestError) and error.error_code == "invalid_grant":
                try:
                    if load_existing_token(self.settings) == self.token:
                        discard_spotify_token(self.settings.token_cache_path)
                except (ValueError, OSError):
                    pass  # Keep the authentication failure primary if cleanup fails.
            self.authentication_error = SpotifyAuthenticationError(
                "Spotify session could not be renewed safely; reauthorize before restarting."
            )
            raise self.authentication_error from None

    def _get_access_token(self, force_refresh: bool) -> str:
        if not token_has_required_scopes(self.token, self.required_scopes):
            raise ValueError("Required scopes are missing")
        now = int(self.wall_time())
        if not force_refresh and self.token.access_token and not self.token.is_expired(now):
            return self.token.access_token
        if not self.token.refresh_token:
            raise ValueError("Refresh token is missing")
        self.refresh_count += 1
        refreshed_token = refresh_access_token(
            settings=self.settings,
            refresh_token=self.token.refresh_token,
            transport=self.token_transport,
            now=now,
        )
        if not refreshed_token.scope:
            refreshed_token = replace(refreshed_token, scope=self.token.scope)
        if not token_has_required_scopes(refreshed_token, self.required_scopes):
            raise ValueError("Required scopes were lost")
        if refreshed_token.is_expired(int(self.wall_time())):
            raise ValueError("Refreshed token is already expired")
        save_spotify_token(self.settings.token_cache_path, refreshed_token)
        self.token = refreshed_token
        return refreshed_token.access_token


def get_spotify_access_token(
    settings: SpotifySettings,
    required_scopes: Sequence[str] = (),
    now: int | None = None,
    token_transport: Transport = urllib_transport,
    authorize_interactively: AuthorizeInteractively = authorize_spotify_interactively,
    on_refresh: Callable[[], None] | None = None,
) -> str:
    current_time = int(now if now is not None else time.time())
    token = load_existing_token(settings)
    if token and token_has_required_scopes(token, required_scopes):
        if not token.is_expired(now=current_time):
            return token.access_token
        if token.refresh_token:
            if on_refresh is not None:
                on_refresh()
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
