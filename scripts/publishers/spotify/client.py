"""Small Spotify Web API client."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from publishers.spotify.matching import SpotifyTrackCandidate
from publishers.spotify.release_matching import SpotifyAlbumCandidate, SpotifyAlbumTrack
from shared.debug_log import DebugLog
from shared.text import clean_cell


SPOTIFY_API_ROOT = "https://api.spotify.com/v1"
DEFAULT_SPOTIFY_RATE_LIMIT_RETRIES = 3
DEFAULT_SPOTIFY_RATE_LIMIT_FALLBACK_WAIT_SECONDS = 60.0
DEFAULT_SPOTIFY_RATE_LIMIT_MAX_WAIT_SECONDS = 480.0


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: str


@dataclass(frozen=True)
class SpotifyPlaylist:
    playlist_id: str
    name: str
    url: str
    owner_id: str = ""
    public: bool | None = None
    collaborative: bool = False


@dataclass(frozen=True)
class SpotifyPlaylistItem:
    uri: str
    name: str
    artists: tuple[str, ...]
    album_name: str
    added_at: str = ""
    position: int = 0


@dataclass(frozen=True)
class SpotifyRetryPolicy:
    max_rate_limit_retries: int = DEFAULT_SPOTIFY_RATE_LIMIT_RETRIES
    fallback_retry_after_seconds: float = DEFAULT_SPOTIFY_RATE_LIMIT_FALLBACK_WAIT_SECONDS
    max_rate_limit_wait_seconds: float = DEFAULT_SPOTIFY_RATE_LIMIT_MAX_WAIT_SECONDS

    def rate_limit_wait_seconds(self, headers: Mapping[str, str]) -> float:
        retry_after = parse_retry_after(header_value(headers, "Retry-After"))
        if retry_after <= 0:
            return max(0.0, self.fallback_retry_after_seconds)
        return retry_after

    def exceeds_max_wait(self, wait_seconds: float) -> bool:
        return wait_seconds > max(0.0, self.max_rate_limit_wait_seconds)


Transport = Callable[[HttpRequest], HttpResponse]


class SpotifyApiError(RuntimeError):
    pass


class SpotifyAlbumLookupError(SpotifyApiError):
    pass


class SpotifyRateLimitDeferredError(SpotifyApiError):
    def __init__(self, retry_after_seconds: float, max_wait_seconds: float):
        self.retry_after_seconds = retry_after_seconds
        self.max_wait_seconds = max_wait_seconds
        super().__init__(
            "Spotify rate limit Retry-After is "
            f"{format_wait_duration(retry_after_seconds)}, exceeding max wait "
            f"{format_wait_duration(max_wait_seconds)}. Retry later. "
            "After the cooldown expires, run only scripts/publishers/spotify/publish_playlist.py."
        )


class SpotifyRateLimitRetriesExhaustedError(SpotifyApiError):
    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            "Spotify rate limit retries were exhausted. "
            f"Retry later; last Retry-After was {format_wait_duration(retry_after_seconds)}."
        )


class SpotifyTrackSearchClient(Protocol):
    def search_tracks(
        self,
        access_token: str,
        query: str,
        limit: int = 10,
    ) -> tuple[SpotifyTrackCandidate, ...]: ...


class SpotifyPlaylistReadClient(Protocol):
    def list_current_user_playlists(self, access_token: str) -> tuple[SpotifyPlaylist, ...]: ...

    def get_current_user_id(self, access_token: str) -> str: ...

    def get_playlist_items(self, access_token: str, playlist_id: str) -> tuple[SpotifyPlaylistItem, ...]: ...


class SpotifyPlaylistPlanningClient(SpotifyTrackSearchClient, SpotifyPlaylistReadClient, Protocol):
    pass


class SpotifyPlaylistPublishClient(SpotifyPlaylistPlanningClient, Protocol):
    def create_playlist(
        self,
        access_token: str,
        name: str,
        public: bool = False,
        description: str = "",
    ) -> SpotifyPlaylist: ...

    def add_playlist_items(
        self,
        access_token: str,
        playlist_id: str,
        uris: Sequence[str],
        position: int | None = None,
    ) -> None: ...

    def replace_playlist_items(
        self,
        access_token: str,
        playlist_id: str,
        uris: Sequence[str],
    ) -> None: ...


class SpotifyClient:
    def __init__(
        self,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_policy: SpotifyRetryPolicy | None = None,
        debug_log: DebugLog | None = None,
    ):
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.retry_policy = retry_policy or SpotifyRetryPolicy()
        self.debug_log = debug_log

    def search_tracks(
        self,
        access_token: str,
        query: str,
        limit: int = 10,
    ) -> tuple[SpotifyTrackCandidate, ...]:
        if limit < 1 or limit > 10:
            raise ValueError("Spotify search limit must be between 1 and 10")
        params = urllib.parse.urlencode(
            {
                "q": query,
                "type": "track",
                "limit": str(limit),
            }
        )
        request = HttpRequest(
            method="GET",
            url=f"{SPOTIFY_API_ROOT}/search?{params}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        response = self.request_with_rate_limit_retries(request, operation_name="spotify_search")
        if response.status != 200:
            raise SpotifyApiError(f"Spotify search failed with status {response.status}: {response.body}")
        return parse_search_track_candidates(response.body)

    def search_albums(
        self,
        access_token: str,
        query: str,
        limit: int = 10,
    ) -> tuple[SpotifyAlbumCandidate, ...]:
        if limit < 1 or limit > 10:
            raise ValueError("Spotify search limit must be between 1 and 10")
        params = urllib.parse.urlencode(
            {
                "q": query,
                "type": "album",
                "limit": str(limit),
            }
        )
        request = HttpRequest(
            method="GET",
            url=f"{SPOTIFY_API_ROOT}/search?{params}",
            headers=spotify_json_headers(access_token),
        )
        response = self.request_with_rate_limit_retries(
            request,
            operation_name="spotify_album_search",
        )
        if response.status != 200:
            raise SpotifyAlbumLookupError(
                f"Spotify album search failed with status {response.status}: {response.body}"
            )
        return parse_search_album_candidates(response.body)

    def get_album_tracks(
        self,
        access_token: str,
        album_id: str,
    ) -> tuple[SpotifyAlbumTrack, ...]:
        album_tracks: list[SpotifyAlbumTrack] = []
        limit = 50
        offset = 0
        while True:
            params = urllib.parse.urlencode({"limit": str(limit), "offset": str(offset)})
            request = HttpRequest(
                method="GET",
                url=(
                    f"{SPOTIFY_API_ROOT}/albums/"
                    f"{urllib.parse.quote(album_id, safe='')}/tracks?{params}"
                ),
                headers=spotify_json_headers(access_token),
            )
            response = self.request_with_rate_limit_retries(
                request,
                operation_name="spotify_album_tracks",
            )
            if response.status != 200:
                raise SpotifyAlbumLookupError(
                    f"Spotify album tracks failed with status {response.status}: {response.body}"
                )
            payload = json.loads(response.body or "{}")
            album_tracks.extend(parse_album_track_page(payload))
            if not isinstance(payload, dict) or not payload.get("next"):
                return tuple(album_tracks)
            offset += limit

    def list_current_user_playlists(self, access_token: str) -> tuple[SpotifyPlaylist, ...]:
        playlists: list[SpotifyPlaylist] = []
        limit = 50
        offset = 0
        while True:
            params = urllib.parse.urlencode({"limit": str(limit), "offset": str(offset)})
            request = HttpRequest(
                method="GET",
                url=f"{SPOTIFY_API_ROOT}/me/playlists?{params}",
                headers=spotify_json_headers(access_token),
            )
            response = self.request_with_rate_limit_retries(request, operation_name="spotify_playlist_list")
            if response.status != 200:
                raise SpotifyApiError(f"Spotify playlist list failed with status {response.status}: {response.body}")
            payload = json.loads(response.body or "{}")
            playlists.extend(parse_playlist_page(payload))
            if not payload.get("next"):
                return tuple(playlists)
            offset += limit

    def get_current_user_id(self, access_token: str) -> str:
        request = HttpRequest(
            method="GET",
            url=f"{SPOTIFY_API_ROOT}/me",
            headers=spotify_json_headers(access_token),
        )
        response = self.request_with_rate_limit_retries(request, operation_name="spotify_current_user")
        if response.status != 200:
            raise SpotifyApiError(f"Spotify current user lookup failed with status {response.status}: {response.body}")
        user_id = parse_current_user_id(response.body)
        if not user_id:
            raise SpotifyApiError("Spotify current user response did not include user id")
        return user_id

    def get_playlist_items(self, access_token: str, playlist_id: str) -> tuple[SpotifyPlaylistItem, ...]:
        playlist_items: list[SpotifyPlaylistItem] = []
        limit = 50
        offset = 0
        fields = (
            "items(added_at,item(uri,name,type,artists(name),album(name)),"
            "track(uri,name,type,artists(name),album(name))),next,total,limit,offset"
        )
        while True:
            params = urllib.parse.urlencode(
                {
                    "limit": str(limit),
                    "offset": str(offset),
                    "fields": fields,
                }
            )
            request = HttpRequest(
                method="GET",
                url=f"{SPOTIFY_API_ROOT}/playlists/{urllib.parse.quote(playlist_id)}/items?{params}",
                headers=spotify_json_headers(access_token),
            )
            response = self.request_with_rate_limit_retries(request, operation_name="spotify_playlist_items")
            if response.status != 200:
                raise SpotifyApiError(f"Spotify playlist items failed with status {response.status}: {response.body}")
            payload = json.loads(response.body or "{}")
            page_items = parse_playlist_item_page(payload, starting_position=offset)
            if playlist_item_page_has_unparsed_tracks(payload, page_items):
                raise SpotifyApiError("Spotify playlist items response could not parse any playlist tracks from a non-empty page")
            playlist_items.extend(page_items)
            if not payload.get("next"):
                return tuple(playlist_items)
            offset += limit

    def create_playlist(
        self,
        access_token: str,
        name: str,
        public: bool = False,
        description: str = "",
    ) -> SpotifyPlaylist:
        body = {
            "name": name,
            "public": public,
        }
        if description:
            body["description"] = description
        request = HttpRequest(
            method="POST",
            url=f"{SPOTIFY_API_ROOT}/me/playlists",
            headers=spotify_json_headers(access_token),
            body=json.dumps(body).encode("utf-8"),
        )
        response = self.request_with_rate_limit_retries(request, operation_name="spotify_playlist_create")
        if response.status != 201:
            raise SpotifyApiError(f"Spotify playlist create failed with status {response.status}: {response.body}")
        playlist = parse_playlist_object(json.loads(response.body or "{}"))
        if not playlist:
            raise SpotifyApiError("Spotify playlist create response did not include playlist id and name")
        return playlist

    def add_playlist_items(
        self,
        access_token: str,
        playlist_id: str,
        uris: Sequence[str],
        position: int | None = None,
    ) -> None:
        for batch in chunk_values(tuple(uris), 100):
            body: dict[str, object] = {"uris": list(batch)}
            if position is not None:
                body["position"] = position
            request = HttpRequest(
                method="POST",
                url=f"{SPOTIFY_API_ROOT}/playlists/{urllib.parse.quote(playlist_id)}/items",
                headers=spotify_json_headers(access_token),
                body=json.dumps(body).encode("utf-8"),
            )
            response = self.request_with_rate_limit_retries(request, operation_name="spotify_playlist_add_items")
            if response.status != 201:
                raise SpotifyApiError(f"Spotify playlist add items failed with status {response.status}: {response.body}")
            json.loads(response.body or "{}")
            if position is not None:
                position += len(batch)

    def replace_playlist_items(
        self,
        access_token: str,
        playlist_id: str,
        uris: Sequence[str],
    ) -> None:
        if len(uris) > 100:
            raise ValueError("Spotify playlist replace accepts at most 100 URIs per request")
        request = HttpRequest(
            method="PUT",
            url=f"{SPOTIFY_API_ROOT}/playlists/{urllib.parse.quote(playlist_id)}/items",
            headers=spotify_json_headers(access_token),
            body=json.dumps({"uris": list(uris)}).encode("utf-8"),
        )
        response = self.request_with_rate_limit_retries(request, operation_name="spotify_playlist_replace_items")
        if response.status != 200:
            raise SpotifyApiError(f"Spotify playlist replace items failed with status {response.status}: {response.body}")
        json.loads(response.body or "{}")

    def request_with_rate_limit_retries(self, request: HttpRequest, operation_name: str = "spotify_search") -> HttpResponse:
        max_retries = max(0, self.retry_policy.max_rate_limit_retries)
        for attempt_number in range(1, max_retries + 2):
            self.log_debug(f"{operation_name}_request attempt={attempt_number}")
            response = self.transport(request)
            self.log_debug(format_debug_response(attempt_number, response, operation_name=operation_name))
            if response.status != 429:
                return response
            if attempt_number > max_retries:
                wait_seconds = self.retry_policy.rate_limit_wait_seconds(response.headers)
                raise SpotifyRateLimitRetriesExhaustedError(wait_seconds)
            wait_seconds = self.retry_policy.rate_limit_wait_seconds(response.headers)
            if self.retry_policy.exceeds_max_wait(wait_seconds):
                self.log_debug(
                    "spotify_rate_limit_deferred "
                    f"attempt={attempt_number} max_wait_seconds={self.retry_policy.max_rate_limit_wait_seconds} "
                    f"retry_after_seconds={wait_seconds}"
                )
                raise SpotifyRateLimitDeferredError(wait_seconds, self.retry_policy.max_rate_limit_wait_seconds)
            self.log_debug(
                "spotify_rate_limit_retry "
                f"attempt={attempt_number} max_retries={max_retries} wait_seconds={wait_seconds}"
            )
            self.sleep(wait_seconds)
        raise RuntimeError("Spotify request retry loop ended unexpectedly")

    def log_debug(self, message: str) -> None:
        if self.debug_log:
            self.debug_log(message)


def parse_search_track_candidates(body: str) -> tuple[SpotifyTrackCandidate, ...]:
    payload = json.loads(body or "{}")
    tracks = payload.get("tracks", {}) if isinstance(payload, dict) else {}
    items = tracks.get("items", []) if isinstance(tracks, dict) else []
    candidates: list[SpotifyTrackCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = clean_cell(item.get("uri"))
        name = clean_cell(item.get("name"))
        album = item.get("album", {})
        artists = item.get("artists", [])
        album_name = clean_cell(album.get("name")) if isinstance(album, dict) else ""
        album_id = clean_cell(album.get("id")) if isinstance(album, dict) else ""
        artist_names = tuple(
            clean_cell(artist.get("name"))
            for artist in artists
            if isinstance(artist, dict) and clean_cell(artist.get("name"))
        )
        if uri and name:
            candidates.append(
                SpotifyTrackCandidate(
                    uri=uri,
                    name=name,
                    artists=artist_names,
                    album_name=album_name,
                    album_id=album_id,
                )
            )
    return tuple(candidates)


def parse_search_album_candidates(body: str) -> tuple[SpotifyAlbumCandidate, ...]:
    payload = json.loads(body or "{}")
    albums = payload.get("albums", {}) if isinstance(payload, dict) else {}
    items = albums.get("items", []) if isinstance(albums, dict) else []
    candidates: list[SpotifyAlbumCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        album_id = clean_cell(item.get("id"))
        name = clean_cell(item.get("name"))
        artists = item.get("artists", [])
        artist_names = tuple(
            clean_cell(artist.get("name"))
            for artist in artists
            if isinstance(artist, dict) and clean_cell(artist.get("name"))
        )
        if album_id and name:
            candidates.append(
                SpotifyAlbumCandidate(
                    album_id=album_id,
                    uri=clean_cell(item.get("uri")),
                    name=name,
                    artists=artist_names,
                    total_tracks=non_negative_int(item.get("total_tracks")),
                )
            )
    return tuple(candidates)


def parse_album_track_page(payload: object) -> tuple[SpotifyAlbumTrack, ...]:
    items = payload.get("items", []) if isinstance(payload, dict) else []
    tracks: list[SpotifyAlbumTrack] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = clean_cell(item.get("uri"))
        name = clean_cell(item.get("name"))
        artists = item.get("artists", [])
        artist_names = tuple(
            clean_cell(artist.get("name"))
            for artist in artists
            if isinstance(artist, dict) and clean_cell(artist.get("name"))
        )
        if not uri or not name:
            continue
        is_playable_value = item.get("is_playable")
        tracks.append(
            SpotifyAlbumTrack(
                uri=uri,
                name=name,
                artists=artist_names,
                disc_number=non_negative_int(item.get("disc_number")),
                track_number=non_negative_int(item.get("track_number")),
                is_playable=(
                    is_playable_value
                    if isinstance(is_playable_value, bool)
                    else None
                ),
            )
        )
    return tuple(tracks)


def non_negative_int(value: object) -> int:
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def spotify_json_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def parse_playlist_page(payload: object) -> tuple[SpotifyPlaylist, ...]:
    items = payload.get("items", []) if isinstance(payload, dict) else []
    playlists: list[SpotifyPlaylist] = []
    for item in items:
        playlist = parse_playlist_object(item)
        if playlist:
            playlists.append(playlist)
    return tuple(playlists)


def parse_playlist_object(payload: object) -> SpotifyPlaylist | None:
    if not isinstance(payload, dict):
        return None
    playlist_id = clean_cell(payload.get("id"))
    name = clean_cell(payload.get("name"))
    owner = payload.get("owner", {})
    external_urls = payload.get("external_urls", {})
    public_value = payload.get("public")
    url = clean_cell(external_urls.get("spotify")) if isinstance(external_urls, dict) else ""
    if not playlist_id or not name:
        return None
    return SpotifyPlaylist(
        playlist_id=playlist_id,
        name=name,
        url=url,
        owner_id=clean_cell(owner.get("id")) if isinstance(owner, dict) else "",
        public=public_value if isinstance(public_value, bool) else None,
        collaborative=bool(payload.get("collaborative")) if isinstance(payload.get("collaborative"), bool) else False,
    )


def parse_playlist_item_page(payload: object, starting_position: int = 0) -> tuple[SpotifyPlaylistItem, ...]:
    items = payload.get("items", []) if isinstance(payload, dict) else []
    playlist_items: list[SpotifyPlaylistItem] = []
    for item_offset, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        playlist_item = parse_playlist_item_track(
            playlist_item_media_payload(item),
            added_at=clean_cell(item.get("added_at")),
            position=starting_position + item_offset,
        )
        if playlist_item:
            playlist_items.append(playlist_item)
    return tuple(playlist_items)


def playlist_item_page_has_unparsed_tracks(payload: object, parsed_items: Sequence[SpotifyPlaylistItem]) -> bool:
    if parsed_items or not isinstance(payload, dict):
        return False
    items = payload.get("items", [])
    if not isinstance(items, list) or not items:
        return False
    return any(playlist_item_payload_looks_like_track(item) for item in items)


def playlist_item_payload_looks_like_track(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    media = playlist_item_media_payload(item)
    if not isinstance(media, dict):
        return False
    media_type = clean_cell(media.get("type")).casefold()
    if media_type and media_type != "track":
        return False
    return any(field in media for field in ("uri", "name", "artists", "album"))


def playlist_item_media_payload(item: Mapping[str, object]) -> object:
    media = item.get("item")
    if isinstance(media, dict):
        return media
    return item.get("track")


def parse_playlist_item_track(track: object, added_at: str = "", position: int = 0) -> SpotifyPlaylistItem | None:
    if not isinstance(track, dict):
        return None
    media_type = clean_cell(track.get("type")).casefold()
    if media_type and media_type != "track":
        return None
    uri = clean_cell(track.get("uri"))
    name = clean_cell(track.get("name"))
    album = track.get("album", {})
    artists = track.get("artists", [])
    album_name = clean_cell(album.get("name")) if isinstance(album, dict) else ""
    artist_names = tuple(
        clean_cell(artist.get("name"))
        for artist in artists
        if isinstance(artist, dict) and clean_cell(artist.get("name"))
    )
    if not uri:
        return None
    return SpotifyPlaylistItem(
        uri=uri,
        name=name,
        artists=artist_names,
        album_name=album_name,
        added_at=added_at,
        position=position,
    )


def parse_current_user_id(body: str) -> str:
    payload = json.loads(body or "{}")
    return clean_cell(payload.get("id")) if isinstance(payload, dict) else ""


def chunk_values(values: Sequence[str], batch_size: int) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(values[index : index + batch_size]) for index in range(0, len(values), batch_size))


def urllib_transport(request: HttpRequest) -> HttpResponse:
    urllib_request = urllib.request.Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        with urllib.request.urlopen(urllib_request, timeout=30) as response:
            return HttpResponse(
                status=response.status,
                headers=dict(response.headers.items()),
                body=response.read().decode("utf-8"),
            )
    except urllib.error.HTTPError as error:
        return HttpResponse(
            status=error.code,
            headers=dict(error.headers.items()),
            body=error.read().decode("utf-8", errors="replace"),
        )
    except urllib.error.URLError as error:
        return HttpResponse(
            status=0,
            headers={},
            body=str(error),
        )


def parse_retry_after(value: object) -> float:
    try:
        retry_after = float(str(value or "").strip())
    except ValueError:
        return 0.0
    return max(0.0, retry_after)


def format_wait_seconds(seconds: float) -> str:
    seconds = float(seconds)
    if seconds.is_integer():
        return str(int(seconds))
    return str(seconds)


def format_wait_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    seconds -= hours * 3600
    minutes = int(seconds // 60)
    seconds -= minutes * 60

    parts: list[str] = []
    if hours:
        parts.append(format_wait_duration_part(float(hours), "hour"))
    if minutes:
        parts.append(format_wait_duration_part(float(minutes), "minute"))
    if seconds or not parts:
        parts.append(format_wait_duration_part(seconds, "second"))
    return " ".join(parts)


def format_wait_duration_part(value: float, singular_unit: str) -> str:
    unit = singular_unit if value == 1 else f"{singular_unit}s"
    return f"{format_wait_seconds(value)} {unit}"


def header_value(headers: Mapping[str, str], name: str) -> str:
    for header_name, value in headers.items():
        if header_name.casefold() == name.casefold():
            return value
    return ""


def display_header_value(headers: Mapping[str, str], name: str) -> str:
    return header_value(headers, name) or "(none)"


def format_debug_response(attempt_number: int, response: HttpResponse, operation_name: str = "spotify_search") -> str:
    message = (
        f"{operation_name}_response "
        f"attempt={attempt_number} status={response.status} "
        f"retry_after={display_header_value(response.headers, 'Retry-After')}"
    )
    if response.status == 0:
        message = f"{message} transport_error={classify_transport_error(response.body)}"
    return message


def classify_transport_error(body: str) -> str:
    text = body.casefold()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if (
        "name or service not known" in text
        or "nodename nor servname" in text
        or "temporary failure in name resolution" in text
        or "no address associated" in text
    ):
        return "dns_or_name_resolution"
    if "certificate" in text or "ssl" in text:
        return "tls_or_certificate"
    if (
        "network is unreachable" in text
        or "no route to host" in text
        or "connection refused" in text
        or "connection reset" in text
    ):
        return "connection_failure"
    return "transport_error"
