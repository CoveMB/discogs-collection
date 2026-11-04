import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.client import (
    SpotifyAlbumLookupError,
    SpotifyApiError,
    SpotifyPlaylist,
    SpotifyPlaylistItem,
    SpotifyRateLimitDeferredError,
    SpotifyRateLimitRetriesExhaustedError,
)
from publishers.spotify.env import SpotifySettings
from publishers.spotify.matching import (
    MATCHED,
    SPOTIFY_ORIGINAL_ANNOTATION_MATCH_REASON,
    SPOTIFY_ORIGINAL_ANNOTATION_MATCH_STRATEGY,
    PlaylistTrack,
    SpotifyTrackCandidate,
    TrackMatchDecision,
)
from publishers.spotify.match_cache import (
    ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD,
    ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD,
    MATCHER_VERSION,
    spotify_track_match_key,
)
from publishers.spotify.release_matching import SpotifyAlbumCandidate, SpotifyAlbumTrack
from publishers.spotify.authorization_flow import DEFAULT_AUTHORIZE_SCOPES
from publishers.spotify.publish_playlist import (
    publish_reason_with_match_details,
    publish_spotify_playlists,
)
from publishers.spotify.publish_state import record_spotify_publish_state_track, save_spotify_publish_state
from shared.progress import ProgressReporter
from shared.publisher_config import PublisherConfig
from shared.tunemymusic import TUNEMYMUSIC_COLUMNS
from publishers.spotify.token_cache import SpotifyToken


class TerminalStream(io.StringIO):
    def isatty(self):
        return True


class FakeSpotifyClient:
    def __init__(self, candidates_by_query):
        self.candidates_by_query = candidates_by_query
        self.searches = []

    def search_tracks(self, access_token, query, limit=10):
        self.searches.append((access_token, query, limit))
        return self.candidates_by_query.get(query, ())


class QueryLengthThenMatchSpotifyClient:
    def __init__(self, matching_query, candidate):
        self.matching_query = matching_query
        self.candidate = candidate
        self.searches = []

    def search_tracks(self, access_token, query, limit=10):
        self.searches.append((access_token, query, limit))
        if query != self.matching_query:
            raise SpotifyApiError('Spotify search failed with status 400: {"error": {"status": 400, "message": "Query exceeds maximum length of 250 characters" } }')
        return (self.candidate,)

    def list_current_user_playlists(self, access_token):
        return ()

    def get_current_user_id(self, access_token):
        return "current-user"

    def get_playlist_items(self, access_token, playlist_id):
        return ()


class QueryLengthThenEmptySpotifyClient(QueryLengthThenMatchSpotifyClient):
    def __init__(self, matching_query):
        super().__init__(matching_query=matching_query, candidate=None)

    def search_tracks(self, access_token, query, limit=10):
        self.searches.append((access_token, query, limit))
        if query != self.matching_query:
            raise SpotifyApiError('Spotify search failed with status 400: {"error": {"status": 400, "message": "Query exceeds maximum length of 250 characters" } }')
        return ()


class PublishingSpotifyClient(FakeSpotifyClient):
    def __init__(self, candidates_by_query, playlists=(), playlist_items_by_id=None, current_user_id="current-user"):
        super().__init__(candidates_by_query)
        self.current_user_id = current_user_id
        self.playlists = list(playlists)
        self.playlist_items_by_id = {
            playlist_id: list(items)
            for playlist_id, items in (playlist_items_by_id or {}).items()
        }
        self.candidates_by_uri = {
            candidate.uri: candidate
            for candidates in candidates_by_query.values()
            for candidate in candidates
        }
        self.created_playlists = []
        self.add_calls = []
        self.replace_calls = []

    def list_current_user_playlists(self, access_token):
        return tuple(self.playlists)

    def get_current_user_id(self, access_token):
        return self.current_user_id

    def get_playlist_items(self, access_token, playlist_id):
        return tuple(self.playlist_items_by_id.get(playlist_id, ()))

    def create_playlist(self, access_token, name, public=False, description=""):
        playlist_id = f"created-{len(self.created_playlists) + 1}"
        playlist = SpotifyPlaylist(
            playlist_id=playlist_id,
            name=name,
            url=f"https://open.spotify.com/playlist/{playlist_id}",
            owner_id=self.current_user_id,
            public=public,
            collaborative=False,
        )
        self.created_playlists.append((name, public, description))
        self.playlists.append(playlist)
        self.playlist_items_by_id[playlist_id] = []
        return playlist

    def add_playlist_items(self, access_token, playlist_id, uris, position=None):
        self.add_calls.append((playlist_id, tuple(uris), position))
        new_items = [
            self.playlist_item_for_uri(uri)
            for uri in uris
        ]
        if position is None:
            self.playlist_items_by_id.setdefault(playlist_id, []).extend(new_items)
        else:
            playlist_items = self.playlist_items_by_id.setdefault(playlist_id, [])
            insert_position = max(0, min(position, len(playlist_items)))
            playlist_items[insert_position:insert_position] = new_items

    def replace_playlist_items(self, access_token, playlist_id, uris):
        self.replace_calls.append((playlist_id, tuple(uris)))
        self.playlist_items_by_id[playlist_id] = [
            self.playlist_item_for_uri(uri)
            for uri in uris
        ]

    def playlist_item_for_uri(self, uri):
        candidate = self.candidates_by_uri.get(uri)
        if candidate:
            return SpotifyPlaylistItem(
                uri=uri,
                name=candidate.name,
                artists=candidate.artists,
                album_name=candidate.album_name,
            )
        return SpotifyPlaylistItem(uri=uri, name=uri.rsplit(":", 1)[-1], artists=(), album_name="")


class StrictPositionPublishingSpotifyClient(PublishingSpotifyClient):
    def add_playlist_items(self, access_token, playlist_id, uris, position=None):
        current_item_count = len(self.playlist_items_by_id.get(playlist_id, ()))
        if position is not None and position > current_item_count:
            raise SpotifyApiError(
                'Spotify playlist add items failed with status 400: '
                '{"error": {"status": 400, "message": "Index out of bounds" } }'
            )
        super().add_playlist_items(access_token, playlist_id, uris, position=position)


class AlbumPublishingSpotifyClient(PublishingSpotifyClient):
    def __init__(self, album_candidates, album_tracks_by_id, candidates_by_query=None):
        super().__init__(candidates_by_query or {})
        self.album_candidates = album_candidates
        self.album_tracks_by_id = album_tracks_by_id
        self.album_searches = []
        self.album_track_requests = []

    def search_albums(self, access_token, query, limit=10):
        self.album_searches.append((access_token, query, limit))
        return self.album_candidates

    def get_album_tracks(self, access_token, album_id):
        self.album_track_requests.append((access_token, album_id))
        return self.album_tracks_by_id[album_id]


class DeferredAlbumPublishingSpotifyClient(AlbumPublishingSpotifyClient):
    def search_albums(self, access_token, query, limit=10):
        self.album_searches.append((access_token, query, limit))
        raise SpotifyRateLimitDeferredError(retry_after_seconds=9999, max_wait_seconds=480)


class FailingAlbumPublishingSpotifyClient(AlbumPublishingSpotifyClient):
    def search_albums(self, access_token, query, limit=10):
        self.album_searches.append((access_token, query, limit))
        raise SpotifyAlbumLookupError(
            "Spotify album search failed with status 500: temporary failure"
        )


class DeferredSecondSearchPublishingClient(PublishingSpotifyClient):
    def search_tracks(self, access_token, query, limit=10):
        self.searches.append((access_token, query, limit))
        if "Beta One" in query:
            raise SpotifyRateLimitDeferredError(retry_after_seconds=9999, max_wait_seconds=480)
        return self.candidates_by_query.get(query, ())


class ExhaustedRateLimitAfterCheckpointPublishingClient(PublishingSpotifyClient):
    def search_tracks(self, access_token, query, limit=10):
        self.searches.append((access_token, query, limit))
        if "Track 101" in query:
            raise SpotifyRateLimitRetriesExhaustedError(retry_after_seconds=1)
        return self.candidates_by_query.get(query, ())


class InterruptedAfterCheckpointPublishingClient(PublishingSpotifyClient):
    def search_tracks(self, access_token, query, limit=10):
        self.searches.append((access_token, query, limit))
        if "Track 101" in query:
            raise RuntimeError("interrupted after checkpoint")
        return self.candidates_by_query.get(query, ())


class FailingSecondPlaylistPublishClient(PublishingSpotifyClient):
    def add_playlist_items(self, access_token, playlist_id, uris, position=None):
        if playlist_id == "playlist-techno":
            raise SpotifyApiError("Spotify playlist add items failed with status 500: temporary failure")
        super().add_playlist_items(access_token, playlist_id, uris, position=position)


class SearchErrorPublishingClient(PublishingSpotifyClient):
    def search_tracks(self, access_token, query, limit=10):
        if "Beta One" in query:
            raise SpotifyApiError("Spotify search failed with status 500: temporary failure")
        return super().search_tracks(access_token, query, limit)


class FailingReplaceRemainderPublishClient(PublishingSpotifyClient):
    def add_playlist_items(self, access_token, playlist_id, uris, position=None):
        if self.replace_calls:
            raise SpotifyApiError("Spotify playlist add items failed with status 500: temporary failure")
        super().add_playlist_items(access_token, playlist_id, uris, position=position)


class FailingFirstAppendBatchClient(PublishingSpotifyClient):
    def __init__(self, candidates_by_query, fail_playlist_id):
        super().__init__(
            candidates_by_query,
            playlists=(
                SpotifyPlaylist(
                    playlist_id=fail_playlist_id,
                    name="Discogs - House",
                    url="",
                    owner_id="current-user",
                    public=False,
                ),
            ),
        )
        self.fail_playlist_id = fail_playlist_id
        self.fail_next_append = True
        self.candidates_by_uri = {
            candidate.uri: candidate
            for candidates in candidates_by_query.values()
            for candidate in candidates
        }

    def add_playlist_items(self, access_token, playlist_id, uris, position=None):
        for batch_start in range(0, len(uris), 100):
            batch = tuple(uris[batch_start : batch_start + 100])
            self.add_calls.append((playlist_id, batch, position))
            new_items = [
                self.playlist_item_for_uri(uri)
                for uri in batch
            ]
            if position is None:
                self.playlist_items_by_id.setdefault(playlist_id, []).extend(new_items)
            else:
                playlist_items = self.playlist_items_by_id.setdefault(playlist_id, [])
                insert_position = max(0, min(position, len(playlist_items)))
                playlist_items[insert_position:insert_position] = new_items
            if playlist_id == self.fail_playlist_id and self.fail_next_append:
                self.fail_next_append = False
                raise SpotifyApiError("Spotify playlist add items failed with status 429: Too many requests")
            if position is not None:
                position += len(batch)

    def playlist_item_for_uri(self, uri):
        candidate = self.candidates_by_uri[uri]
        return SpotifyPlaylistItem(
            uri=uri,
            name=candidate.name,
            artists=candidate.artists,
            album_name=candidate.album_name,
        )


def write_playlist_master(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output_file:
        fieldnames: list[str] = list(TUNEMYMUSIC_COLUMNS)
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def playlist_row(
    release_id: str,
    album_name: str,
    track_name: str,
    artist_name: str,
    track_number: str = "1",
) -> dict[str, str]:
    return {
        "Release Id": release_id,
        "Album Name": album_name,
        "Track Number": track_number,
        "Track Name": track_name,
        "Artist Name": artist_name,
        "Spotify Search Query": f"{artist_name} {track_name} {album_name}",
    }


def matching_candidate(row: dict[str, str]) -> SpotifyTrackCandidate:
    return SpotifyTrackCandidate(
        uri=f"spotify:track:{row['Release Id']}",
        name=row["Track Name"],
        artists=(row["Artist Name"],),
        album_name=row["Album Name"],
    )


def playlist_track(
    playlist_name: str,
    release_id: str,
    album_name: str,
    track_name: str,
    artist_name: str,
    track_number: str = "1",
) -> PlaylistTrack:
    return PlaylistTrack(
        playlist_name=playlist_name,
        release_id=release_id,
        album_name=album_name,
        track_number=track_number,
        track_name=track_name,
        artist_name=artist_name,
        spotify_search_query=f"{artist_name} {track_name} {album_name}",
    )


def cached_negative_match_record(
    row: dict[str, str],
    candidate_album_ids: tuple[str, ...],
    *,
    attempted_album_ids: tuple[str, ...] = (),
    attempt_matcher_version: int | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "release_id": row["Release Id"],
        "track_number": row["Track Number"],
        "artist_name": row["Artist Name"],
        "album_name": row["Album Name"],
        "track_name": row["Track Name"],
        "search_query": row["Spotify Search Query"],
        "match_status": "unmatched",
        "match_reason": "no candidates matched track, artist, and album",
        "matcher_version": MATCHER_VERSION,
        "review_candidates": [
            {
                "spotify_uri": f"spotify:track:{album_id}-{row['Track Number']}",
                "spotify_track_name": row["Track Name"],
                "spotify_artist_names": [row["Artist Name"]],
                "spotify_album_name": row["Album Name"],
                "spotify_album_id": album_id,
            }
            for album_id in candidate_album_ids
        ],
        "searched_at": "2026-07-17T00:00:00Z",
        "last_seen_at": "2026-07-17T00:00:00Z",
    }
    if attempted_album_ids:
        record[ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD] = list(attempted_album_ids)
        record[ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD] = (
            MATCHER_VERSION
            if attempt_matcher_version is None
            else attempt_matcher_version
        )
    return record


def write_cached_negative_release(
    directory: Path,
    candidate_album_ids: tuple[str, ...],
    *,
    attempted_album_ids: tuple[str, ...] = (),
    attempt_matcher_version: int | None = None,
) -> tuple[Path, Path, list[dict[str, str]]]:
    playlist_directory = directory / "collection" / "playlists"
    source_rows = [
        playlist_row("release-1", "Recovered Album", f"Anchor {index}", "Example Artist", str(index))
        for index in range(1, 4)
    ]
    write_playlist_master(
        playlist_directory / "Techno" / "Techno.csv",
        source_rows,
    )
    match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
    records = {
        spotify_track_match_key(
            playlist_track(
                "Techno",
                row["Release Id"],
                row["Album Name"],
                row["Track Name"],
                row["Artist Name"],
                row["Track Number"],
            )
        ): cached_negative_match_record(
            row,
            candidate_album_ids,
            attempted_album_ids=attempted_album_ids,
            attempt_matcher_version=attempt_matcher_version,
        )
        for row in source_rows
    }
    match_cache_path.parent.mkdir(parents=True)
    match_cache_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_type": "spotify_track_match_cache",
                "matches": records,
            }
        ),
        encoding="utf-8",
    )
    return playlist_directory, match_cache_path, source_rows


def write_automatic_matched_hint_release(
    directory: Path,
    *,
    matcher_version: int = MATCHER_VERSION,
) -> tuple[Path, Path, list[dict[str, str]], str]:
    playlist_directory = directory / "collection" / "playlists"
    source_rows = [
        playlist_row("release-1", "Recovered Album", f"Anchor {index}", "Example Artist", str(index))
        for index in range(1, 4)
    ]
    write_playlist_master(
        playlist_directory / "Techno" / "Techno.csv",
        source_rows,
    )
    first_row = source_rows[0]
    first_cache_key = spotify_track_match_key(
        playlist_track(
            "Techno",
            first_row["Release Id"],
            first_row["Album Name"],
            first_row["Track Name"],
            first_row["Artist Name"],
            first_row["Track Number"],
        )
    )
    match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
    match_cache_path.parent.mkdir(parents=True)
    match_cache_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_type": "spotify_track_match_cache",
                "matches": {
                    first_cache_key: {
                        "release_id": first_row["Release Id"],
                        "track_number": first_row["Track Number"],
                        "artist_name": first_row["Artist Name"],
                        "album_name": first_row["Album Name"],
                        "track_name": first_row["Track Name"],
                        "search_query": first_row["Spotify Search Query"],
                        "search_queries": [first_row["Spotify Search Query"]],
                        "match_status": "matched",
                        "match_reason": "track, artist, and album matched",
                        "matcher_version": matcher_version,
                        "spotify_uri": "spotify:track:cached-anchor-one",
                        "spotify_url": "https://open.spotify.com/track/cached-anchor-one",
                        "spotify_track_name": first_row["Track Name"],
                        "spotify_artist_names": [first_row["Artist Name"]],
                        "spotify_album_name": first_row["Album Name"],
                        "spotify_album_id": "recovered-album",
                        "matched_at": "2026-07-17T00:00:00Z",
                        "last_seen_at": "2026-07-17T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return playlist_directory, match_cache_path, source_rows, first_cache_key


def seed_known_publish_state(path: Path, playlist_name: str, row: dict[str, str], source_position: int) -> None:
    from publishers.spotify import publish_playlist

    state = {}
    track = playlist_track(
        "House",
        row["Release Id"],
        row["Album Name"],
        row["Track Name"],
        row["Artist Name"],
    )
    identity_key = publish_playlist.spotify_track_identity_key(
        target_playlist_name=playlist_name,
        artist_names=(row["Artist Name"],),
        album_name=row["Album Name"],
        track_name=row["Track Name"],
    )
    record_spotify_publish_state_track(
        state=state,
        playlist_name=playlist_name,
        identity_key=identity_key,
        spotify_uri=f"spotify:track:{row['Release Id']}",
        source_position=source_position,
        track=track,
        timestamp="2026-07-09T00:00:00Z",
        event="published",
    )
    save_spotify_publish_state(path, state)


def publish_summary_stub(**overrides):
    values = {
        "report_path": Path("reports/spotify_playlist_publish_report.txt"),
        "track_count": 0,
        "cache_hit_count": 0,
        "search_count": 0,
        "request_count": None,
        "searched_row_count": 0,
        "matched_count": 0,
        "already_present_count": 0,
        "would_add_count": 0,
        "added_count": 0,
        "would_include_count": 0,
        "included_count": 0,
        "duplicate_in_source_count": 0,
        "ambiguous_count": 0,
        "unmatched_count": 0,
        "error_count": 0,
    }
    values.update(overrides)
    return type("Summary", (), values)()


class SpotifyPublishPlaylistTests(unittest.TestCase):
    def setUp(self):
        config_patcher = patch(
            "publishers.spotify.publish_playlist.load_or_create_publisher_config",
            return_value=PublisherConfig("spotify", "", ""),
        )
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

    def test_publish_reason_keeps_spotify_original_annotation_match_evidence(self):
        track = PlaylistTrack(
            playlist_name="Techno",
            release_id="release-1",
            album_name="Source Album",
            track_number="1",
            track_name="Source Track",
            artist_name="Source Artist",
            spotify_search_query="",
        )
        decision = TrackMatchDecision(
            track=track,
            status=MATCHED,
            spotify_uri="spotify:track:manual-one",
            reason=SPOTIFY_ORIGINAL_ANNOTATION_MATCH_REASON,
            candidate=None,
            match_strategy=SPOTIFY_ORIGINAL_ANNOTATION_MATCH_STRATEGY,
        )

        reason = publish_reason_with_match_details("track will be appended", decision)

        self.assertEqual(
            reason,
            f"track will be appended; {SPOTIFY_ORIGINAL_ANNOTATION_MATCH_REASON}",
        )

    def test_append_apply_orders_new_append_before_later_restored_track_position(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist"),
                playlist_row("222", "Beta Album", "Beta One", "Beta Artist"),
                playlist_row("333", "Gamma Album", "Gamma One", "Gamma Artist"),
            ]
            write_playlist_master(playlist_directory / "Deep Techno" / "Deep Techno.csv", rows)
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            publish_state_cache_path = directory / "collection" / "cache" / "spotify-publish-state.cache.json"
            seed_known_publish_state(
                publish_state_cache_path,
                playlist_name="Discogs - Deep Techno",
                row=rows[2],
                source_position=3,
            )
            client = StrictPositionPublishingSpotifyClient(
                {
                    f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                        matching_candidate(row),
                    )
                    for row in rows
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-deep-techno",
                        name="Discogs - Deep Techno",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
                playlist_items_by_id={
                    "playlist-deep-techno": (
                        SpotifyPlaylistItem(
                            uri="spotify:track:111",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    )
                },
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publish_state_cache_path=publish_state_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="append",
                max_new_searches_per_run=0,
            )

        self.assertEqual(summary.already_present_count, 1)
        self.assertEqual(summary.added_count, 2)
        self.assertIn("restored at its source-order position", summary.decisions[2].reason)
        self.assertEqual(
            client.add_calls,
            [
                (
                    "playlist-deep-techno",
                    ("spotify:track:222", "spotify:track:333"),
                    None,
                ),
            ],
        )
        self.assertEqual(
            [item.name for item in client.playlist_items_by_id["playlist-deep-techno"]],
            ["Alpha One", "Beta One", "Gamma One"],
        )

    def test_append_plan_places_known_missing_track_before_next_source_anchor(self):
        from publishers.spotify import publish_playlist

        existing_items = (
            SpotifyPlaylistItem(
                uri="spotify:track:111",
                name="Alpha One",
                artists=("Alpha Artist",),
                album_name="Alpha Album",
            ),
            SpotifyPlaylistItem(
                uri="spotify:track:manual",
                name="Manual One",
                artists=("Manual Artist",),
                album_name="Manual Album",
            ),
            SpotifyPlaylistItem(
                uri="spotify:track:333",
                name="Gamma One",
                artists=("Gamma Artist",),
                album_name="Gamma Album",
            ),
        )
        beta_track = playlist_track("House", "222", "Beta Album", "Beta One", "Beta Artist")
        beta_candidate = SpotifyTrackCandidate(
            uri="spotify:track:222",
            name="Beta One",
            artists=("Beta Artist",),
            album_name="Beta Album",
        )
        decision = publish_playlist.PlaylistPublishDecision(
            playlist_name="House",
            target_playlist_name="Discogs - House",
            track=beta_track,
            status="would_add",
            spotify_uri="spotify:track:222",
            reason="",
            match_source="search",
            candidate=beta_candidate,
        )
        beta_identity = publish_playlist.publish_decision_identity_key(decision)
        candidate = publish_playlist.PendingAppendTrack(
            decision_index=0,
            identity_key=beta_identity,
            spotify_uri="spotify:track:222",
            source_position=2,
            known_to_publisher=True,
            final_item=publish_playlist.final_playlist_item_from_decision(2, decision),
        )

        plan = publish_playlist.build_append_playlist_plan(
            target_playlist_name="Discogs - House",
            existing_items=existing_items,
            append_candidates=(candidate,),
            source_positions_by_identity={
                publish_playlist.spotify_playlist_item_identity_key("Discogs - House", existing_items[0]): 1,
                beta_identity: 2,
                publish_playlist.spotify_playlist_item_identity_key("Discogs - House", existing_items[2]): 3,
            },
        )

        self.assertEqual([item.track_name for item in plan.final_items], ["Alpha One", "Manual One", "Beta One", "Gamma One"])
        self.assertEqual(plan.write_operations[0].position, 2)

    def test_append_apply_restores_known_deleted_track_at_source_position(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist"),
                playlist_row("222", "Beta Album", "Beta One", "Beta Artist"),
                playlist_row("333", "Gamma Album", "Gamma One", "Gamma Artist"),
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            publish_state_cache_path = directory / "collection" / "cache" / "spotify-publish-state.cache.json"
            client = PublishingSpotifyClient(
                {
                    f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                        matching_candidate(row),
                    )
                    for row in rows
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
                playlist_items_by_id={
                    "playlist-house": (
                        SpotifyPlaylistItem(uri="spotify:track:111", name="Alpha One", artists=("Alpha Artist",), album_name="Alpha Album"),
                        SpotifyPlaylistItem(uri="spotify:track:manual", name="Manual One", artists=("Manual Artist",), album_name="Manual Album"),
                        SpotifyPlaylistItem(uri="spotify:track:333", name="Gamma One", artists=("Gamma Artist",), album_name="Gamma Album"),
                    )
                },
            )
            seed_known_publish_state(
                publish_state_cache_path,
                playlist_name="Discogs - House",
                row=rows[1],
                source_position=2,
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publish_state_cache_path=publish_state_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="append",
                max_new_searches_per_run=0,
            )
            report_text = report_path.read_text(encoding="utf-8")
            publish_state_payload = json.loads(publish_state_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(summary.already_present_count, 2)
        self.assertEqual(summary.added_count, 1)
        self.assertEqual(client.add_calls, [("playlist-house", ("spotify:track:222",), 2)])
        self.assertEqual(
            [item.name for item in client.playlist_items_by_id["playlist-house"]],
            ["Alpha One", "Manual One", "Beta One", "Gamma One"],
        )
        self.assertIn("3 | added | Beta Artist | Beta One | Beta Album | spotify:track:222", report_text)
        self.assertIn("will be restored at its source-order position", report_text)
        self.assertIn("discogs house|beta artist|beta album|beta one", publish_state_payload["playlists"]["Discogs - House"]["tracks"])

    def test_append_apply_appends_newly_seen_missing_track(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist"),
                playlist_row("222", "Beta Album", "Beta One", "Beta Artist"),
                playlist_row("333", "Gamma Album", "Gamma One", "Gamma Artist"),
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            publish_state_cache_path = directory / "collection" / "cache" / "spotify-publish-state.cache.json"
            client = PublishingSpotifyClient(
                {
                    f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                        matching_candidate(row),
                    )
                    for row in rows
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
                playlist_items_by_id={
                    "playlist-house": (
                        SpotifyPlaylistItem(uri="spotify:track:111", name="Alpha One", artists=("Alpha Artist",), album_name="Alpha Album"),
                        SpotifyPlaylistItem(uri="spotify:track:333", name="Gamma One", artists=("Gamma Artist",), album_name="Gamma Album"),
                    )
                },
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publish_state_cache_path=publish_state_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="append",
                max_new_searches_per_run=0,
            )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(summary.already_present_count, 2)
        self.assertEqual(summary.added_count, 1)
        self.assertEqual(client.add_calls, [("playlist-house", ("spotify:track:222",), None)])
        self.assertEqual(
            [item.name for item in client.playlist_items_by_id["playlist-house"]],
            ["Alpha One", "Gamma One", "Beta One"],
        )
        self.assertIn("3 | added | Beta Artist | Beta One | Beta Album | spotify:track:222", report_text)
        self.assertIn("newly seen and will be appended", report_text)

    def test_publish_dry_run_reports_row_progress_while_searching_spotify_tracks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Discogs - Breakbeat" / "Discogs - Breakbeat.csv",
                [
                    playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist"),
                    playlist_row("222", "Beta Album", "Beta One", "Beta Artist"),
                ],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            client = PublishingSpotifyClient({})
            progress_stream = TerminalStream()
            progress = ProgressReporter(stream=progress_stream, label="Searching Spotify tracks")

            publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                progress=progress,
                match_cache_path=directory / "collection" / "cache" / "spotify-track-matches.cache.json",
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

            progress_text = progress_stream.getvalue()
            self.assertIn("\rSearching Spotify tracks [", progress_text)
            self.assertIn("1/2", progress_text)
            self.assertIn("50%", progress_text)
            self.assertIn("2/2", progress_text)
            self.assertIn("100%", progress_text)
            self.assertTrue(progress_text.endswith("\n"))

    def test_publish_defaults_to_validated_album_matching_before_track_search(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            source_rows = [
                playlist_row("23288552", "Numbers and Colours", "1", "Example Artist, Collaborator", "1"),
                playlist_row("23288552", "Numbers and Colours", "2", "Example Artist, Collaborator", "2"),
                playlist_row("23288552", "Numbers and Colours", "3", "Example Artist, Collaborator", "3"),
                playlist_row("23288552", "Numbers and Colours", "Silent Anchor", "Example Artist, Collaborator", "4"),
                playlist_row("23288552", "Numbers and Colours", "Hidden Anchor", "Example Artist, Collaborator", "5"),
                playlist_row("23288552", "Numbers and Colours", "Memory Of An Unconncet Era", "Example Artist", "6"),
                playlist_row("23288552", "Numbers and Colours", "Eon", "Example Artist, Second Collaborator", "7"),
                playlist_row("23288552", "Numbers and Colours", "Reson", "Example Artist", "8"),
                playlist_row("23288552", "Numbers and Colours", "Ergo", "Example Artist", "9"),
            ]
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                source_rows,
            )
            spotify_names_and_artists = (
                ("White", ("Example Artist", "Collaborator")),
                ("Grey", ("Example Artist", "Collaborator")),
                ("Black", ("Example Artist", "Collaborator")),
                ("Silent Anchor", ("Example Artist", "Collaborator")),
                ("Hidden Anchor", ("Example Artist", "Collaborator")),
                ("Memory Of An Unconnected Era", ("Example Artist",)),
                ("Eon", ("Example Artist", "Second Collaborator")),
                ("Resin", ("Example Artist",)),
                ("Ergo", ("Example Artist",)),
            )
            spotify_album_tracks = tuple(
                SpotifyAlbumTrack(
                    uri=f"spotify:track:album-track-{track_number}",
                    name=track_name,
                    artists=artists,
                    disc_number=1,
                    track_number=track_number,
                    is_playable=True,
                )
                for track_number, (track_name, artists) in enumerate(spotify_names_and_artists, start=1)
            )
            client = AlbumPublishingSpotifyClient(
                album_candidates=(
                    SpotifyAlbumCandidate(
                        album_id="numbers-and-colours",
                        uri="spotify:album:numbers-and-colours",
                        name="Numbers And Colours",
                        artists=("Example Artist",),
                        total_tracks=9,
                    ),
                ),
                album_tracks_by_id={"numbers-and-colours": spotify_album_tracks},
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(summary.search_count, 1)
        self.assertEqual(summary.searched_row_count, 0)
        self.assertEqual(summary.would_add_count, 9)
        self.assertEqual(client.album_searches, [("access-token", 'album:"Numbers and Colours"', 10)])
        self.assertEqual(client.album_track_requests, [("access-token", "numbers-and-colours")])
        self.assertEqual(client.searches, [])
        self.assertEqual(
            [decision.spotify_uri for decision in summary.decisions],
            [f"spotify:track:album-track-{track_number}" for track_number in range(1, 10)],
        )
        first_cache_record = cache_payload["matches"][
            "23288552|1|example artist collaborator|numbers and colours|1"
        ]
        self.assertEqual(first_cache_record["match_strategy"], "validated_album_position")
        self.assertEqual(first_cache_record["spotify_album_id"], "numbers-and-colours")
        self.assertIn("Album matched tracks: 9", report_text)

    def test_publish_reuses_cached_track_candidate_album_id_after_matcher_update(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            source_rows = [
                playlist_row("release-1", "Recovered Album", f"Anchor {index}", "Example Artist", str(index))
                for index in range(1, 4)
            ]
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                source_rows,
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            cached_candidate = {
                "spotify_uri": "spotify:track:cached-candidate",
                "spotify_track_name": "Anchor 1",
                "spotify_artist_names": ["Example Artist"],
                "spotify_album_name": "Recovered Album",
                "spotify_album_id": "recovered-album",
            }
            cached_matches = {}
            for row in source_rows:
                track_number = row["Track Number"]
                track_name = row["Track Name"]
                cached_matches[
                    f"release-1|{track_number}|example artist|recovered album|{track_name.casefold()}"
                ] = {
                    "release_id": "release-1",
                    "track_number": track_number,
                    "artist_name": "Example Artist",
                    "album_name": "Recovered Album",
                    "track_name": track_name,
                    "search_query": f'Example Artist {track_name} Recovered Album',
                    "match_status": "unmatched",
                    "match_reason": "no candidates matched track, artist, and album",
                    "matcher_version": MATCHER_VERSION,
                    "review_candidates": [cached_candidate],
                    "searched_at": "2026-07-17T00:00:00Z",
                    "last_seen_at": "2026-07-17T00:00:00Z",
                }
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": cached_matches,
                    }
                ),
                encoding="utf-8",
            )
            spotify_album_tracks = tuple(
                SpotifyAlbumTrack(
                    uri=f"spotify:track:recovered-{index}",
                    name=f"Anchor {index}",
                    artists=("Example Artist",),
                    disc_number=1,
                    track_number=index,
                    is_playable=True,
                )
                for index in range(1, 4)
            )
            client = AlbumPublishingSpotifyClient(
                album_candidates=(
                    SpotifyAlbumCandidate(
                        album_id="different-album",
                        uri="spotify:album:different-album",
                        name="Different Album",
                        artists=("Example Artist",),
                        total_tracks=3,
                    ),
                ),
                album_tracks_by_id={"recovered-album": spotify_album_tracks},
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(summary.search_count, 1)
        self.assertEqual(summary.searched_row_count, 0)
        self.assertEqual(summary.would_add_count, 3)
        self.assertEqual(client.searches, [])
        self.assertEqual(
            client.album_track_requests,
            [("access-token", "recovered-album")],
        )
        self.assertTrue(
            all(
                "album recovered from cached track-search candidates" in decision.reason
                for decision in summary.decisions
            )
        )

    def test_publish_reuses_album_id_from_automatic_matched_cache_record(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            source_rows = [
                playlist_row("release-1", "Recovered Album", f"Anchor {index}", "Example Artist", str(index))
                for index in range(1, 4)
            ]
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                source_rows,
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            cached_matches = {
                "release-1|1|example artist|recovered album|anchor 1": {
                    "release_id": "release-1",
                    "track_number": "1",
                    "artist_name": "Example Artist",
                    "album_name": "Recovered Album",
                    "track_name": "Anchor 1",
                    "match_status": "matched",
                    "match_reason": "track, artist, and album matched",
                    "matcher_version": MATCHER_VERSION,
                    "spotify_uri": "spotify:track:cached-anchor-one",
                    "spotify_track_name": "Anchor 1",
                    "spotify_artist_names": ["Example Artist"],
                    "spotify_album_name": "Recovered Album",
                    "spotify_album_id": "recovered-album",
                    "matched_at": "2026-07-17T00:00:00Z",
                    "last_seen_at": "2026-07-17T00:00:00Z",
                }
            }
            for row in source_rows[1:]:
                track_number = row["Track Number"]
                track_name = row["Track Name"]
                cached_matches[
                    f"release-1|{track_number}|example artist|recovered album|{track_name.casefold()}"
                ] = {
                    "release_id": "release-1",
                    "track_number": track_number,
                    "artist_name": "Example Artist",
                    "album_name": "Recovered Album",
                    "track_name": track_name,
                    "match_status": "unmatched",
                    "match_reason": "no candidates matched track, artist, and album",
                    "matcher_version": MATCHER_VERSION,
                    "review_candidates": [],
                    "searched_at": "2026-07-17T00:00:00Z",
                    "last_seen_at": "2026-07-17T00:00:00Z",
                }
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": cached_matches,
                    }
                ),
                encoding="utf-8",
            )
            spotify_album_tracks = tuple(
                SpotifyAlbumTrack(
                    uri=f"spotify:track:recovered-{index}",
                    name=f"Anchor {index}",
                    artists=("Example Artist",),
                    disc_number=1,
                    track_number=index,
                    is_playable=True,
                )
                for index in range(1, 4)
            )
            client = AlbumPublishingSpotifyClient(
                album_candidates=(
                    SpotifyAlbumCandidate(
                        album_id="different-album",
                        uri="spotify:album:different-album",
                        name="Different Album",
                        artists=("Example Artist",),
                        total_tracks=3,
                    ),
                ),
                album_tracks_by_id={"recovered-album": spotify_album_tracks},
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(summary.search_count, 1)
        self.assertEqual(client.album_track_requests, [("access-token", "recovered-album")])
        self.assertEqual(client.searches, [])
        self.assertEqual(
            [decision.spotify_uri for decision in summary.decisions],
            [
                "spotify:track:cached-anchor-one",
                "spotify:track:recovered-2",
                "spotify:track:recovered-3",
            ],
        )
        self.assertEqual(
            cache_payload["matches"][
                "release-1|1|example artist|recovered album|anchor 1"
            ]["spotify_uri"],
            "spotify:track:cached-anchor-one",
        )
        self.assertNotIn(
            ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD,
            cache_payload["matches"][
                "release-1|1|example artist|recovered album|anchor 1"
            ],
        )

    def test_failed_automatic_matched_album_recovery_records_attempt_and_next_run_advances(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (
                playlist_directory,
                match_cache_path,
                _source_rows,
                matched_cache_key,
            ) = write_automatic_matched_hint_release(
                directory,
                matcher_version=MATCHER_VERSION - 1,
            )
            invalid_album_tracks = tuple(
                SpotifyAlbumTrack(
                    uri=f"spotify:track:different-{index}",
                    name=f"Different {index}",
                    artists=("Other Artist",),
                    disc_number=1,
                    track_number=index,
                    is_playable=True,
                )
                for index in range(1, 4)
            )
            album_candidates = (
                SpotifyAlbumCandidate(
                    album_id="different-album",
                    uri="spotify:album:different-album",
                    name="Different Album",
                    artists=("Example Artist",),
                    total_tracks=3,
                ),
            )
            first_client = AlbumPublishingSpotifyClient(
                album_candidates=album_candidates,
                album_tracks_by_id={"recovered-album": invalid_album_tracks},
            )
            first_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=first_client,
                access_token="access-token",
                search_limit=7,
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
                max_new_searches_per_run=1,
            )
            first_cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            second_client = AlbumPublishingSpotifyClient(
                album_candidates=album_candidates,
                album_tracks_by_id={"recovered-album": invalid_album_tracks},
            )
            second_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=second_client,
                access_token="access-token",
                search_limit=7,
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
                max_new_searches_per_run=1,
            )

        matched_record = first_cache_payload["matches"][matched_cache_key]
        self.assertEqual(first_summary.search_count, 1)
        self.assertEqual(
            first_client.album_searches,
            [("access-token", 'album:"Recovered Album"', 7)],
        )
        self.assertEqual(first_client.album_track_requests, [("access-token", "recovered-album")])
        self.assertEqual(
            matched_record.get(ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD),
            MATCHER_VERSION,
        )
        self.assertEqual(
            matched_record.get(ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD),
            ["recovered-album"],
        )
        self.assertEqual(matched_record["spotify_uri"], "spotify:track:cached-anchor-one")
        self.assertEqual(second_client.album_searches, [])
        self.assertEqual(second_client.album_track_requests, [])
        self.assertGreater(second_summary.search_count, 0)
        self.assertTrue(second_client.searches)
        self.assertTrue(all(search[2] == 7 for search in second_client.searches))

    def test_automatic_matched_album_recovery_retries_after_transient_failure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (
                playlist_directory,
                match_cache_path,
                _source_rows,
                matched_cache_key,
            ) = write_automatic_matched_hint_release(directory)
            deferred_client = DeferredAlbumPublishingSpotifyClient((), {})

            with self.assertRaises(SpotifyRateLimitDeferredError):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=directory / "reports" / "spotify-report.txt",
                    spotify_client=deferred_client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    apply=False,
                    publisher_sync_mode="append",
                    max_new_searches_per_run=1,
                )
            deferred_cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            invalid_album_tracks = tuple(
                SpotifyAlbumTrack(
                    uri=f"spotify:track:different-{index}",
                    name=f"Different {index}",
                    artists=("Other Artist",),
                    disc_number=1,
                    track_number=index,
                    is_playable=True,
                )
                for index in range(1, 4)
            )
            retry_client = AlbumPublishingSpotifyClient(
                album_candidates=(
                    SpotifyAlbumCandidate(
                        album_id="different-album",
                        uri="spotify:album:different-album",
                        name="Different Album",
                        artists=("Example Artist",),
                        total_tracks=3,
                    ),
                ),
                album_tracks_by_id={"recovered-album": invalid_album_tracks},
            )
            retry_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=retry_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
                max_new_searches_per_run=1,
            )

        deferred_record = deferred_cache_payload["matches"][matched_cache_key]
        self.assertNotIn(ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD, deferred_record)
        self.assertNotIn(ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD, deferred_record)
        self.assertEqual(retry_summary.search_count, 1)
        self.assertEqual(
            retry_client.album_track_requests,
            [("access-token", "recovered-album")],
        )

    def test_publish_does_not_use_manual_match_as_cached_album_hint(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            source_rows = [
                playlist_row("release-1", "Recovered Album", f"Anchor {index}", "Example Artist", str(index))
                for index in range(1, 4)
            ]
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                source_rows,
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            cached_matches = {
                "release-1|1|example artist|recovered album|anchor 1": {
                    "release_id": "release-1",
                    "track_number": "1",
                    "artist_name": "Example Artist",
                    "album_name": "Recovered Album",
                    "track_name": "Anchor 1",
                    "match_status": "manual",
                    "match_reason": "manually selected Spotify match",
                    "matcher_version": MATCHER_VERSION,
                    "spotify_uri": "spotify:track:manual-anchor-one",
                    "spotify_track_name": "Anchor 1",
                    "spotify_artist_names": ["Example Artist"],
                    "spotify_album_name": "Recovered Album",
                    "spotify_album_id": "manual-album",
                    "matched_at": "2026-07-17T00:00:00Z",
                    "last_seen_at": "2026-07-17T00:00:00Z",
                }
            }
            for row in source_rows[1:]:
                track_number = row["Track Number"]
                track_name = row["Track Name"]
                cached_matches[
                    f"release-1|{track_number}|example artist|recovered album|{track_name.casefold()}"
                ] = {
                    "release_id": "release-1",
                    "track_number": track_number,
                    "artist_name": "Example Artist",
                    "album_name": "Recovered Album",
                    "track_name": track_name,
                    "match_status": "unmatched",
                    "match_reason": "no candidates matched track, artist, and album",
                    "matcher_version": MATCHER_VERSION,
                    "review_candidates": [],
                    "searched_at": "2026-07-17T00:00:00Z",
                    "last_seen_at": "2026-07-17T00:00:00Z",
                }
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": cached_matches,
                    }
                ),
                encoding="utf-8",
            )
            client = AlbumPublishingSpotifyClient(
                album_candidates=(),
                album_tracks_by_id={},
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(summary.search_count, 0)
        self.assertEqual(client.album_searches, [])
        self.assertEqual(client.album_track_requests, [])
        self.assertEqual(client.searches, [])

    def test_failed_cached_album_recovery_is_not_retried_on_unchanged_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory, match_cache_path, _source_rows = write_cached_negative_release(
                directory,
                ("recovered-album",),
            )
            invalid_album_tracks = tuple(
                SpotifyAlbumTrack(
                    uri=f"spotify:track:different-{index}",
                    name=f"Different {index}",
                    artists=("Other Artist",),
                    disc_number=1,
                    track_number=index,
                    is_playable=True,
                )
                for index in range(1, 4)
            )
            first_client = AlbumPublishingSpotifyClient(
                album_candidates=(
                    SpotifyAlbumCandidate(
                        album_id="different-album",
                        uri="spotify:album:different-album",
                        name="Different Album",
                        artists=("Example Artist",),
                        total_tracks=3,
                    ),
                ),
                album_tracks_by_id={"recovered-album": invalid_album_tracks},
            )
            first_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=first_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
                max_new_searches_per_run=1,
            )
            first_cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            second_client = AlbumPublishingSpotifyClient(
                album_candidates=(),
                album_tracks_by_id={},
            )
            second_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=second_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
                max_new_searches_per_run=1,
            )

        self.assertEqual(first_summary.search_count, 1)
        self.assertEqual(first_client.album_track_requests, [("access-token", "recovered-album")])
        self.assertTrue(
            all(
                record[ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD] == MATCHER_VERSION
                and record[ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD] == ["recovered-album"]
                for record in first_cache_payload["matches"].values()
            )
        )
        self.assertEqual(second_summary.search_count, 0)
        self.assertEqual(second_client.album_searches, [])
        self.assertEqual(second_client.album_track_requests, [])
        self.assertEqual(second_client.searches, [])

    def test_cached_album_recovery_retries_after_hint_or_version_change_and_refresh(self):
        retry_cases = (
            ("hint-change", ("new-album",), ("old-album",), MATCHER_VERSION, False),
            (
                "attempt-version-change",
                ("recovered-album",),
                ("recovered-album",),
                MATCHER_VERSION - 1,
                False,
            ),
            (
                "explicit-refresh",
                ("recovered-album",),
                ("recovered-album",),
                MATCHER_VERSION,
                True,
            ),
        )
        for (
            case_name,
            candidate_album_ids,
            attempted_album_ids,
            attempt_matcher_version,
            refresh_match_cache,
        ) in retry_cases:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                playlist_directory, match_cache_path, _source_rows = write_cached_negative_release(
                    directory,
                    candidate_album_ids,
                    attempted_album_ids=attempted_album_ids,
                    attempt_matcher_version=attempt_matcher_version,
                )
                fetched_album_id = (
                    "fresh-album"
                    if refresh_match_cache
                    else candidate_album_ids[0]
                )
                spotify_album_tracks = tuple(
                    SpotifyAlbumTrack(
                        uri=f"spotify:track:{fetched_album_id}-{index}",
                        name=f"Anchor {index}",
                        artists=("Example Artist",),
                        disc_number=1,
                        track_number=index,
                        is_playable=True,
                    )
                    for index in range(1, 4)
                )
                album_candidates = (
                    SpotifyAlbumCandidate(
                        album_id=fetched_album_id,
                        uri=f"spotify:album:{fetched_album_id}",
                        name=(
                            "Recovered Album"
                            if refresh_match_cache
                            else "Different Album"
                        ),
                        artists=("Example Artist",),
                        total_tracks=3,
                    ),
                )
                client = AlbumPublishingSpotifyClient(
                    album_candidates=album_candidates,
                    album_tracks_by_id={fetched_album_id: spotify_album_tracks},
                )

                summary = publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=directory / "reports" / "spotify-report.txt",
                    spotify_client=client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    apply=False,
                    publisher_sync_mode="append",
                    refresh_match_cache=refresh_match_cache,
                )

            self.assertEqual(summary.search_count, 1)
            self.assertEqual(
                client.album_track_requests,
                [("access-token", fetched_album_id)],
            )
            if refresh_match_cache:
                self.assertNotIn(
                    ("access-token", "recovered-album"),
                    client.album_track_requests,
                )

    def test_cached_album_recovery_is_retryable_after_transient_rate_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory, match_cache_path, _source_rows = write_cached_negative_release(
                directory,
                ("recovered-album",),
            )
            deferred_client = DeferredAlbumPublishingSpotifyClient((), {})

            with self.assertRaises(SpotifyRateLimitDeferredError):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=directory / "reports" / "spotify-report.txt",
                    spotify_client=deferred_client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    apply=False,
                    publisher_sync_mode="append",
                )
            deferred_cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            invalid_album_tracks = tuple(
                SpotifyAlbumTrack(
                    uri=f"spotify:track:different-{index}",
                    name=f"Different {index}",
                    artists=("Other Artist",),
                    disc_number=1,
                    track_number=index,
                    is_playable=True,
                )
                for index in range(1, 4)
            )
            retry_client = AlbumPublishingSpotifyClient(
                album_candidates=(
                    SpotifyAlbumCandidate(
                        album_id="different-album",
                        uri="spotify:album:different-album",
                        name="Different Album",
                        artists=("Example Artist",),
                        total_tracks=3,
                    ),
                ),
                album_tracks_by_id={"recovered-album": invalid_album_tracks},
            )
            retry_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=retry_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertTrue(
            all(
                ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD not in record
                and ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD not in record
                for record in deferred_cache_payload["matches"].values()
            )
        )
        self.assertEqual(retry_summary.search_count, 1)
        self.assertEqual(
            retry_client.album_track_requests,
            [("access-token", "recovered-album")],
        )

    def test_partial_cached_album_recovery_marks_only_unresolved_negative_rows(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory, match_cache_path, source_rows = write_cached_negative_release(
                directory,
                ("recovered-album",),
            )
            partial_album_tracks = (
                SpotifyAlbumTrack(
                    uri="spotify:track:recovered-1",
                    name="Anchor 1",
                    artists=("Example Artist",),
                    disc_number=1,
                    track_number=1,
                    is_playable=True,
                ),
                SpotifyAlbumTrack(
                    uri="spotify:track:wrong-2",
                    name="Different 2",
                    artists=("Other Artist",),
                    disc_number=1,
                    track_number=2,
                    is_playable=True,
                ),
                SpotifyAlbumTrack(
                    uri="spotify:track:recovered-3",
                    name="Anchor 3",
                    artists=("Example Artist",),
                    disc_number=1,
                    track_number=3,
                    is_playable=True,
                ),
            )
            first_client = AlbumPublishingSpotifyClient(
                album_candidates=(
                    SpotifyAlbumCandidate(
                        album_id="different-album",
                        uri="spotify:album:different-album",
                        name="Different Album",
                        artists=("Example Artist",),
                        total_tracks=3,
                    ),
                ),
                album_tracks_by_id={"recovered-album": partial_album_tracks},
            )
            first_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=first_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            first_cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            cache_keys = [
                spotify_track_match_key(
                    playlist_track(
                        "Techno",
                        row["Release Id"],
                        row["Album Name"],
                        row["Track Name"],
                        row["Artist Name"],
                        row["Track Number"],
                    )
                )
                for row in source_rows
            ]
            second_client = AlbumPublishingSpotifyClient((), {})
            second_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=second_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(first_summary.matched_count, 2)
        self.assertEqual(first_cache_payload["matches"][cache_keys[0]]["match_status"], "matched")
        self.assertEqual(first_cache_payload["matches"][cache_keys[2]]["match_status"], "matched")
        self.assertNotIn(
            ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD,
            first_cache_payload["matches"][cache_keys[0]],
        )
        self.assertNotIn(
            ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD,
            first_cache_payload["matches"][cache_keys[2]],
        )
        self.assertEqual(
            first_cache_payload["matches"][cache_keys[1]][
                ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD
            ],
            ["recovered-album"],
        )
        self.assertEqual(second_summary.search_count, 0)
        self.assertEqual(second_client.album_searches, [])
        self.assertEqual(second_client.album_track_requests, [])

    def test_ordinary_album_search_failure_does_not_record_cached_fallback_attempt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory, match_cache_path, _source_rows = write_cached_negative_release(
                directory,
                ("recovered-album",),
            )
            invalid_album_tracks = tuple(
                SpotifyAlbumTrack(
                    uri=f"spotify:track:ordinary-{index}",
                    name=f"Different {index}",
                    artists=("Other Artist",),
                    disc_number=1,
                    track_number=index,
                    is_playable=True,
                )
                for index in range(1, 4)
            )
            client = AlbumPublishingSpotifyClient(
                album_candidates=(
                    SpotifyAlbumCandidate(
                        album_id="ordinary-album",
                        uri="spotify:album:ordinary-album",
                        name="Recovered Album",
                        artists=("Example Artist",),
                        total_tracks=3,
                    ),
                ),
                album_tracks_by_id={"ordinary-album": invalid_album_tracks},
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(summary.search_count, 1)
        self.assertEqual(client.album_track_requests, [("access-token", "ordinary-album")])
        self.assertTrue(
            all(
                ALBUM_RECOVERY_ATTEMPT_ALBUM_IDS_FIELD not in record
                and ALBUM_RECOVERY_ATTEMPT_MATCHER_VERSION_FIELD not in record
                for record in cache_payload["matches"].values()
            )
        )

    def test_album_rate_limit_stops_without_track_search_fallback_storm(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                [
                    playlist_row("release-1", "Numbers and Colours", str(index), "Example Artist", str(index))
                    for index in range(1, 4)
                ],
            )
            client = DeferredAlbumPublishingSpotifyClient((), {})

            with self.assertRaises(SpotifyRateLimitDeferredError):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=directory / "reports" / "spotify-report.txt",
                    spotify_client=client,
                    access_token="access-token",
                    match_cache_path=directory / "collection" / "cache" / "spotify-track-matches.cache.json",
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    apply=False,
                    publisher_sync_mode="append",
                )

        self.assertEqual(len(client.album_searches), 1)
        self.assertEqual(client.searches, [])

    def test_album_lookup_only_falls_back_for_unresolved_tracks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            source_rows = [
                playlist_row("release-1", "Numbers and Colours", "1", "Example Artist, Collaborator", "1"),
                playlist_row("release-1", "Numbers and Colours", "2", "Example Artist, Collaborator", "2"),
                playlist_row("release-1", "Numbers and Colours", "3", "Example Artist, Collaborator", "3"),
                playlist_row("release-1", "Numbers and Colours", "Anchor Four", "Example Artist, Collaborator", "4"),
                playlist_row("release-1", "Numbers and Colours", "Anchor Five", "Example Artist, Collaborator", "5"),
                playlist_row("release-1", "Numbers and Colours", "Anchor Six", "Example Artist", "6"),
            ]
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                source_rows,
            )
            fallback_candidate = SpotifyTrackCandidate(
                uri="spotify:track:fallback-two",
                name="2",
                artists=("Example Artist", "Collaborator"),
                album_name="Numbers and Colours",
            )
            spotify_album_tracks = (
                SpotifyAlbumTrack("spotify:track:one", "White", ("Example Artist", "Collaborator"), 1, 1, True),
                SpotifyAlbumTrack("spotify:track:two", "Grey", ("Different Artist",), 1, 2, True),
                SpotifyAlbumTrack("spotify:track:three", "Black", ("Example Artist", "Collaborator"), 1, 3, True),
                SpotifyAlbumTrack("spotify:track:four", "Anchor Four", ("Example Artist", "Collaborator"), 1, 4, True),
                SpotifyAlbumTrack("spotify:track:five", "Anchor Five", ("Example Artist", "Collaborator"), 1, 5, True),
                SpotifyAlbumTrack("spotify:track:six", "Anchor Six", ("Example Artist",), 1, 6, True),
            )
            client = AlbumPublishingSpotifyClient(
                album_candidates=(
                    SpotifyAlbumCandidate(
                        album_id="album-1",
                        uri="spotify:album:album-1",
                        name="Numbers and Colours",
                        artists=("Example Artist",),
                        total_tracks=6,
                    ),
                ),
                album_tracks_by_id={"album-1": spotify_album_tracks},
                candidates_by_query={
                    'track:"2" artist:"Example Artist, Collaborator" album:"Numbers and Colours"': (
                        fallback_candidate,
                    ),
                },
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=directory / "collection" / "cache" / "spotify-track-matches.cache.json",
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(summary.search_count, 2)
        self.assertEqual(summary.searched_row_count, 1)
        self.assertEqual(summary.would_add_count, 6)
        self.assertEqual(len(client.album_searches), 1)
        self.assertEqual(len(client.searches), 1)
        self.assertEqual(client.searches[0][1], 'track:"2" artist:"Example Artist, Collaborator" album:"Numbers and Colours"')
        self.assertEqual(
            [decision.match_source for decision in summary.decisions],
            ["album", "search", "album", "album", "album", "album"],
        )

    def test_unmatched_track_reason_includes_album_lookup_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                [
                    playlist_row("release-1", "Missing Album", f"Track {index}", "Example Artist", str(index))
                    for index in range(1, 4)
                ],
            )
            client = AlbumPublishingSpotifyClient(
                album_candidates=(),
                album_tracks_by_id={},
                candidates_by_query={},
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=directory / "collection" / "cache" / "spotify-track-matches.cache.json",
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(summary.unmatched_count, 3)
        self.assertTrue(
            all(
                decision.reason.endswith(
                    "album lookup: Spotify returned no album candidate with a matching title"
                )
                for decision in summary.decisions
            )
        )

    def test_album_server_error_stops_without_track_search_fallback_storm(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                [
                    playlist_row("release-1", "Numbers and Colours", str(index), "Example Artist", str(index))
                    for index in range(1, 4)
                ],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            client = FailingAlbumPublishingSpotifyClient((), {})

            with self.assertRaises(SpotifyAlbumLookupError):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=client,
                    access_token="access-token",
                    match_cache_path=directory / "collection" / "cache" / "spotify-track-matches.cache.json",
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    apply=False,
                    publisher_sync_mode="append",
                )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(len(client.album_searches), 1)
        self.assertEqual(client.searches, [])
        self.assertIn("aborted - Spotify API error", report_text)

    def test_album_lookup_is_reused_across_selected_playlists(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row("release-1", "Numbers and Colours", "1", "Example Artist", "1"),
                playlist_row("release-1", "Numbers and Colours", "Anchor Two", "Example Artist", "2"),
                playlist_row("release-1", "Numbers and Colours", "Anchor Three", "Example Artist", "3"),
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv", rows)
            client = AlbumPublishingSpotifyClient(
                album_candidates=(
                    SpotifyAlbumCandidate(
                        album_id="album-1",
                        uri="spotify:album:album-1",
                        name="Numbers and Colours",
                        artists=("Example Artist",),
                        total_tracks=3,
                    ),
                ),
                album_tracks_by_id={
                    "album-1": (
                        SpotifyAlbumTrack("spotify:track:one", "White", ("Example Artist",), 1, 1, True),
                        SpotifyAlbumTrack("spotify:track:two", "Anchor Two", ("Example Artist",), 1, 2, True),
                        SpotifyAlbumTrack("spotify:track:three", "Anchor Three", ("Example Artist",), 1, 3, True),
                    )
                },
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=directory / "collection" / "cache" / "spotify-track-matches.cache.json",
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(summary.search_count, 1)
        self.assertEqual(summary.would_add_count, 6)
        self.assertEqual(len(client.album_searches), 1)
        self.assertEqual(len(client.album_track_requests), 1)
        self.assertEqual(client.searches, [])

    def test_album_matches_are_cached_before_search_budget_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row("release-1", "Numbers and Colours", "1", "Example Artist", "1"),
                playlist_row("release-1", "Numbers and Colours", "Anchor Two", "Example Artist", "2"),
                playlist_row("release-1", "Numbers and Colours", "Anchor Three", "Example Artist", "3"),
            ]
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv", rows)
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            report_path = directory / "reports" / "spotify-report.txt"
            album_candidate_value = SpotifyAlbumCandidate(
                album_id="album-1",
                uri="spotify:album:album-1",
                name="Numbers and Colours",
                artists=("Example Artist",),
                total_tracks=3,
            )
            album_tracks = (
                SpotifyAlbumTrack("spotify:track:one", "White", ("Example Artist",), 1, 1, True),
                SpotifyAlbumTrack("spotify:track:two", "Anchor Two", ("Example Artist",), 1, 2, True),
                SpotifyAlbumTrack("spotify:track:three", "Anchor Three", ("Example Artist",), 1, 3, True),
            )
            first_client = AlbumPublishingSpotifyClient(
                album_candidates=(album_candidate_value,),
                album_tracks_by_id={"album-1": album_tracks},
            )

            first_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=first_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
                max_new_searches_per_run=1,
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            second_client = AlbumPublishingSpotifyClient((), {})
            second_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=second_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
                max_new_searches_per_run=1,
            )

        self.assertEqual(first_summary.search_count, 1)
        self.assertEqual(first_summary.track_count, 1)
        self.assertEqual(len(cache_payload["matches"]), 3)
        self.assertEqual(second_summary.search_count, 0)
        self.assertEqual(second_summary.cache_hit_count, 3)
        self.assertEqual(second_summary.track_count, 3)
        self.assertEqual(second_client.album_searches, [])

    def test_album_lookup_preserves_manual_match_and_can_replace_partial_negative_cache(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row("release-1", "Numbers and Colours", "1", "Example Artist", "1"),
                playlist_row("release-1", "Numbers and Colours", "Anchor Two", "Example Artist", "2"),
                playlist_row("release-1", "Numbers and Colours", "Anchor Three", "Example Artist", "3"),
            ]
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv", rows)
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": {
                            "release-1|1|example artist|numbers and colours|1": {
                                "release_id": "release-1",
                                "track_number": "1",
                                "artist_name": "Example Artist",
                                "album_name": "Numbers and Colours",
                                "track_name": "1",
                                "match_status": "manual",
                                "match_reason": "manually selected Spotify match",
                                "spotify_uri": "spotify:track:manual-one",
                                "spotify_track_name": "Manual One",
                                "spotify_artist_names": ["Example Artist"],
                                "spotify_album_name": "Numbers and Colours",
                            },
                            "release-1|2|example artist|numbers and colours|anchor two": {
                                "release_id": "release-1",
                                "track_number": "2",
                                "artist_name": "Example Artist",
                                "album_name": "Numbers and Colours",
                                "track_name": "Anchor Two",
                                "match_status": "unmatched",
                                "match_reason": "no Spotify track matched",
                                "matcher_version": MATCHER_VERSION,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            client = AlbumPublishingSpotifyClient(
                album_candidates=(
                    SpotifyAlbumCandidate(
                        album_id="album-1",
                        uri="spotify:album:album-1",
                        name="Numbers and Colours",
                        artists=("Example Artist",),
                        total_tracks=3,
                    ),
                ),
                album_tracks_by_id={
                    "album-1": (
                        SpotifyAlbumTrack("spotify:track:album-one", "White", ("Example Artist",), 1, 1, True),
                        SpotifyAlbumTrack("spotify:track:album-two", "Anchor Two", ("Example Artist",), 1, 2, True),
                        SpotifyAlbumTrack("spotify:track:album-three", "Anchor Three", ("Example Artist",), 1, 3, True),
                    )
                },
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(summary.search_count, 1)
        self.assertEqual(
            [decision.match_source for decision in summary.decisions],
            ["cache", "album", "album"],
        )
        self.assertEqual(summary.decisions[0].spotify_uri, "spotify:track:manual-one")
        self.assertEqual(cache_payload["matches"][
            "release-1|1|example artist|numbers and colours|1"
        ]["match_status"], "manual")
        self.assertEqual(cache_payload["matches"][
            "release-1|2|example artist|numbers and colours|anchor two"
        ]["match_status"], "matched")

    def test_publish_dry_run_uses_match_cache_fetches_playlist_state_and_reports_final_append_plan(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [
                    playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist"),
                    playlist_row("222", "Beta Album", "Beta One", "Beta Artist"),
                ],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": {
                            "111|1|alpha artist|alpha album|alpha one": {
                                "release_id": "111",
                                "track_number": "1",
                                "artist_name": "Alpha Artist",
                                "album_name": "Alpha Album",
                                "track_name": "Alpha One",
                                "search_query": 'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
                                "spotify_uri": "spotify:track:alpha",
                                "spotify_url": "https://open.spotify.com/track/alpha",
                                "spotify_track_name": "Alpha One",
                                "spotify_artist_names": ["Alpha Artist"],
                                "spotify_album_name": "Alpha Album",
                                "match_status": "matched",
                                "match_reason": "track, artist, and album matched",
                                "matcher_version": 1,
                                "matched_at": "2026-06-18T00:00:00Z",
                                "last_seen_at": "2026-06-18T00:00:00Z",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            report_path = directory / "reports" / "spotify-report.txt"
            client = PublishingSpotifyClient(
                {
                    'track:"Beta One" artist:"Beta Artist" album:"Beta Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:beta",
                            name="Beta One",
                            artists=("Beta Artist",),
                            album_name="Beta Album",
                        ),
                    )
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="https://open.spotify.com/playlist/playlist-house",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
                playlist_items_by_id={
                    "playlist-house": (
                        SpotifyPlaylistItem(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    )
                },
            )
            info_lines = []

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
                info_log=info_lines.append,
            )
            report_text = report_path.read_text(encoding="utf-8")
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(summary.cache_hit_count, 1)
        self.assertEqual(summary.search_count, 1)
        self.assertEqual(summary.already_present_count, 1)
        self.assertEqual(summary.would_add_count, 1)
        self.assertEqual(summary.added_count, 0)
        self.assertEqual(client.searches, [('access-token', 'track:"Beta One" artist:"Beta Artist" album:"Beta Album"', 10)])
        self.assertEqual(client.add_calls, [])
        self.assertEqual(client.replace_calls, [])
        self.assertEqual(client.created_playlists, [])
        self.assertIn("Playlist Discogs - House already exists with 1 songs, updating", info_lines)
        self.assertIn("Spotify playlist publish dry-run report", report_text)
        self.assertIn("Publisher sync mode: append", report_text)
        self.assertIn("Cache hits: 1", report_text)
        self.assertIn("Would add tracks: 1", report_text)
        self.assertIn("Already-present tracks", report_text)
        self.assertIn("Discogs - House | 111 | 1 | Alpha Artist | Alpha One | already_present | spotify:track:alpha", report_text)
        self.assertIn("Tracks that would be added", report_text)
        self.assertIn("Discogs - House | 222 | 1 | Beta Artist | Beta One | would_add | spotify:track:beta", report_text)
        self.assertIn("Final planned playlist state", report_text)
        self.assertIn("1 | existing | Alpha Artist | Alpha One | Alpha Album | spotify:track:alpha", report_text)
        self.assertIn("2 | would_add | Beta Artist | Beta One | Beta Album | spotify:track:beta", report_text)
        self.assertIn("222|1|beta artist|beta album|beta one", cache_payload["matches"])

    def test_publish_dry_run_caches_unmatched_and_ambiguous_decisions_for_reruns(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [
                    playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist"),
                    playlist_row("222", "Beta Album", "Beta One", "Beta Artist"),
                ],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            report_path = directory / "reports" / "spotify-report.txt"
            first_client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (),
                    'track:"Beta One" artist:"Beta Artist" album:"Beta Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:beta-first",
                            name="Beta One",
                            artists=("Beta Artist",),
                            album_name="Beta Album",
                        ),
                        SpotifyTrackCandidate(
                            uri="spotify:track:beta-second",
                            name="Beta One",
                            artists=("Beta Artist",),
                            album_name="Beta Album",
                        ),
                    ),
                }
            )

            first_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=first_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            second_client = PublishingSpotifyClient({})

            second_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=second_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(first_summary.cache_hit_count, 0)
        self.assertEqual(first_summary.search_count, 5)
        self.assertEqual(first_summary.unmatched_count, 1)
        self.assertEqual(first_summary.ambiguous_count, 1)
        self.assertEqual(cache_payload["matches"]["111|1|alpha artist|alpha album|alpha one"]["match_status"], "unmatched")
        self.assertEqual(cache_payload["matches"]["222|1|beta artist|beta album|beta one"]["match_status"], "ambiguous")
        self.assertEqual(second_client.searches, [])
        self.assertEqual(second_summary.cache_hit_count, 2)
        self.assertEqual(second_summary.search_count, 0)
        self.assertEqual(second_summary.unmatched_count, 1)
        self.assertEqual(second_summary.ambiguous_count, 1)
        self.assertIn("Cache hits: 2", report_text)
        self.assertIn("Spotify searches: 0", report_text)
        self.assertIn("Discogs - House | 111 | 1 | Alpha Artist | Alpha One | unmatched | no Spotify URI", report_text)
        self.assertIn("Discogs - House | 222 | 1 | Beta Artist | Beta One | ambiguous | no Spotify URI", report_text)

    def test_publish_refresh_match_cache_rechecks_cached_unmatched_decisions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": {
                            "111|1|alpha artist|alpha album|alpha one": {
                                "release_id": "111",
                                "track_number": "1",
                                "artist_name": "Alpha Artist",
                                "album_name": "Alpha Album",
                                "track_name": "Alpha One",
                                "search_query": 'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
                                "spotify_uri": "",
                                "spotify_url": "",
                                "match_status": "unmatched",
                                "match_reason": "no candidates matched track, artist, and album",
                                "matcher_version": 3,
                                "searched_at": "2026-06-18T00:00:00Z",
                                "last_seen_at": "2026-06-18T00:00:00Z",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            report_path = directory / "reports" / "spotify-report.txt"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    )
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
                refresh_match_cache=True,
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(client.searches, [('access-token', 'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"', 10)])
        self.assertEqual(summary.cache_hit_count, 0)
        self.assertEqual(summary.search_count, 1)
        self.assertEqual(summary.would_add_count, 1)
        self.assertEqual(cache_payload["matches"]["111|1|alpha artist|alpha album|alpha one"]["match_status"], "matched")
        self.assertEqual(cache_payload["matches"]["111|1|alpha artist|alpha album|alpha one"]["spotify_uri"], "spotify:track:alpha")

    def test_publish_applies_constrained_typo_fallback_only_after_search_ladder_finishes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Alpha Signel", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            typo_candidate = SpotifyTrackCandidate(
                uri="spotify:track:typo",
                name="Alpha Signal",
                artists=("Alpha Artist",),
                album_name="Alpha Album",
            )
            exact_candidate = SpotifyTrackCandidate(
                uri="spotify:track:exact",
                name="Alpha Signel",
                artists=("Alpha Artist",),
                album_name="Alpha Album",
            )
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha Signel" artist:"Alpha Artist" album:"Alpha Album"': (typo_candidate,),
                    'track:"Alpha Signel" artist:"Alpha Artist"': (exact_candidate,),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        cache_record = cache_payload["matches"]["111|1|alpha artist|alpha album|alpha signel"]
        self.assertEqual(summary.search_count, 2)
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(cache_record["spotify_uri"], "spotify:track:exact")
        self.assertEqual(cache_record["match_reason"], "track, artist, and album matched")
        self.assertNotIn("version_sensitive", cache_record)

    def test_publish_applies_alphanumeric_spacing_only_after_search_ladder_finishes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                [playlist_row("35661301", "Chroma000", "Chroma001 Helium", "B.D.B")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            spacing_candidate = SpotifyTrackCandidate(
                uri="spotify:track:spacing",
                name="CHROMA 001 HELIUM",
                artists=("B.D.B",),
                album_name="CHROMA 000",
            )
            exact_candidate = SpotifyTrackCandidate(
                uri="spotify:track:exact",
                name="Chroma001 Helium",
                artists=("B.D.B",),
                album_name="Chroma000",
            )
            client = PublishingSpotifyClient(
                {
                    'track:"Chroma001 Helium" artist:"B.D.B" album:"Chroma000"': (
                        spacing_candidate,
                    ),
                    'track:"Chroma001 Helium" artist:"B.D.B"': (exact_candidate,),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        cache_record = cache_payload["matches"]["35661301|1|b d b|chroma000|chroma001 helium"]
        self.assertEqual(summary.search_count, 2)
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(cache_record["spotify_uri"], "spotify:track:exact")
        self.assertEqual(cache_record["match_reason"], "track, artist, and album matched")
        self.assertNotIn("version_sensitive", cache_record)

    def test_publish_applies_version_substitute_only_after_search_ladder_finishes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                [playlist_row("5083040", "First Contact", "First Contact", "Outline")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            substitute_candidate = SpotifyTrackCandidate(
                uri="spotify:track:remastered",
                name="First Contact - Remastered",
                artists=("Outline",),
                album_name="First Contact (Remastered)",
            )
            exact_candidate = SpotifyTrackCandidate(
                uri="spotify:track:exact",
                name="First Contact",
                artists=("Outline",),
                album_name="First Contact",
            )
            client = PublishingSpotifyClient(
                {
                    'track:"First Contact" artist:"Outline" album:"First Contact"': (
                        substitute_candidate,
                    ),
                    'track:"First Contact" artist:"Outline"': (exact_candidate,),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        cache_record = cache_payload["matches"]["5083040|1|outline|first contact|first contact"]
        self.assertEqual(summary.search_count, 2)
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(cache_record["spotify_uri"], "spotify:track:exact")
        self.assertEqual(cache_record["match_reason"], "track, artist, and album matched")
        self.assertNotIn("version_sensitive", cache_record)

    def test_publish_caches_and_reports_alphanumeric_spacing_as_version_sensitive(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                [playlist_row("35661301", "Chroma000", "Chroma001 Helium", "B.D.B")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            spacing_candidate = SpotifyTrackCandidate(
                uri="spotify:track:spacing",
                name="CHROMA 001 HELIUM",
                artists=("B.D.B",),
                album_name="CHROMA 000",
            )
            client = PublishingSpotifyClient(
                {
                    'track:"Chroma001 Helium" artist:"B.D.B" album:"Chroma000"': (
                        spacing_candidate,
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            searched_report_text = report_path.read_text(encoding="utf-8")
            second_client = PublishingSpotifyClient({})
            second_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=second_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cached_report_text = report_path.read_text(encoding="utf-8")

        cache_record = cache_payload["matches"]["35661301|1|b d b|chroma000|chroma001 helium"]
        expected_reason = "track and album matched after normalizing letter-number spacing"
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(cache_record["matcher_version"], MATCHER_VERSION)
        self.assertEqual(cache_record["match_strategy"], "alphanumeric_spacing")
        self.assertIs(cache_record["version_sensitive"], True)
        self.assertEqual(cache_record["match_reason"], expected_reason)
        self.assertIn(expected_reason, searched_report_text)
        self.assertEqual(second_summary.cache_hit_count, 1)
        self.assertEqual(second_summary.search_count, 0)
        self.assertEqual(second_client.searches, [])
        self.assertIn(expected_reason, cached_report_text)

    def test_publish_caches_version_substitute_as_version_sensitive(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                [playlist_row("5083040", "First Contact", "First Contact", "Outline")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            substitute_candidate = SpotifyTrackCandidate(
                uri="spotify:track:remastered",
                name="First Contact - Remastered",
                artists=("Outline",),
                album_name="First Contact (Remastered)",
            )
            client = PublishingSpotifyClient(
                {
                    'track:"First Contact" artist:"Outline" album:"First Contact"': (
                        substitute_candidate,
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            searched_report_text = report_path.read_text(encoding="utf-8")
            second_client = PublishingSpotifyClient({})
            second_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=second_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cached_report_text = report_path.read_text(encoding="utf-8")

        cache_record = cache_payload["matches"]["5083040|1|outline|first contact|first contact"]
        expected_reason = (
            "track matched using the remastered version substitute "
            "after no ordinary title match"
        )
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(cache_record["matcher_version"], MATCHER_VERSION)
        self.assertEqual(cache_record["match_strategy"], "version_substitute")
        self.assertIs(cache_record["version_sensitive"], True)
        self.assertEqual(cache_record["match_reason"], expected_reason)
        self.assertIn(expected_reason, searched_report_text)
        self.assertEqual(second_summary.cache_hit_count, 1)
        self.assertEqual(second_summary.search_count, 0)
        self.assertEqual(second_client.searches, [])
        self.assertIn(expected_reason, cached_report_text)

    def test_publish_caches_and_reports_constrained_typo_match_as_version_sensitive(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Loosing Time", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            typo_candidate = SpotifyTrackCandidate(
                uri="spotify:track:losing-time",
                name="Losing Time",
                artists=("Alpha Artist",),
                album_name="Alpha Album",
            )
            client = PublishingSpotifyClient(
                {
                    'track:"Loosing Time" artist:"Alpha Artist" album:"Alpha Album"': (typo_candidate,),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            report_text = report_path.read_text(encoding="utf-8")
            second_client = PublishingSpotifyClient({})
            second_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=second_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        cache_record = cache_payload["matches"]["111|1|alpha artist|alpha album|loosing time"]
        expected_reason = (
            "track title matched by constrained typo fallback (distance 1); "
            "artist set and album matched"
        )
        self.assertEqual(summary.search_count, 4)
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(cache_record["matcher_version"], MATCHER_VERSION)
        self.assertEqual(cache_record["match_strategy"], "constrained_title_typo")
        self.assertIs(cache_record["version_sensitive"], True)
        self.assertEqual(cache_record["match_reason"], expected_reason)
        self.assertIn(expected_reason, report_text)
        self.assertEqual(second_summary.cache_hit_count, 1)
        self.assertEqual(second_summary.search_count, 0)
        self.assertEqual(second_client.searches, [])

    def test_publish_researches_stale_version_sensitive_typo_match(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Loosing Time", "Alpha Artist")],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": {
                            "111|1|alpha artist|alpha album|loosing time": {
                                "release_id": "111",
                                "track_number": "1",
                                "artist_name": "Alpha Artist",
                                "album_name": "Alpha Album",
                                "track_name": "Loosing Time",
                                "search_query": 'track:"Loosing Time" artist:"Alpha Artist" album:"Alpha Album"',
                                "match_status": "matched",
                                "match_reason": (
                                    "track title matched by constrained typo fallback (distance 1); "
                                    "artist set and album matched"
                                ),
                                "matcher_version": 9,
                                "match_strategy": "constrained_title_typo",
                                "version_sensitive": True,
                                "spotify_uri": "spotify:track:losing-time",
                                "spotify_track_name": "Losing Time",
                                "spotify_artist_names": ["Alpha Artist"],
                                "spotify_album_name": "Alpha Album",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            report_path = directory / "reports" / "spotify-report.txt"
            typo_candidate = SpotifyTrackCandidate(
                uri="spotify:track:losing-time",
                name="Losing Time",
                artists=("Alpha Artist",),
                album_name="Alpha Album",
            )
            client = PublishingSpotifyClient(
                {
                    'track:"Loosing Time" artist:"Alpha Artist" album:"Alpha Album"': (typo_candidate,),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        cache_record = cache_payload["matches"]["111|1|alpha artist|alpha album|loosing time"]
        self.assertEqual(summary.cache_hit_count, 0)
        self.assertEqual(summary.search_count, 4)
        self.assertEqual(cache_record["matcher_version"], MATCHER_VERSION)

    def test_publish_ladder_matches_unique_title_and_artist_when_album_differs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Discogs EP", "Alpha One", "Alpha Artist")],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            report_path = directory / "reports" / "spotify-report.txt"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Discogs EP"': (),
                    'track:"Alpha One" artist:"Alpha Artist"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Spotify Single",
                        ),
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            report_text = report_path.read_text(encoding="utf-8")
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(
            client.searches,
            [
                ("access-token", 'track:"Alpha One" artist:"Alpha Artist" album:"Discogs EP"', 10),
                ("access-token", 'track:"Alpha One" artist:"Alpha Artist"', 10),
            ],
        )
        self.assertEqual(summary.search_count, 2)
        self.assertEqual(summary.searched_row_count, 1)
        self.assertEqual(summary.would_add_count, 1)
        self.assertIn("Rows searched with ladder: 1", report_text)
        self.assertEqual(cache_payload["matches"]["111|1|alpha artist|discogs ep|alpha one"]["match_status"], "matched")
        self.assertEqual(
            cache_payload["matches"]["111|1|alpha artist|discogs ep|alpha one"]["match_reason"],
            "track and artist matched; album differed",
        )

    def test_publish_ladder_matches_plain_artist_and_title_query(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            row = playlist_row("25308169", "False Hope", "Loosing Time", "Heap")
            row["Track Number"] = "7"
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [row],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            report_path = directory / "reports" / "spotify-report.txt"
            client = PublishingSpotifyClient(
                {
                    "Heap Loosing Time": (
                        SpotifyTrackCandidate(
                            uri="spotify:track:heap-loosing-time",
                            name="Loosing Time",
                            artists=("Heap",),
                            album_name="False Hope",
                        ),
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(
            client.searches,
            [
                ("access-token", 'track:"Loosing Time" artist:"Heap" album:"False Hope"', 10),
                ("access-token", 'track:"Loosing Time" artist:"Heap"', 10),
                ("access-token", "Heap Loosing Time", 10),
            ],
        )
        self.assertEqual(summary.search_count, 3)
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(summary.would_add_count, 1)

    def test_publish_ladder_researches_stale_featured_credit_and_accepts_relocated_artists(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            row = playlist_row(
                "37672899",
                "Carl Gari",
                "Swim feat. Polygonia",
                "Carl Gari",
            )
            row["Track Number"] = "2"
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [row],
            )
            cache_key = "37672899|2|carl gari|carl gari|swim feat polygonia"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": {
                            cache_key: {
                                "release_id": "37672899",
                                "track_number": "2",
                                "artist_name": "Carl Gari",
                                "album_name": "Carl Gari",
                                "track_name": "Swim feat. Polygonia",
                                "search_query": (
                                    'track:"Swim feat. Polygonia" artist:"Carl Gari" album:"Carl Gari"'
                                ),
                                "spotify_uri": "",
                                "spotify_url": "",
                                "match_status": "unmatched",
                                "match_reason": "no candidates matched track, artist, and album",
                                "matcher_version": 7,
                                "searched_at": "2026-07-10T00:00:00Z",
                                "last_seen_at": "2026-07-10T00:00:00Z",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            report_path = directory / "reports" / "spotify-report.txt"
            candidate = SpotifyTrackCandidate(
                uri="spotify:track:swim",
                name="Swim",
                artists=("Carl Gari", "Polygonia"),
                album_name="Swim",
            )
            client = PublishingSpotifyClient(
                {
                    'track:"Swim feat. Polygonia" artist:"Carl Gari" album:"Carl Gari"': (
                        candidate,
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            report_text = report_path.read_text(encoding="utf-8")

        cache_record = cache_payload["matches"][cache_key]
        self.assertEqual(summary.cache_hit_count, 0)
        self.assertEqual(summary.search_count, 1)
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(summary.would_add_count, 1)
        self.assertEqual(cache_record["matcher_version"], MATCHER_VERSION)
        self.assertEqual(cache_record["spotify_uri"], "spotify:track:swim")
        self.assertEqual(cache_record["spotify_artist_names"], ["Carl Gari", "Polygonia"])
        self.assertEqual(
            cache_record["match_reason"],
            "track matched after moving source featured credit to Spotify artists; "
            "source and featured artists matched; album differed",
        )
        self.assertIn(
            "track matched after moving source featured credit to Spotify artists; "
            "source and featured artists matched; album differed",
            report_text,
        )

    def test_publish_ladder_researches_stale_match_and_accepts_spotify_only_featured_credit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            row = playlist_row(
                "111",
                "Source Album",
                "Signal Path",
                "Main Artist",
            )
            write_playlist_master(
                playlist_directory / "Test" / "Test.csv",
                [row],
            )
            cache_key = "111|1|main artist|source album|signal path"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": {
                            cache_key: {
                                "release_id": "111",
                                "track_number": "1",
                                "artist_name": "Main Artist",
                                "album_name": "Source Album",
                                "track_name": "Signal Path",
                                "search_query": (
                                    'track:"Signal Path" artist:"Main Artist" album:"Source Album"'
                                ),
                                "spotify_uri": "",
                                "spotify_url": "",
                                "match_status": "unmatched",
                                "match_reason": "no candidates matched track, artist, and album",
                                "matcher_version": 8,
                                "searched_at": "2026-07-10T00:00:00Z",
                                "last_seen_at": "2026-07-10T00:00:00Z",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            report_path = directory / "reports" / "spotify-report.txt"
            candidate = SpotifyTrackCandidate(
                uri="spotify:track:signal-path",
                name="Signal Path feat. Guest One",
                artists=("Main Artist", "Guest One"),
                album_name="Spotify Album",
            )
            client = PublishingSpotifyClient(
                {
                    'track:"Signal Path" artist:"Main Artist" album:"Source Album"': (
                        candidate,
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            report_text = report_path.read_text(encoding="utf-8")

        cache_record = cache_payload["matches"][cache_key]
        expected_reason = (
            "track matched after removing Spotify featured credit from track title; "
            "source and featured artists matched; album differed"
        )
        self.assertEqual(summary.cache_hit_count, 0)
        self.assertEqual(summary.search_count, 1)
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(summary.would_add_count, 1)
        self.assertEqual(cache_record["matcher_version"], MATCHER_VERSION)
        self.assertEqual(cache_record["spotify_uri"], "spotify:track:signal-path")
        self.assertEqual(cache_record["spotify_artist_names"], ["Main Artist", "Guest One"])
        self.assertEqual(cache_record["match_reason"], expected_reason)
        self.assertIn(expected_reason, report_text)

    def test_publish_ladder_uses_featured_credit_base_title_fallback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            row = playlist_row(
                "37672899",
                "Carl Gari",
                "Swim feat. Polygonia",
                "Carl Gari",
            )
            row["Track Number"] = "2"
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [row],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            report_path = directory / "reports" / "spotify-report.txt"
            client = PublishingSpotifyClient(
                {
                    'track:"Swim" artist:"Carl Gari" album:"Carl Gari"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:swim",
                            name="Swim",
                            artists=("Carl Gari", "Polygonia"),
                            album_name="Carl Gari",
                        ),
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(
            client.searches,
            [
                (
                    "access-token",
                    'track:"Swim feat. Polygonia" artist:"Carl Gari" album:"Carl Gari"',
                    10,
                ),
                ("access-token", 'track:"Swim feat. Polygonia" artist:"Carl Gari"', 10),
                ("access-token", "Carl Gari Swim feat. Polygonia", 10),
                ("access-token", 'track:"Swim" artist:"Carl Gari" album:"Carl Gari"', 10),
            ],
        )
        self.assertEqual(summary.search_count, 4)
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(summary.would_add_count, 1)

    def test_publish_ladder_uses_bracketed_featured_credit_base_title_fallback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            row = playlist_row(
                "222",
                "Test Album",
                "((( soft pressure ))) [feat. Guest Three]",
                "Main Artist",
            )
            row["Track Number"] = "6"
            write_playlist_master(
                playlist_directory / "Experimental" / "Experimental.csv",
                [row],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            report_path = directory / "reports" / "spotify-report.txt"
            client = PublishingSpotifyClient(
                {
                    'track:"((( soft pressure )))" artist:"Main Artist" album:"Test Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:soft-pressure",
                            name="((( soft pressure )))",
                            artists=("Main Artist", "Guest Three"),
                            album_name="Test Album",
                        ),
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(summary.search_count, 4)
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(summary.would_add_count, 1)
        self.assertEqual(
            client.searches[-1],
            (
                "access-token",
                'track:"((( soft pressure )))" artist:"Main Artist" album:"Test Album"',
                10,
            ),
        )

    def test_publish_ladder_retries_cached_unmatched_and_accepts_leading_in_title_variant(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            row = playlist_row(
                "29905411",
                "Environment Control",
                "Dark Territory",
                "Polar Inertia",
            )
            row["Track Number"] = "3"
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [row],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": {
                            "29905411|3|polar inertia|environment control|dark territory": {
                                "release_id": "29905411",
                                "track_number": "3",
                                "artist_name": "Polar Inertia",
                                "album_name": "Environment Control",
                                "track_name": "Dark Territory",
                                "search_query": (
                                    'track:"Dark Territory" artist:"Polar Inertia" '
                                    'album:"Environment Control"'
                                ),
                                "spotify_uri": "",
                                "spotify_url": "",
                                "match_status": "unmatched",
                                "match_reason": "no candidates matched track, artist, and album",
                                "matcher_version": 4,
                                "searched_at": "2026-07-10T00:00:00Z",
                                "last_seen_at": "2026-07-10T00:00:00Z",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            report_path = directory / "reports" / "spotify-report.txt"
            candidate = SpotifyTrackCandidate(
                uri="spotify:track:dark-territory",
                name="In Dark Territory",
                artists=("Polar Inertia",),
                album_name="Environment Control",
            )
            client = PublishingSpotifyClient(
                {
                    'track:"Dark Territory" artist:"Polar Inertia" album:"Environment Control"': (
                        candidate,
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(
            client.searches,
            [
                (
                    "access-token",
                    'track:"Dark Territory" artist:"Polar Inertia" album:"Environment Control"',
                    10,
                ),
                ("access-token", 'track:"Dark Territory" artist:"Polar Inertia"', 10),
                ("access-token", "Polar Inertia Dark Territory", 10),
                ("access-token", "Polar Inertia Dark Territory Environment Control", 10),
            ],
        )
        cache_record = cache_payload["matches"][
            "29905411|3|polar inertia|environment control|dark territory"
        ]
        self.assertEqual(summary.cache_hit_count, 0)
        self.assertEqual(summary.search_count, 4)
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(summary.would_add_count, 1)
        self.assertEqual(cache_record["match_status"], "matched")
        self.assertEqual(cache_record["matcher_version"], MATCHER_VERSION)
        self.assertEqual(
            cache_record["match_reason"],
            "track title differed only by Spotify's leading 'in'; artist and album matched",
        )
        self.assertIn(
            "track title differed only by Spotify's leading 'in'; artist and album matched",
            report_text,
        )

    def test_publish_ladder_matches_original_mix_title_without_annotation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            row = playlist_row(
                "37196016",
                "A Thousand Faces",
                "Skull Shrine (Original Mix)",
                "Feral",
            )
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [row],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            report_path = directory / "reports" / "spotify-report.txt"
            client = PublishingSpotifyClient(
                {
                    'track:"Skull Shrine" artist:"Feral"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:skull-shrine",
                            name="Skull Shrine",
                            artists=("Feral",),
                            album_name="A Thousand Faces EP",
                        ),
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(
            client.searches,
            [
                (
                    "access-token",
                    'track:"Skull Shrine (Original Mix)" artist:"Feral" album:"A Thousand Faces"',
                    10,
                ),
                ("access-token", 'track:"Skull Shrine (Original Mix)" artist:"Feral"', 10),
                ("access-token", "Feral Skull Shrine (Original Mix)", 10),
                ("access-token", 'track:"Skull Shrine" artist:"Feral" album:"A Thousand Faces"', 10),
                ("access-token", 'track:"Skull Shrine" artist:"Feral"', 10),
            ],
        )
        self.assertEqual(summary.search_count, 5)
        self.assertEqual(summary.matched_count, 1)
        self.assertEqual(summary.would_add_count, 1)
        self.assertIn(
            "track matched after removing source Original Mix annotation; artist matched; album differed",
            report_text,
        )
        self.assertIn('search query: track:"Skull Shrine" artist:"Feral"', report_text)

    def test_search_budget_finishes_started_ladder_then_stops_before_next_uncached_row(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [
                    playlist_row("111", "Discogs EP", "Alpha One", "Alpha Artist"),
                    playlist_row("222", "Beta Album", "Beta One", "Beta Artist"),
                ],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            report_path = directory / "reports" / "spotify-report.txt"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Discogs EP"': (),
                    'track:"Alpha One" artist:"Alpha Artist"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Spotify Single",
                        ),
                    ),
                    'track:"Beta One" artist:"Beta Artist" album:"Beta Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:beta",
                            name="Beta One",
                            artists=("Beta Artist",),
                            album_name="Beta Album",
                        ),
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
                max_new_searches_per_run=1,
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(summary.run_status, "partial - new Spotify search budget reached after 2 searches")
        self.assertEqual(summary.track_count, 1)
        self.assertEqual(summary.search_count, 2)
        self.assertEqual(summary.searched_row_count, 1)
        self.assertEqual(
            client.searches,
            [
                ("access-token", 'track:"Alpha One" artist:"Alpha Artist" album:"Discogs EP"', 10),
                ("access-token", 'track:"Alpha One" artist:"Alpha Artist"', 10),
            ],
        )
        self.assertIn("111|1|alpha artist|discogs ep|alpha one", cache_payload["matches"])
        self.assertNotIn("222|1|beta artist|beta album|beta one", cache_payload["matches"])

    def test_stale_unmatched_cache_records_are_researched_without_refresh_flag(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": {
                            "111|1|alpha artist|alpha album|alpha one": {
                                "release_id": "111",
                                "track_number": "1",
                                "artist_name": "Alpha Artist",
                                "album_name": "Alpha Album",
                                "track_name": "Alpha One",
                                "search_query": 'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
                                "spotify_uri": "",
                                "spotify_url": "",
                                "match_status": "unmatched",
                                "match_reason": "no candidates matched track, artist, and album",
                                "matcher_version": 5,
                                "searched_at": "2026-06-18T00:00:00Z",
                                "last_seen_at": "2026-06-18T00:00:00Z",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            report_path = directory / "reports" / "spotify-report.txt"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    )
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(client.searches, [('access-token', 'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"', 10)])
        self.assertEqual(summary.cache_hit_count, 0)
        self.assertEqual(summary.search_count, 1)
        self.assertEqual(summary.would_add_count, 1)
        self.assertEqual(cache_payload["matches"]["111|1|alpha artist|alpha album|alpha one"]["match_status"], "matched")

    def test_query_length_error_continues_to_shorter_ladder_query(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            long_album_name = " ".join(["Very Long Album Name"] * 20)
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", long_album_name, "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            candidate = SpotifyTrackCandidate(
                uri="spotify:track:alpha",
                name="Alpha One",
                artists=("Alpha Artist",),
                album_name="Spotify Single",
            )
            client = QueryLengthThenMatchSpotifyClient(
                matching_query='track:"Alpha One" artist:"Alpha Artist"',
                candidate=candidate,
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(len(client.searches), 2)
        self.assertEqual(summary.search_count, 2)
        self.assertEqual(summary.error_count, 0)
        self.assertEqual(summary.would_add_count, 1)

    def test_query_length_error_with_empty_shorter_query_is_unmatched_not_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            long_album_name = " ".join(["Very Long Album Name"] * 20)
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", long_album_name, "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = QueryLengthThenEmptySpotifyClient(
                matching_query='track:"Alpha One" artist:"Alpha Artist"',
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(len(client.searches), 4)
        self.assertEqual(summary.search_count, 4)
        self.assertEqual(summary.error_count, 0)
        self.assertEqual(summary.unmatched_count, 1)

    def test_publish_report_includes_ladder_queries_and_candidate_comparison_for_unmatched_tracks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:wrong",
                            name="Wrong Track",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    ),
                    'track:"Alpha One" artist:"Alpha Artist"': (),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(summary.unmatched_count, 1)
        self.assertIn("Search queries:", report_text)
        self.assertIn('    - track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"', report_text)
        self.assertIn('    - track:"Alpha One" artist:"Alpha Artist"', report_text)
        self.assertIn("Spotify returned 1 candidate(s).", report_text)
        self.assertIn("Best diagnostic candidate: spotify:track:wrong | Alpha Artist | Wrong Track | Alpha Album", report_text)
        self.assertIn("Track name: different", report_text)

    def test_publish_writes_partial_rate_limit_report_then_complete_report_on_rerun(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [
                    playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist"),
                    playlist_row("222", "Beta Album", "Beta One", "Beta Artist"),
                ],
            )
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            report_path = directory / "reports" / "spotify-report.txt"
            candidates_by_query = {
                'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                    SpotifyTrackCandidate(
                        uri="spotify:track:alpha",
                        name="Alpha One",
                        artists=("Alpha Artist",),
                        album_name="Alpha Album",
                    ),
                )
            }
            first_client = DeferredSecondSearchPublishingClient(candidates_by_query)

            with self.assertRaisesRegex(SpotifyRateLimitDeferredError, "Retry-After is 2 hours 46 minutes 39 seconds"):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=first_client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    apply=False,
                    publisher_sync_mode="append",
                )
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            partial_report_text = report_path.read_text(encoding="utf-8")
            second_client = DeferredSecondSearchPublishingClient(candidates_by_query)

            with self.assertRaisesRegex(SpotifyRateLimitDeferredError, "Retry-After is 2 hours 46 minutes 39 seconds"):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=second_client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    apply=False,
                    publisher_sync_mode="append",
                )
            successful_client = PublishingSpotifyClient(
                {
                    'track:"Beta One" artist:"Beta Artist" album:"Beta Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:beta",
                            name="Beta One",
                            artists=("Beta Artist",),
                            album_name="Beta Album",
                        ),
                    )
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=successful_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            complete_report_text = report_path.read_text(encoding="utf-8")

        self.assertIn("111|1|alpha artist|alpha album|alpha one", cache_payload["matches"])
        self.assertIn("Run status: aborted - Spotify rate limit stopped the run after 1 of 2 tracks", partial_report_text)
        self.assertIn("no playlist writes were attempted", partial_report_text)
        self.assertIn("Tracks: 1", partial_report_text)
        self.assertIn("Spotify searches: 1", partial_report_text)
        self.assertIn("Discogs - House | 111 | 1 | Alpha Artist | Alpha One | would_add | spotify:track:alpha", partial_report_text)
        self.assertNotIn("Beta Artist | Beta One | would_add", partial_report_text)
        self.assertEqual(
            second_client.searches,
            [('access-token', 'track:"Beta One" artist:"Beta Artist" album:"Beta Album"', 10)],
        )
        self.assertEqual(summary.track_count, 2)
        self.assertEqual(summary.cache_hit_count, 1)
        self.assertEqual(summary.search_count, 1)
        self.assertIn("Run status: complete", complete_report_text)
        self.assertIn("Tracks: 2", complete_report_text)
        self.assertIn("Cache hits: 1", complete_report_text)
        self.assertIn("Spotify searches: 1", complete_report_text)
        self.assertIn("Discogs - House | 111 | 1 | Alpha Artist | Alpha One | would_add | spotify:track:alpha", complete_report_text)
        self.assertIn("Discogs - House | 222 | 1 | Beta Artist | Beta One | would_add | spotify:track:beta", complete_report_text)

    def test_append_skips_existing_track_identity_even_if_spotify_uri_changed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Ambient" / "Ambient.csv",
                [playlist_row("30887115", "Revelations", "Reprobation", "Cauê")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    'track:"Reprobation" artist:"Cauê" album:"Revelations"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:new",
                            name="Reprobation",
                            artists=("Cauê",),
                            album_name="Revelations",
                        ),
                    )
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-ambient",
                        name="Discogs - Ambient",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
                playlist_items_by_id={
                    "playlist-ambient": (
                        SpotifyPlaylistItem(
                            uri="spotify:track:old",
                            name="Reprobation",
                            artists=("Cauê",),
                            album_name="Revelations",
                        ),
                    )
                },
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(summary.already_present_count, 1)
        self.assertEqual(summary.would_add_count, 0)
        self.assertEqual(client.add_calls, [])
        self.assertIn("Spotify artist, album, and track already exist in playlist", report_text)

    def test_append_skips_existing_uri_when_spotify_item_metadata_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    )
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
                playlist_items_by_id={
                    "playlist-house": (
                        SpotifyPlaylistItem(uri="spotify:track:alpha", name="", artists=(), album_name=""),
                    )
                },
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(summary.already_present_count, 1)
        self.assertEqual(summary.would_add_count, 0)
        self.assertEqual(client.add_calls, [])
        self.assertIn("Spotify URI already exists in playlist with incomplete metadata", report_text)

    def test_append_rerun_after_partial_rate_limit_adds_only_missing_tracks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row(str(index), f"Album {index}", f"Track {index}", f"Artist {index}")
                for index in range(1, 151)
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = FailingFirstAppendBatchClient(
                {
                    f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                        matching_candidate(row),
                    )
                    for row in rows
                },
                fail_playlist_id="playlist-house",
            )

            with self.assertRaisesRegex(SpotifyApiError, "status 429"):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    publisher_sync_mode="append",
                    max_new_searches_per_run=0,
                )
            searches_after_failed_run = len(client.searches)

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="append",
                max_new_searches_per_run=0,
            )

        final_items = client.playlist_items_by_id["playlist-house"]
        final_identities = {
            (item.artists, item.album_name, item.name)
            for item in final_items
        }
        self.assertEqual([len(batch) for _, batch, _ in client.add_calls], [100, 50])
        self.assertEqual(summary.already_present_count, 100)
        self.assertEqual(summary.added_count, 50)
        self.assertEqual(len(final_items), 150)
        self.assertEqual(len(final_identities), 150)
        self.assertEqual(len(client.searches) - searches_after_failed_run, 50)

    def test_append_default_search_budget_writes_checkpoint_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row(str(index), f"Album {index}", f"Track {index}", f"Artist {index}")
                for index in range(1, 502)
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                        matching_candidate(row),
                    )
                    for row in rows
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="append",
            )
            report_text = report_path.read_text(encoding="utf-8")
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(summary.run_status, "partial - new Spotify search budget reached after 500 searches")
        self.assertEqual(summary.track_count, 500)
        self.assertEqual(summary.search_count, 500)
        self.assertEqual(summary.added_count, 500)
        self.assertEqual(summary.would_add_count, 0)
        self.assertEqual(len(client.searches), 500)
        self.assertEqual(client.created_playlists, [("Discogs - House", False, "Generated from Discogs collection")])
        self.assertEqual([len(batch) for _, batch, _ in client.add_calls], [100, 100, 100, 100, 100])
        self.assertIn("Run status: partial - new Spotify search budget reached after 500 searches", report_text)
        self.assertIn("Tracks added", report_text)
        self.assertIn("500|1|artist 500|album 500|track 500", cache_payload["matches"])
        self.assertNotIn("501|1|artist 501|album 501|track 501", cache_payload["matches"])

    def test_append_budget_rerun_adds_only_missing_tracks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row(str(index), f"Album {index}", f"Track {index}", f"Artist {index}")
                for index in range(1, 551)
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                        matching_candidate(row),
                    )
                    for row in rows
                }
            )

            first_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="append",
            )
            searches_after_first_run = len(client.searches)
            second_summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="append",
            )

        final_items = client.playlist_items_by_id["created-1"]
        final_identities = {
            (item.artists, item.album_name, item.name)
            for item in final_items
        }
        self.assertEqual(first_summary.added_count, 500)
        self.assertEqual(second_summary.cache_hit_count, 500)
        self.assertEqual(second_summary.search_count, 50)
        self.assertEqual(second_summary.already_present_count, 500)
        self.assertEqual(second_summary.added_count, 50)
        self.assertEqual(len(client.searches) - searches_after_first_run, 50)
        self.assertEqual([len(batch) for _, batch, _ in client.add_calls], [100, 100, 100, 100, 100, 50])
        self.assertEqual(len(final_items), 550)
        self.assertEqual(len(final_identities), 550)

    def test_append_rate_limit_stop_after_checkpoint_keeps_written_tracks_and_no_error_cache(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row(str(index), f"Album {index}", f"Track {index}", f"Artist {index}")
                for index in range(1, 102)
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = ExhaustedRateLimitAfterCheckpointPublishingClient(
                {
                    f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                        matching_candidate(row),
                    )
                    for row in rows
                }
            )

            with self.assertRaisesRegex(SpotifyRateLimitRetriesExhaustedError, "Spotify rate limit retries were exhausted"):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    publisher_sync_mode="append",
                    max_new_searches_per_run=0,
                )
            report_text = report_path.read_text(encoding="utf-8")
            cache_payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        self.assertEqual([len(batch) for _, batch, _ in client.add_calls], [100])
        self.assertEqual(len(client.playlist_items_by_id["created-1"]), 100)
        self.assertIn("Run status: aborted - Spotify rate limit stopped the run after 100 of 101 tracks", report_text)
        self.assertIn("append checkpoints completed before the rate limit stop were written", report_text)
        self.assertIn("Search errors: 0", report_text)
        self.assertIn("100|1|artist 100|album 100|track 100", cache_payload["matches"])
        self.assertNotIn("101|1|artist 101|album 101|track 101", cache_payload["matches"])

    def test_append_checkpoint_report_does_not_claim_complete_before_run_finishes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row(str(index), f"Album {index}", f"Track {index}", f"Artist {index}")
                for index in range(1, 102)
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = InterruptedAfterCheckpointPublishingClient(
                {
                    f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                        matching_candidate(row),
                    )
                    for row in rows
                }
            )

            with self.assertRaisesRegex(RuntimeError, "interrupted after checkpoint"):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    publisher_sync_mode="append",
                    max_new_searches_per_run=0,
                )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual([len(batch) for _, batch, _ in client.add_calls], [100])
        self.assertIn("Run status: failed - publisher error after 100 of 101 tracks", report_text)
        self.assertIn("Added tracks: 100", report_text)
        self.assertIn("pending writes were not sent", report_text)
        self.assertNotIn("Run status: complete", report_text)

    def test_replace_mode_exact_search_budget_writes_complete_replacement(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row(str(index), f"Album {index}", f"Track {index}", f"Artist {index}")
                for index in range(1, 101)
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                        matching_candidate(row),
                    )
                    for row in rows
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="replace",
            )

        self.assertEqual(summary.run_status, "complete")
        self.assertEqual(summary.search_count, 100)
        self.assertEqual(summary.included_count, 100)
        self.assertEqual(summary.would_include_count, 0)
        self.assertEqual(len(client.replace_calls), 1)
        self.assertEqual(len(client.replace_calls[0][1]), 100)

    def test_replace_mode_preserves_repeated_rows_while_append_still_dedupes(self):
        rows = [
            playlist_row("111", "Shared Album", "Shared Track", "Shared Artist"),
            playlist_row("222", "Shared Album", "Shared Track", "Shared Artist"),
        ]
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:shared",
            name="Shared Track",
            artists=("Shared Artist",),
            album_name="Shared Album",
        )
        query = 'track:"Shared Track" artist:"Shared Artist" album:"Shared Album"'

        for publisher_sync_mode, expected_statuses in (
            ("replace", ["would_include", "would_include"]),
            ("append", ["would_add", "duplicate_in_source"]),
        ):
            with self.subTest(publisher_sync_mode=publisher_sync_mode):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    directory = Path(temporary_directory)
                    playlist_directory = directory / "collection" / "playlists"
                    write_playlist_master(playlist_directory / "House" / "House.csv", rows)
                    client = PublishingSpotifyClient({query: (candidate,)})

                    summary = publish_spotify_playlists(
                        playlist_output_directory=playlist_directory,
                        report_path=directory / "reports" / "spotify-report.txt",
                        spotify_client=client,
                        access_token="access-token",
                        match_cache_path=directory / "collection" / "cache" / "matches.json",
                        publisher_config=PublisherConfig(
                            default_publisher="spotify",
                            playlist_prefix="Discogs - ",
                            playlist_suffix="",
                        ),
                        apply=False,
                        publisher_sync_mode=publisher_sync_mode,
                    )

                self.assertEqual([decision.status for decision in summary.decisions], expected_statuses)
                self.assertEqual(client.add_calls, [])
                self.assertEqual(client.replace_calls, [])
                if publisher_sync_mode == "replace":
                    self.assertEqual(summary.would_include_count, 2)
                    self.assertEqual(summary.duplicate_in_source_count, 0)
                    self.assertEqual(
                        [item.spotify_uri for item in summary.final_items],
                        ["spotify:track:shared", "spotify:track:shared"],
                    )
                else:
                    self.assertEqual(summary.would_add_count, 1)
                    self.assertEqual(summary.duplicate_in_source_count, 1)

    def test_replace_mode_preserves_repeated_rows_across_write_batch_boundary(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row(str(index), f"Album {index}", f"Track {index}", f"Artist {index}")
                for index in range(1, 101)
            ]
            rows.append(playlist_row("101", "Album 100", "Track 100", "Artist 100"))
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            candidates_by_query = {
                f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                    matching_candidate(row),
                )
                for row in rows
            }
            client = PublishingSpotifyClient(
                candidates_by_query,
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=directory / "collection" / "cache" / "matches.json",
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="replace",
            )

        expected_uris = tuple(f"spotify:track:{index}" for index in range(1, 100)) + (
            "spotify:track:101",
            "spotify:track:101",
        )
        self.assertEqual(summary.included_count, 101)
        self.assertEqual(summary.duplicate_in_source_count, 0)
        self.assertEqual(client.replace_calls, [("playlist-house", expected_uris[:100])])
        self.assertEqual(client.add_calls, [("playlist-house", expected_uris[100:], None)])

    def test_replace_mode_preserves_repeated_cached_rows_without_searching(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row("111", "Shared Album", "Shared Track", "Shared Artist"),
                playlist_row("222", "Shared Album", "Shared Track", "Shared Artist"),
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            query = 'track:"Shared Track" artist:"Shared Artist" album:"Shared Album"'
            candidate = SpotifyTrackCandidate(
                uri="spotify:track:shared",
                name="Shared Track",
                artists=("Shared Artist",),
                album_name="Shared Album",
            )
            match_cache_path = directory / "collection" / "cache" / "matches.json"
            publisher_config = PublisherConfig(
                default_publisher="spotify",
                playlist_prefix="Discogs - ",
                playlist_suffix="",
            )
            publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "first-report.txt",
                spotify_client=PublishingSpotifyClient({query: (candidate,)}),
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=publisher_config,
                apply=False,
                publisher_sync_mode="replace",
            )
            cached_client = PublishingSpotifyClient({})

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "cached-report.txt",
                spotify_client=cached_client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=publisher_config,
                apply=False,
                publisher_sync_mode="replace",
            )

        self.assertEqual(summary.cache_hit_count, 2)
        self.assertEqual(summary.search_count, 0)
        self.assertEqual(summary.would_include_count, 2)
        self.assertEqual(summary.duplicate_in_source_count, 0)
        self.assertEqual(cached_client.searches, [])

    def test_replace_mode_search_budget_stops_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row(str(index), f"Album {index}", f"Track {index}", f"Artist {index}")
                for index in range(1, 502)
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                        matching_candidate(row),
                    )
                    for row in rows
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="replace",
            )

        self.assertEqual(summary.run_status, "partial - new Spotify search budget reached after 500 searches")
        self.assertEqual(summary.search_count, 500)
        self.assertEqual(summary.would_include_count, 500)
        self.assertEqual(client.replace_calls, [])
        self.assertEqual(client.add_calls, [])

    def test_replace_mode_clears_existing_playlist_for_selected_header_only_master(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists" / "release-playlists"
            master_path = playlist_directory / "Empty Mirror" / "Empty Mirror.csv"
            write_playlist_master(master_path, [])
            client = PublishingSpotifyClient(
                {},
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-empty",
                        name="Release - Empty Mirror",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
                playlist_items_by_id={
                    "playlist-empty": (
                        SpotifyPlaylistItem(
                            uri="spotify:track:old",
                            name="Old Track",
                            artists=("Old Artist",),
                            album_name="Old Album",
                        ),
                    )
                },
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=directory / "collection" / "cache" / "matches.json",
                playlist_master_paths=(master_path,),
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Release - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="replace",
            )

        self.assertEqual(summary.run_status, "complete")
        self.assertEqual(client.replace_calls, [("playlist-empty", ())])
        self.assertEqual(client.created_playlists, [])

    def test_replace_mode_does_not_create_missing_playlist_for_selected_header_only_master(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists" / "release-playlists"
            master_path = playlist_directory / "Empty Mirror" / "Empty Mirror.csv"
            write_playlist_master(master_path, [])
            client = PublishingSpotifyClient({})

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=directory / "reports" / "spotify-report.txt",
                spotify_client=client,
                access_token="access-token",
                match_cache_path=directory / "collection" / "cache" / "matches.json",
                playlist_master_paths=(master_path,),
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Release - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="replace",
            )

        self.assertEqual(summary.run_status, "complete")
        self.assertEqual(client.created_playlists, [])
        self.assertEqual(client.replace_calls, [])

    def test_replace_mode_unmatched_row_in_later_selected_playlist_blocks_all_writes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists" / "release-playlists"
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "matches.json"
            first_master_path = playlist_directory / "First Picks" / "First Picks.csv"
            second_master_path = playlist_directory / "Second Picks" / "Second Picks.csv"
            first_row = playlist_row("111", "First Album", "First Track", "First Artist")
            second_row = playlist_row("222", "Second Album", "Missing Track", "Second Artist")
            write_playlist_master(first_master_path, [first_row])
            write_playlist_master(second_master_path, [second_row])
            client = PublishingSpotifyClient(
                {
                    'track:"First Track" artist:"First Artist" album:"First Album"': (
                        matching_candidate(first_row),
                    ),
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-first",
                        name="Release - First Picks",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                    SpotifyPlaylist(
                        playlist_id="playlist-second",
                        name="Release - Second Picks",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
            )

            with self.assertRaisesRegex(ValueError, "replace mode aborted before writing"):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    playlist_master_paths=(first_master_path, second_master_path),
                    playlist_names_by_master_path={
                        first_master_path: "First Picks",
                        second_master_path: "Second Picks",
                    },
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Release - ",
                        playlist_suffix="",
                    ),
                    publisher_sync_mode="replace",
                )
            report_text = report_path.read_text(encoding="utf-8")
            match_cache_exists = match_cache_path.is_file()

        self.assertEqual(client.replace_calls, [])
        self.assertEqual(client.add_calls, [])
        self.assertEqual(client.created_playlists, [])
        self.assertTrue(match_cache_exists)
        self.assertIn(
            "Run status: replace mode aborted before writing because not every source row resolved "
            "to a publishable Spotify URI (unmatched=1)",
            report_text,
        )
        self.assertNotIn("Run status: complete", report_text)

    def test_replace_mode_blocking_dry_run_reports_complete_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [
                    playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist"),
                    playlist_row("222", "Beta Album", "Missing Track", "Beta Artist"),
                ],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    ),
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=directory / "collection" / "cache" / "matches.json",
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="replace",
            )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(summary.run_status, "complete")
        self.assertEqual(summary.would_include_count, 1)
        self.assertEqual(summary.unmatched_count, 1)
        self.assertEqual(client.replace_calls, [])
        self.assertEqual(client.add_calls, [])
        self.assertIn("Run status: complete", report_text)

    def test_append_allows_same_artist_and_track_on_different_albums(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Ambient" / "Ambient.csv",
                [
                    playlist_row("111", "First Album", "Shared Track", "Shared Artist"),
                    playlist_row("222", "Second Album", "Shared Track", "Shared Artist"),
                ],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    'track:"Shared Track" artist:"Shared Artist" album:"First Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:first",
                            name="Shared Track",
                            artists=("Shared Artist",),
                            album_name="First Album",
                        ),
                    ),
                    'track:"Shared Track" artist:"Shared Artist" album:"Second Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:second",
                            name="Shared Track",
                            artists=("Shared Artist",),
                            album_name="Second Album",
                        ),
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual([decision.status for decision in summary.decisions], ["would_add", "would_add"])
        self.assertEqual(summary.duplicate_in_source_count, 0)
        self.assertEqual(summary.would_add_count, 2)

    def test_append_allows_same_spotify_uri_on_different_albums(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Ambient" / "Ambient.csv",
                [
                    playlist_row("111", "First Album", "Shared Track", "Shared Artist"),
                    playlist_row("222", "Second Album", "Shared Track", "Shared Artist"),
                ],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    'track:"Shared Track" artist:"Shared Artist" album:"First Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:shared",
                            name="Shared Track",
                            artists=("Shared Artist",),
                            album_name="First Album",
                        ),
                    ),
                    'track:"Shared Track" artist:"Shared Artist" album:"Second Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:shared",
                            name="Shared Track",
                            artists=("Shared Artist",),
                            album_name="Second Album",
                        ),
                    ),
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual([decision.status for decision in summary.decisions], ["would_add", "would_add"])
        self.assertEqual(summary.duplicate_in_source_count, 0)
        self.assertEqual(summary.would_add_count, 2)

    def test_publish_defaults_to_apply_and_reports_added_tracks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    )
                }
            )
            info_lines = []

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="append",
                info_log=info_lines.append,
            )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(summary.added_count, 1)
        self.assertEqual(summary.would_add_count, 0)
        self.assertEqual(client.created_playlists, [("Discogs - House", False, "Generated from Discogs collection")])
        self.assertEqual(client.add_calls, [("created-1", ("spotify:track:alpha",), None)])
        self.assertEqual(client.replace_calls, [])
        self.assertIn("Playlist Discogs - House does not exist, creating", info_lines)
        self.assertIn("Spotify playlist publish report", report_text)
        self.assertIn("Spotify playlist publish report\n===============================", report_text)
        self.assertIn("Added tracks: 1", report_text)
        self.assertIn("Tracks added", report_text)
        self.assertIn("Discogs - House | 111 | 1 | Alpha Artist | Alpha One | added | spotify:track:alpha", report_text)

    def test_publish_uses_playlist_name_override_for_exact_master_path_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists" / "on-the-fly"
            playlist_path = playlist_directory / "Friday_Picks" / "Friday_Picks.csv"
            row = playlist_row("111", "House Album", "House One", "House Artist")
            write_playlist_master(playlist_path, [row])
            report_path = directory / "reports" / "spotify-publish.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    'track:"House One" artist:"House Artist" album:"House Album"': (
                        matching_candidate(row),
                    )
                }
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                playlist_master_paths=(playlist_path,),
                playlist_names_by_master_path={playlist_path: "Friday/Picks"},
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="append",
            )

        self.assertEqual(summary.playlist_contexts[0].playlist_name, "Friday/Picks")
        self.assertEqual(summary.playlist_contexts[0].target_playlist_name, "Friday/Picks")
        self.assertEqual(summary.decisions[0].playlist_name, "Friday/Picks")
        self.assertEqual(summary.decisions[0].target_playlist_name, "Friday/Picks")
        self.assertEqual(client.created_playlists, [])

    def test_publish_uses_empty_affixes_when_config_is_omitted(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    )
                }
            )

            publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_sync_mode="append",
            )

        self.assertEqual(client.created_playlists, [("House", False, "Generated from Discogs collection")])

    def test_publish_failure_after_partial_write_leaves_durable_report(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                [playlist_row("222", "Beta Album", "Beta One", "Beta Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = FailingSecondPlaylistPublishClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    ),
                    'track:"Beta One" artist:"Beta Artist" album:"Beta Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:beta",
                            name="Beta One",
                            artists=("Beta Artist",),
                            album_name="Beta Album",
                        ),
                    ),
                },
                playlists=(
                    SpotifyPlaylist(playlist_id="playlist-house", name="Discogs - House", url="", owner_id="current-user", public=False),
                    SpotifyPlaylist(playlist_id="playlist-techno", name="Discogs - Techno", url="", owner_id="current-user", public=False),
                ),
            )

            with self.assertRaisesRegex(SpotifyApiError, "temporary failure"):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    publisher_sync_mode="append",
                )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(client.add_calls, [("playlist-house", ("spotify:track:alpha",), None)])
        self.assertIn("Run status: failed - publishing stopped while writing Discogs - Techno", report_text)
        self.assertNotIn("Run status: complete", report_text)
        self.assertIn("Discogs - House | 111 | 1 | Alpha Artist | Alpha One | added | spotify:track:alpha", report_text)
        self.assertIn("Discogs - Techno | 222 | 1 | Beta Artist | Beta One | would_add | spotify:track:beta", report_text)
        self.assertIn("publishing failed: playlist append stopped", report_text)
        self.assertIn("remote outcome may be uncertain", report_text)

    def test_publish_replace_dry_run_reports_replaced_final_state_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    )
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="https://open.spotify.com/playlist/playlist-house",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
                playlist_items_by_id={
                    "playlist-house": (
                        SpotifyPlaylistItem(uri="spotify:track:old-1", name="Old One", artists=("Old Artist",), album_name="Old Album"),
                        SpotifyPlaylistItem(uri="spotify:track:old-2", name="Old Two", artists=("Old Artist",), album_name="Old Album"),
                    )
                },
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                publisher_sync_mode="replace",
            )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(summary.would_include_count, 1)
        self.assertEqual(summary.would_add_count, 0)
        self.assertEqual(client.add_calls, [])
        self.assertEqual(client.replace_calls, [])
        self.assertIn("Publisher sync mode: replace", report_text)
        self.assertIn("Tracks that would be included in replacement: 1", report_text)
        self.assertIn("Discogs - House | 111 | 1 | Alpha Artist | Alpha One | would_include | spotify:track:alpha", report_text)
        self.assertIn("Final planned playlist state", report_text)
        self.assertIn("1 | would_include | Alpha Artist | Alpha One | Alpha Album | spotify:track:alpha", report_text)
        self.assertNotIn("Old One", report_text)

    def test_publish_replace_apply_aborts_before_write_when_search_errors_exist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [
                    playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist"),
                    playlist_row("222", "Beta Album", "Beta One", "Beta Artist"),
                ],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = SearchErrorPublishingClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    )
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
                playlist_items_by_id={
                    "playlist-house": (
                        SpotifyPlaylistItem(uri="spotify:track:old", name="Old One", artists=("Old Artist",), album_name="Old Album"),
                    )
                },
            )

            with self.assertRaisesRegex(ValueError, "replace mode aborted before writing"):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    publisher_sync_mode="replace",
                    max_new_searches_per_run=0,
                )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(client.replace_calls, [])
        self.assertEqual(client.add_calls, [])
        self.assertIn(
            "Run status: replace mode aborted before writing because not every source row resolved "
            "to a publishable Spotify URI (error=1)",
            report_text,
        )
        self.assertNotIn("Run status: complete", report_text)
        self.assertIn("Search errors: 1", report_text)
        self.assertIn("Discogs - House | 222 | 1 | Beta Artist | Beta One | error | no Spotify URI", report_text)

    def test_publish_replace_apply_reports_partial_write_if_remainder_append_fails(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            rows = [
                playlist_row(str(index), "Alpha Album", f"Alpha {index}", "Alpha Artist")
                for index in range(1, 102)
            ]
            write_playlist_master(playlist_directory / "House" / "House.csv", rows)
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = FailingReplaceRemainderPublishClient(
                {
                    f'track:"{row["Track Name"]}" artist:"{row["Artist Name"]}" album:"{row["Album Name"]}"': (
                        matching_candidate(row),
                    )
                    for row in rows
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
            )

            with self.assertRaisesRegex(ValueError, "partially replaced"):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    publisher_sync_mode="replace",
                    max_new_searches_per_run=0,
                )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(len(client.replace_calls), 1)
        self.assertEqual(len(client.replace_calls[0][1]), 100)
        self.assertEqual(client.add_calls, [])
        self.assertIn("publishing failed: replace mode partially replaced Discogs - House", report_text)

    def test_publish_ignores_followed_playlist_with_target_name_and_creates_owned_private_playlist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    )
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="followed-house",
                        name="Discogs - House",
                        url="",
                        owner_id="other-user",
                        public=False,
                    ),
                ),
                playlist_items_by_id={
                    "followed-house": (
                        SpotifyPlaylistItem(uri="spotify:track:old", name="Old One", artists=("Old Artist",), album_name="Old Album"),
                    )
                },
            )

            summary = publish_spotify_playlists(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                match_cache_path=match_cache_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                publisher_sync_mode="append",
            )

        self.assertEqual(summary.added_count, 1)
        self.assertEqual(client.created_playlists, [("Discogs - House", False, "Generated from Discogs collection")])
        self.assertEqual(client.add_calls, [("created-1", ("spotify:track:alpha",), None)])

    def test_publish_rejects_owned_public_playlist_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            client = PublishingSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    )
                },
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=True,
                    ),
                ),
            )

            with self.assertRaisesRegex(ValueError, "public Spotify playlist"):
                publish_spotify_playlists(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=client,
                    access_token="access-token",
                    match_cache_path=match_cache_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    publisher_sync_mode="append",
                )

        self.assertEqual(client.add_calls, [])
        self.assertEqual(client.replace_calls, [])

    def test_main_rejects_missing_playlist_before_authorizing_spotify(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            playlist_directory = Path(temporary_directory) / "collection" / "playlists"
            playlist_directory.mkdir(parents=True)
            stderr = io.StringIO()

            with (
                patch("publishers.spotify.publish_playlist.load_spotify_settings") as load_settings,
                patch("publishers.spotify.publish_playlist.get_spotify_access_token") as get_access_token,
                patch("sys.stderr", stderr),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                from publishers.spotify import publish_playlist

                exit_code = publish_playlist.main(
                    [
                        "--playlist-output-dir",
                        str(playlist_directory),
                        "--playlists",
                        "Missing",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("no playlist match found", stderr.getvalue())
        load_settings.assert_not_called()
        get_access_token.assert_not_called()

    def test_main_rejects_playlists_all_before_authorizing_spotify(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            playlist_directory = Path(temporary_directory) / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "House Album", "House One", "House Artist")],
            )
            stderr = io.StringIO()

            with (
                patch("publishers.spotify.publish_playlist.load_spotify_settings") as load_settings,
                patch("publishers.spotify.publish_playlist.get_spotify_access_token") as get_access_token,
                patch("sys.stderr", stderr),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                from publishers.spotify import publish_playlist

                exit_code = publish_playlist.main(
                    [
                        "--playlist-output-dir",
                        str(playlist_directory),
                        "--playlists",
                        "all",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("all", stderr.getvalue())
        self.assertIn("not allowed", stderr.getvalue())
        load_settings.assert_not_called()
        get_access_token.assert_not_called()

    def test_main_reports_os_errors_with_shared_cli_boundary(self):
        from publishers.spotify import publish_playlist

        stderr = io.StringIO()
        with (
            patch.object(publish_playlist, "run_spotify_publish_from_args", side_effect=OSError("disk full")),
            patch("sys.stderr", stderr),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            exit_code = publish_playlist.main(["--no-progress"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "Error: disk full\n")

    def test_main_uses_auto_session_for_default_publish(self):
        settings = SpotifySettings(
            client_id="client-id",
            redirect_uri="http://127.0.0.1:8765/callback",
        )
        token = SpotifyToken(
            access_token="session-access-token",
            refresh_token="refresh-token",
            expires_at=5000,
            scope="playlist-modify-private",
            token_type="Bearer",
        )
        with (
            patch("publishers.spotify.publish_playlist.load_spotify_settings", return_value=settings),
            patch("publishers.spotify.publish_playlist.get_spotify_access_token", return_value=token.access_token) as get_access_token,
            patch("publishers.spotify.publish_playlist.load_existing_token", return_value=token),
            patch("publishers.spotify.publish_playlist.publish_spotify_playlists") as publish,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            publish.return_value = publish_summary_stub()

            from publishers.spotify import publish_playlist

            exit_code = publish_playlist.main([])

        self.assertEqual(exit_code, 0)
        get_access_token.assert_called_once_with(
            settings=settings,
            required_scopes=DEFAULT_AUTHORIZE_SCOPES,
        )
        self.assertEqual(publish.call_args.kwargs["access_token"], "session-access-token")
        self.assertTrue(publish.call_args.kwargs["apply"])

    def test_main_uses_dry_run_when_requested(self):
        settings = SpotifySettings(
            client_id="client-id",
            redirect_uri="http://127.0.0.1:8765/callback",
        )
        token = SpotifyToken(
            access_token="session-access-token",
            refresh_token="refresh-token",
            expires_at=5000,
            scope="playlist-modify-private",
            token_type="Bearer",
        )
        with (
            patch("publishers.spotify.publish_playlist.load_spotify_settings", return_value=settings),
            patch("publishers.spotify.publish_playlist.get_spotify_access_token", return_value=token.access_token),
            patch("publishers.spotify.publish_playlist.load_existing_token", return_value=token),
            patch("publishers.spotify.publish_playlist.publish_spotify_playlists") as publish,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            publish.return_value = publish_summary_stub()

            from publishers.spotify import publish_playlist

            exit_code = publish_playlist.main(["--publishing-dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertFalse(publish.call_args.kwargs["apply"])

    def test_main_prints_summary_without_startup_step_header(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
            )
            token = SpotifyToken(
                access_token="session-access-token",
                refresh_token="refresh-token",
                expires_at=5000,
                scope="playlist-modify-private",
                token_type="Bearer",
            )

            with (
                patch("publishers.spotify.publish_playlist.load_spotify_settings", return_value=settings),
                patch("publishers.spotify.publish_playlist.get_spotify_access_token", return_value=token.access_token),
                patch("publishers.spotify.publish_playlist.load_existing_token", return_value=token),
                patch("publishers.spotify.publish_playlist.publish_spotify_playlists") as publish,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                publish.return_value = publish_summary_stub()

                from publishers.spotify import publish_playlist

                exit_code = publish_playlist.main(
                    [
                        "--playlist-output-dir",
                        str(playlist_directory),
                        "--playlists",
                        "Deep Techno",
                        "--no-progress",
                    ]
                )

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertNotIn("------------------------------------", output)
        self.assertNotIn("Running Spotify playlist publisher...", output)
        self.assertIn("Files\n-----\nReport:", output)
        self.assertIn("Processed\n---------", output)
        self.assertIn("Tracks: 0", output)

    def test_main_does_not_log_spotify_rate_limit_waits(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
            )
            token = SpotifyToken(
                access_token="session-access-token",
                refresh_token="refresh-token",
                expires_at=5000,
                scope="playlist-modify-private",
                token_type="Bearer",
            )
            stderr = io.StringIO()

            def publish_side_effect(**kwargs):
                self.assertIsNone(getattr(kwargs["spotify_client"], "rate_limit_callback", None))
                return publish_summary_stub(track_count=1)

            with (
                patch("publishers.spotify.publish_playlist.load_spotify_settings", return_value=settings),
                patch("publishers.spotify.publish_playlist.get_spotify_access_token", return_value=token.access_token),
                patch("publishers.spotify.publish_playlist.load_existing_token", return_value=token),
                patch("publishers.spotify.publish_playlist.publish_spotify_playlists", side_effect=publish_side_effect),
                patch("sys.stderr", stderr),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                from publishers.spotify import publish_playlist

                exit_code = publish_playlist.main(
                    [
                        "--playlist-output-dir",
                        str(playlist_directory),
                        "--playlists",
                        "Deep Techno",
                        "--no-progress",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertNotIn("rate limited search", stderr.getvalue())

    def test_main_prints_partial_report_path_for_deferred_rate_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
            )
            token = SpotifyToken(
                access_token="session-access-token",
                refresh_token="refresh-token",
                expires_at=5000,
                scope="playlist-modify-private",
                token_type="Bearer",
            )
            stderr = io.StringIO()

            def publish_side_effect(**kwargs):
                kwargs["report_path"].parent.mkdir(parents=True, exist_ok=True)
                kwargs["report_path"].write_text("partial report\n", encoding="utf-8")
                raise SpotifyRateLimitDeferredError(retry_after_seconds=9999, max_wait_seconds=480)

            with (
                patch("publishers.spotify.publish_playlist.load_spotify_settings", return_value=settings),
                patch("publishers.spotify.publish_playlist.get_spotify_access_token", return_value=token.access_token),
                patch("publishers.spotify.publish_playlist.load_existing_token", return_value=token),
                patch("publishers.spotify.publish_playlist.publish_spotify_playlists", side_effect=publish_side_effect),
                patch("sys.stderr", stderr),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                from publishers.spotify import publish_playlist

                exit_code = publish_playlist.main(
                    [
                        "--playlist-output-dir",
                        str(playlist_directory),
                        "--playlists",
                        "Deep Techno",
                        "--report",
                        str(report_path),
                        "--no-progress",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("Spotify rate limit Retry-After is 2 hours 46 minutes 39 seconds", stderr.getvalue())
        self.assertIn(f"Spotify publish report: {report_path}", stderr.getvalue())

    def test_main_prints_partial_report_path_for_exhausted_rate_limit_retries(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-report.txt"
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
            )
            token = SpotifyToken(
                access_token="session-access-token",
                refresh_token="refresh-token",
                expires_at=5000,
                scope="playlist-modify-private",
                token_type="Bearer",
            )
            stderr = io.StringIO()

            def publish_side_effect(**kwargs):
                kwargs["report_path"].parent.mkdir(parents=True, exist_ok=True)
                kwargs["report_path"].write_text("partial report\n", encoding="utf-8")
                raise SpotifyRateLimitRetriesExhaustedError(retry_after_seconds=1)

            with (
                patch("publishers.spotify.publish_playlist.load_spotify_settings", return_value=settings),
                patch("publishers.spotify.publish_playlist.get_spotify_access_token", return_value=token.access_token),
                patch("publishers.spotify.publish_playlist.load_existing_token", return_value=token),
                patch("publishers.spotify.publish_playlist.publish_spotify_playlists", side_effect=publish_side_effect),
                patch("sys.stderr", stderr),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                from publishers.spotify import publish_playlist

                exit_code = publish_playlist.main(
                    [
                        "--playlist-output-dir",
                        str(playlist_directory),
                        "--playlists",
                        "Deep Techno",
                        "--report",
                        str(report_path),
                        "--no-progress",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("Spotify rate limit retries were exhausted", stderr.getvalue())
        self.assertIn(f"Spotify publish report: {report_path}", stderr.getvalue())

    def test_main_writes_safe_debug_log_when_requested(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            debug_log_path = directory / "debug" / "spotify-debug.log"
            settings = SpotifySettings(
                client_id="client-id",
                redirect_uri="http://127.0.0.1:8765/callback",
            )
            token = SpotifyToken(
                access_token="session-access-token",
                refresh_token="refresh-token",
                expires_at=5000,
                scope="playlist-modify-private",
                token_type="Bearer",
            )

            def publish_side_effect(**kwargs):
                kwargs["spotify_client"].debug_log("spotify_search_response attempt=1 status=200 retry_after=(none)")
                return publish_summary_stub(track_count=1)

            with (
                patch("publishers.spotify.publish_playlist.load_spotify_settings", return_value=settings),
                patch("publishers.spotify.publish_playlist.get_spotify_access_token", return_value=token.access_token),
                patch("publishers.spotify.publish_playlist.load_existing_token", return_value=token),
                patch("publishers.spotify.publish_playlist.publish_spotify_playlists", side_effect=publish_side_effect),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                from publishers.spotify import publish_playlist

                exit_code = publish_playlist.main(
                    [
                        "--playlist-output-dir",
                        str(playlist_directory),
                        "--playlists",
                        "Deep Techno",
                        "--debug-log",
                        str(debug_log_path),
                        "--no-progress",
                    ]
                )

            self.assertEqual(exit_code, 0)
            debug_text = debug_log_path.read_text(encoding="utf-8")
            self.assertIn("start spotify_publish", debug_text)
            self.assertIn("resolved_playlist_masters count=1", debug_text)
            self.assertIn("spotify_search_response attempt=1 status=200 retry_after=(none)", debug_text)
            self.assertIn("completed track_count=1 matched=0 cache_hits=0 searches=0 already_present=0 would_add=0 added=0 ambiguous=0 unmatched=0 errors=0", debug_text)
            self.assertNotIn("Deep Techno", debug_text)
            self.assertNotIn("Alpha One", debug_text)
            self.assertNotIn("session-access-token", debug_text)

    def test_parse_args_enables_progress_by_default_and_can_disable_it(self):
        from publishers.spotify import publish_playlist

        default_args = publish_playlist.parse_args([])
        quiet_args = publish_playlist.parse_args(["--no-progress"])
        publishing_dry_run_args = publish_playlist.parse_args(["--publishing-dry-run"])

        self.assertTrue(default_args.progress)
        self.assertFalse(quiet_args.progress)
        self.assertIsNone(default_args.playlists)
        self.assertEqual(publish_playlist.parse_args(["--playlists", "House", "Techno"]).playlists, ["House", "Techno"])
        self.assertEqual(default_args.publisher_sync_mode, "append")
        self.assertEqual(publish_playlist.parse_args(["--publisher-sync-mode", "replace"]).publisher_sync_mode, "replace")
        self.assertEqual(default_args.max_new_searches_per_run, 500)
        self.assertEqual(
            publish_playlist.parse_args(["--max-new-searches-per-run", "0"]).max_new_searches_per_run,
            0,
        )
        self.assertEqual(
            publish_playlist.parse_args(["--max-new-searches-per-run", "250"]).max_new_searches_per_run,
            250,
        )
        self.assertFalse(default_args.refresh_match_cache)
        self.assertTrue(publish_playlist.parse_args(["--refresh-match-cache"]).refresh_match_cache)
        self.assertEqual(default_args.publish_state_cache, Path("collection/cache/spotify-publish-state.cache.json"))
        self.assertEqual(
            publish_playlist.parse_args(["--publish-state-cache", "custom-state.json"]).publish_state_cache,
            Path("custom-state.json"),
        )
        self.assertTrue(default_args.apply)
        self.assertFalse(default_args.dry_run)
        self.assertTrue(publishing_dry_run_args.dry_run)
        self.assertFalse(publishing_dry_run_args.apply)

    def test_build_spotify_publisher_argv_forwards_shared_options(self):
        from publishers.spotify import publish_playlist

        argv = publish_playlist.build_spotify_publisher_argv(
            playlist_output_dir=Path("collection/custom-playlists"),
            publisher_config=Path("config/custom-publisher.json"),
            no_progress=True,
            dry_run=True,
            max_new_searches_per_run=25,
            publish_state_cache=Path("collection/cache/custom-publish-state.json"),
        )

        self.assertEqual(
            argv,
            [
                "--playlist-output-dir",
                "collection/custom-playlists",
                "--publish-state-cache",
                "collection/cache/custom-publish-state.json",
                "--publisher-config",
                "config/custom-publisher.json",
                "--no-progress",
                "--publishing-dry-run",
                "--max-new-searches-per-run",
                "25",
            ],
        )

    def test_build_spotify_publisher_namespace_sets_apply_from_dry_run(self):
        from publishers.spotify import publish_playlist

        args = publish_playlist.build_spotify_publisher_namespace(
            playlist_output_dir=Path("collection/custom-playlists"),
            report=Path("reports/publish.txt"),
            publisher_config=Path("config/custom-publisher.json"),
            publish_state_cache=Path("collection/cache/custom-publish-state.json"),
            max_new_searches_per_run=25,
            dry_run=True,
            progress=False,
        )

        self.assertEqual(args.playlist_output_dir, Path("collection/custom-playlists"))
        self.assertEqual(args.report, Path("reports/publish.txt"))
        self.assertEqual(args.publisher_config, Path("config/custom-publisher.json"))
        self.assertEqual(args.publish_state_cache, Path("collection/cache/custom-publish-state.json"))
        self.assertEqual(args.max_new_searches_per_run, 25)
        self.assertFalse(args.apply)
        self.assertFalse(args.progress)

    def test_parse_args_rejects_removed_apply_flag(self):
        from publishers.spotify import publish_playlist

        stderr = io.StringIO()
        with (
            patch("sys.stderr", stderr),
            self.assertRaises(SystemExit) as exit_context,
        ):
            publish_playlist.parse_args(["--apply"])

        self.assertEqual(exit_context.exception.code, 2)
        self.assertIn("unrecognized arguments: --apply", stderr.getvalue())

    def test_parse_args_rejects_negative_max_new_searches_per_run(self):
        from publishers.spotify import publish_playlist

        stderr = io.StringIO()
        with (
            patch("sys.stderr", stderr),
            self.assertRaises(SystemExit) as exit_context,
        ):
            publish_playlist.parse_args(["--max-new-searches-per-run", "-1"])

        self.assertEqual(exit_context.exception.code, 2)
        self.assertIn("--max-new-searches-per-run must be non-negative", stderr.getvalue())

    def test_parse_args_defaults_to_script_report_path(self):
        from publishers.spotify import publish_playlist

        with patch("shared.reports.readable_timestamp", return_value="2026-06-10_14-30-00"):
            args = publish_playlist.parse_args([])

        self.assertEqual(args.report, Path("reports/2026-06-10_14-30-00_publish_playlist.txt"))


class DurableAppendCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.master = self.directory / "Synthetic" / "Synthetic.csv"
        self.cache = self.directory / "matches.json"
        self.state = self.directory / "state.json"
        self.report = self.directory / "report.txt"

    def prepare_tracks(self, count):
        rows = [playlist_row(str(index), "Album", f"Track {index}", "Artist") for index in range(1, count + 1)]
        write_playlist_master(self.master, rows)
        return PublishingSpotifyClient({
            f'track:"Track {index}" artist:"Artist" album:"Album"': (
                SpotifyTrackCandidate(f"spotify:track:{index}", f"Track {index}", ("Artist",), "Album"),
            ) for index in range(1, count + 1)
        })

    def publish(self, client, **kwargs):
        return publish_spotify_playlists(
            self.directory, self.report, client, "synthetic-token",
            playlist_master_paths=(self.master,), match_cache_path=self.cache,
            publish_state_cache_path=self.state, publisher_config=PublisherConfig("spotify", "", ""),
            max_new_searches_per_run=0, **kwargs,
        )

    def test_negative_decisions_are_durable_before_row_26(self):
        client = self.prepare_tracks(26)
        snapshot = []

        def search(access_token, query, limit=10):
            if "Track 26" in query:
                snapshot.append(json.loads(self.cache.read_text()) if self.cache.exists() else None)
                raise KeyboardInterrupt()
            return ()

        with patch.object(client, "search_tracks", side_effect=search), self.assertRaises(KeyboardInterrupt):
            self.publish(client)
        self.assertIsNotNone(snapshot[0], "25 completed negative decisions must already be on disk")
        matches = snapshot[0]["matches"]
        self.assertEqual({record["release_id"] for record in matches.values()}, {str(index) for index in range(1, 26)})
        self.assertEqual({record["match_status"] for record in matches.values()}, {"unmatched"})
        self.assertEqual(client.created_playlists, [])
        self.assertEqual(client.add_calls, [])

    def test_album_decisions_are_durable_before_next_release_search(self):
        rows = [playlist_row("album", "Album", f"Track {index}", "Artist", str(index)) for index in range(1, 4)]
        rows.append(playlist_row("next", "Next Album", "Next Track", "Next Artist"))
        write_playlist_master(self.master, rows)
        client = AlbumPublishingSpotifyClient(
            (SpotifyAlbumCandidate("album", "spotify:album:album", "Album", ("Artist",), 3),),
            {"album": tuple(SpotifyAlbumTrack(f"spotify:track:{index}", f"Track {index}", ("Artist",), 1, index, True) for index in range(1, 4))},
        )
        snapshot = []

        def search(access_token, query, limit=10):
            snapshot.append(json.loads(self.cache.read_text()) if self.cache.exists() else None)
            raise KeyboardInterrupt()

        with patch.object(client, "search_tracks", side_effect=search), self.assertRaises(KeyboardInterrupt):
            self.publish(client)
        self.assertIsNotNone(snapshot[0], "completed album resolution must already be on disk")
        self.assertEqual({record["spotify_uri"] for record in snapshot[0]["matches"].values()}, {"spotify:track:1", "spotify:track:2", "spotify:track:3"})
        self.assertEqual(client.add_calls, [])
        self.assertIn("interrupted", self.report.read_text())

    def test_startup_api_failure_writes_report_without_mutations(self):
        for method in ("list_current_user_playlists", "get_current_user_id", "get_playlist_items"):
            with self.subTest(method=method):
                client = self.prepare_tracks(1)
                client.playlists.append(SpotifyPlaylist("existing", "Synthetic", "", "current-user", False))
                error = SpotifyApiError("synthetic startup failure")
                with patch.object(client, method, side_effect=error), self.assertRaises(SpotifyApiError) as caught:
                    self.publish(client)
                self.assertIs(caught.exception, error)
                self.assertTrue(self.report.exists(), "startup failures need a current report")
                self.assertIn("failed", self.report.read_text())
                self.assertIn("Tracks: 0", self.report.read_text())
                self.assertEqual(client.searches, [])
                self.assertEqual(client.add_calls, [])

    def test_interruption_keeps_matches_and_confirmed_batch_without_flushing_pending(self):
        for completed in (1, 101):
            with self.subTest(completed=completed):
                # Distinct cache files prevent a previous subcase from hiding search work.
                self.cache = self.directory / f"matches-{completed}.json"
                self.state = self.directory / f"state-{completed}.json"
                client = self.prepare_tracks(completed + 1)
                real_search = client.search_tracks
                error = KeyboardInterrupt()

                def search(access_token, query, limit=10):
                    if f'Track {completed + 1}"' in query:
                        raise error
                    return real_search(access_token, query, limit)

                with patch.object(client, "search_tracks", side_effect=search), self.assertRaises(KeyboardInterrupt) as caught:
                    self.publish(client)
                self.assertIs(caught.exception, error)
                self.assertTrue(self.cache.exists())
                self.assertEqual(len(json.loads(self.cache.read_text())["matches"]), completed)
                self.assertEqual([len(uris) for _, uris, _ in client.add_calls], [100] if completed == 101 else [])
                report = self.report.read_text()
                self.assertIn("interrupted", report)
                self.assertIn("pending writes were not sent", report)
                self.assertIn(f"Added tracks: {100 if completed == 101 else 0}", report)
                if completed == 101:
                    tracks = json.loads(self.state.read_text())["playlists"]["Synthetic"]["tracks"]
                    self.assertEqual({record["spotify_uri"] for record in tracks.values()}, {f"spotify:track:{index}" for index in range(1, 101)})

    def test_atomic_checkpoint_failure_preserves_old_bytes_and_stops_search(self):
        from publishers.spotify.match_cache import save_spotify_track_match_cache

        client = self.prepare_tracks(26)
        save_spotify_track_match_cache(self.cache, {})
        original = self.cache.read_bytes()
        original_replace = Path.replace
        error = OSError("synthetic disk failure")

        def replace_file(source, destination):
            if Path(destination) == self.cache:
                raise error
            return original_replace(source, destination)

        with patch.object(Path, "replace", replace_file), self.assertRaises(OSError) as caught:
            self.publish(client)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.cache.read_bytes(), original)
        self.assertEqual(len(client.searches), 25)
        self.assertEqual(client.add_calls, [])
        self.assertEqual(client.created_playlists, [])
        self.assertIn("match cache checkpoint failed", self.report.read_text())
        self.assertIn("failed", self.report.read_text())

    def test_final_save_interruption_is_reported_and_best_effort_save_retried(self):
        from publishers.spotify import publish_playlist

        client = self.prepare_tracks(1)
        original_save = publish_playlist.save_spotify_track_match_cache
        attempts = []
        error = KeyboardInterrupt()

        def save(path, cache):
            attempts.append(len(cache))
            if len(attempts) == 1:
                raise error
            original_save(path, cache)

        with patch.object(publish_playlist, "save_spotify_track_match_cache", side_effect=save), self.assertRaises(KeyboardInterrupt) as caught:
            self.publish(client, apply=False)
        self.assertIs(caught.exception, error)
        self.assertEqual(attempts, [1, 1])
        self.assertEqual(len(json.loads(self.cache.read_text())["matches"]), 1)
        self.assertIn("interrupted", self.report.read_text())
        self.assertEqual(client.add_calls, [])

    def test_secondary_save_interruptions_preserve_primary_error_and_attempt_report(self):
        from publishers.spotify import publish_playlist

        client = self.prepare_tracks(1)
        for secondary in (OSError("private-secret"), KeyboardInterrupt()):
            with self.subTest(secondary=type(secondary).__name__):
                error = SpotifyApiError("primary startup failure")
                with (
                    patch.object(client, "list_current_user_playlists", side_effect=error),
                    patch.object(publish_playlist, "save_spotify_track_match_cache", side_effect=secondary),
                    patch.object(publish_playlist, "save_spotify_publish_state", side_effect=secondary),
                    self.assertRaises(SpotifyApiError) as caught,
                ):
                    try:
                        self.publish(client)
                    except KeyboardInterrupt:
                        self.fail("secondary interruption replaced the primary API failure")
                self.assertIs(caught.exception, error)
                report = self.report.read_text()
                self.assertIn("match cache checkpoint failed", report)
                self.assertIn("publish state checkpoint failed", report)
                self.assertNotIn("private-secret", report + str(error))

    def test_report_failure_never_replaces_primary_interruption(self):
        from publishers.spotify import publish_playlist

        client = self.prepare_tracks(1)
        error = KeyboardInterrupt()
        with (
            patch.object(client, "search_tracks", side_effect=error),
            patch.object(publish_playlist, "write_publish_report", side_effect=OSError("report-secret")),
            self.assertRaises(KeyboardInterrupt) as caught,
        ):
            self.publish(client)
        self.assertIs(caught.exception, error)
        self.assertIn("report write failed", str(error))
        self.assertNotIn("report-secret", str(error))
        self.assertEqual(client.add_calls, [])

    def test_uncertain_second_append_retains_confirmed_state_and_specific_report(self):
        client = self.prepare_tracks(105)
        real_add = client.add_playlist_items
        attempts = []
        error = SpotifyRateLimitDeferredError(9999, 480)

        def add(access_token, playlist_id, uris, position=None):
            attempts.append(tuple(uris))
            if len(attempts) == 2:
                # Simulate a remote write whose outcome the caller never confirms.
                real_add(access_token, playlist_id, uris, position)
                raise error
            real_add(access_token, playlist_id, uris, position)

        with patch.object(client, "add_playlist_items", side_effect=add), self.assertRaises(SpotifyRateLimitDeferredError) as caught:
            self.publish(client)
        self.assertIs(caught.exception, error)
        self.assertEqual([len(uris) for uris in attempts], [100, 5])
        self.assertEqual(len(client.playlist_items_by_id["created-1"]), 105)
        state = json.loads(self.state.read_text())["playlists"]["Synthetic"]["tracks"]
        self.assertEqual({record["spotify_uri"] for record in state.values()}, {f"spotify:track:{index}" for index in range(1, 101)})
        report = self.report.read_text()
        self.assertIn("Added tracks: 100", report)
        self.assertIn("publishing failed", report)
        self.assertIn("uncertain", report)
        self.assertNotIn("no playlist writes were attempted", report)

    def test_uncertain_create_stops_once_with_honest_report(self):
        client = self.prepare_tracks(1)
        real_create = client.create_playlist
        error = SpotifyApiError("synthetic unreadable success")

        def create(access_token, name, public=False, description=""):
            real_create(access_token, name, public, description)
            raise error

        with patch.object(client, "create_playlist", side_effect=create), self.assertRaises(SpotifyApiError) as caught:
            self.publish(client)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(client.created_playlists), 1)
        self.assertEqual(client.add_calls, [])
        self.assertIn("creation", self.report.read_text())
        self.assertIn("uncertain", self.report.read_text())
        self.assertNotIn("no playlist writes were attempted", self.report.read_text())

    def test_failed_cache_load_does_not_overwrite_invalid_input(self):
        client = self.prepare_tracks(1)
        self.cache.write_text("invalid cache sentinel")
        with self.assertRaises(ValueError):
            self.publish(client)
        self.assertEqual(self.cache.read_text(), "invalid cache sentinel")
        self.assertEqual(client.searches, [])
        self.assertTrue(self.report.exists())
        self.assertIn("failed", self.report.read_text())

    def test_replace_interruption_preserves_partial_detail_and_primary_error(self):
        from publishers.spotify import publish_playlist

        client = self.prepare_tracks(101)
        error = KeyboardInterrupt()
        real_save = publish_playlist.save_spotify_track_match_cache

        def save(path, cache):
            if client.replace_calls:
                raise OSError("private-secondary-sentinel")
            real_save(path, cache)

        with (
            patch.object(client, "add_playlist_items", side_effect=error),
            patch.object(publish_playlist, "save_spotify_track_match_cache", side_effect=save),
            self.assertRaises(KeyboardInterrupt) as caught,
        ):
            self.publish(client, publisher_sync_mode="replace")
        self.assertIs(caught.exception, error)
        self.assertEqual(len(client.replace_calls), 1)
        self.assertEqual(len(client.replace_calls[0][1]), 100)
        report = self.report.read_text()
        self.assertIn("interrupted", report)
        self.assertIn("sent first 100 URI(s)", report)
        self.assertIn("match cache checkpoint failed", report)
        self.assertNotIn("private-secondary-sentinel", report)

    def test_progress_finish_interruption_updates_complete_report(self):
        client = self.prepare_tracks(1)
        progress = ProgressReporter(label="Synthetic", stream=io.StringIO())
        error = KeyboardInterrupt()
        with patch.object(progress, "finish", side_effect=error), self.assertRaises(KeyboardInterrupt) as caught:
            try:
                raise ValueError("a caller is handling an unrelated earlier error")
            except ValueError:
                self.publish(client, progress=progress, apply=False)
        self.assertIs(caught.exception, error)
        self.assertIn("interrupted", self.report.read_text())
        self.assertNotIn("Run status: complete", self.report.read_text())

    def test_row_progress_failure_preserves_primary_search_stop(self):
        client = self.prepare_tracks(1)
        progress = ProgressReporter(label="Synthetic", stream=io.StringIO())
        error = SpotifyRateLimitDeferredError(9999, 480)
        with (
            patch.object(client, "search_tracks", side_effect=error),
            patch.object(progress, "update", side_effect=KeyboardInterrupt()),
            self.assertRaises(SpotifyRateLimitDeferredError) as caught,
        ):
            try:
                self.publish(client, progress=progress)
            except KeyboardInterrupt:
                self.fail("progress cleanup replaced the primary rate-limit stop")
        self.assertIs(caught.exception, error)
        self.assertIn("rate limit", self.report.read_text())

    def test_progress_cleanup_failure_does_not_hide_primary_stop(self):
        client = self.prepare_tracks(1)
        progress = ProgressReporter(label="Synthetic", stream=io.StringIO())
        error = SpotifyApiError("primary failure")
        with (
            patch.object(client, "list_current_user_playlists", side_effect=error),
            patch.object(progress, "finish", side_effect=KeyboardInterrupt()),
            self.assertRaises(SpotifyApiError) as caught,
        ):
            self.publish(client, progress=progress)
        self.assertIs(caught.exception, error)
        self.assertIn("failed", self.report.read_text())
        self.assertEqual(client.add_calls, [])

    def test_filesystem_error_surfaces_failed_checkpoints_even_when_report_is_stale(self):
        import argparse
        import errno
        from publishers.spotify import publish_playlist

        client = self.prepare_tracks(1)
        self.report.write_text("previous report sentinel")
        error = OSError(errno.ENOSPC, "synthetic disk full", "synthetic-cache.json")
        caught_errors = []

        def run(*args, **kwargs):
            try:
                return self.publish(client)
            except OSError as caught:
                caught_errors.append(caught)
                raise

        with (
            patch.object(publish_playlist, "parse_args", return_value=argparse.Namespace(report=self.report, debug_log=None)),
            patch.object(publish_playlist, "run_spotify_publish_from_args", side_effect=run),
            patch.object(client, "list_current_user_playlists", side_effect=error),
            patch.object(publish_playlist, "save_spotify_track_match_cache", side_effect=OSError("secondary-private-sentinel")),
            patch.object(publish_playlist, "write_publish_report", side_effect=OSError("secondary-report-sentinel")),
            patch("sys.stderr", new_callable=io.StringIO) as output,
        ):
            result = publish_playlist.main([])
        self.assertEqual(result, 1)
        self.assertEqual(caught_errors, [error])
        self.assertIs(caught_errors[0], error)
        self.assertEqual(error.errno, errno.ENOSPC)
        self.assertEqual(error.filename, "synthetic-cache.json")
        self.assertIn("synthetic disk full", output.getvalue())
        self.assertIn("match cache checkpoint failed", output.getvalue())
        self.assertIn("publish report write failed", output.getvalue())
        self.assertNotIn("secondary-private-sentinel", output.getvalue())
        self.assertNotIn("secondary-report-sentinel", output.getvalue())
        self.assertEqual(self.report.read_text(), "previous report sentinel")
        self.assertEqual(client.add_calls, [])
        self.assertEqual(client.created_playlists, [])

    def test_main_ctrl_c_returns_130_with_current_report(self):
        from publishers.spotify import publish_playlist

        client = self.prepare_tracks(2)
        original_search = client.search_tracks

        def search(access_token, query, limit=10):
            if "Track 2" in query:
                raise KeyboardInterrupt()
            return original_search(access_token, query, limit)

        args = publish_playlist.build_spotify_publisher_namespace(
            env_file=self.directory / "absent.env", playlist_output_dir=self.directory,
            report=self.report, token_cache=self.directory / "token.json", match_cache=self.cache,
            publish_state_cache=self.state, publisher_config=self.directory / "publisher.json", progress=False,
        )
        args.access_token = "synthetic-token"
        original_run = publish_playlist.run_spotify_publish_from_args
        with (
            patch.object(publish_playlist, "parse_args", return_value=args),
            patch.object(publish_playlist, "load_spotify_settings", return_value=SpotifySettings("client", "http://localhost/callback", args.token_cache)),
            patch.object(publish_playlist, "load_or_create_publisher_config", return_value=PublisherConfig("spotify", "", "")),
            patch.object(client, "search_tracks", side_effect=search),
            patch("sys.stderr", new_callable=io.StringIO) as output,
        ):
            with patch.object(publish_playlist, "run_spotify_publish_from_args", side_effect=lambda args, **kwargs: original_run(args, spotify_client=client, **kwargs)):
                try:
                    result = publish_playlist.main([])
                except KeyboardInterrupt:
                    self.fail("CLI must return 130 after preserving progress")
        self.assertEqual(result, 130)
        self.assertIn("interrupted", output.getvalue().lower())
        self.assertIn(str(self.report), output.getvalue())
        self.assertIn("interrupted", self.report.read_text())
        self.assertEqual(len(json.loads(self.cache.read_text())["matches"]), 1)
        self.assertEqual(client.add_calls, [])


class RenewablePublisherTests(unittest.TestCase):
    def test_incomplete_refresh_response_preserves_completed_match_and_stops_once(self):
        from http.client import IncompleteRead
        from publishers.spotify.client import HttpResponse, SpotifyAuthenticationError, SpotifyClient
        from publishers.spotify.session import SpotifySession
        from publishers.spotify.token_cache import save_spotify_token

        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            master = directory / "Synthetic" / "Synthetic.csv"
            write_playlist_master(master, [playlist_row(str(index), "Album", f"Track {index}", "Artist") for index in range(3)])
            settings = SpotifySettings("client", "http://localhost/callback", directory / "token.json")
            token = SpotifyToken("old-secret", "refresh-secret", 1061, " ".join(DEFAULT_AUTHORIZE_SCOPES), "Bearer")
            save_spotify_token(settings.token_cache_path, token)
            now = [1000.0]
            requests = []
            token_requests = []

            def token_transport(request):
                token_requests.append(request)
                raise IncompleteRead(b"response-secret", 100)

            session = SpotifySession(settings, token, DEFAULT_AUTHORIZE_SCOPES, wall_time=lambda: now[0], token_transport=token_transport)

            def transport(request):
                requests.append(request)
                if "/search?" in request.url:
                    now[0] = 1002
                    return HttpResponse(200, {}, '{"tracks":{"items":[{"uri":"spotify:track:0","name":"Track 0","artists":[{"name":"Artist"}],"album":{"name":"Album"}}]}}')
                return HttpResponse(200, {}, '{"items":[],"id":"owner"}')

            client = SpotifyClient(transport=transport, token_provider=session)
            with self.assertRaises(SpotifyAuthenticationError) as caught:
                publish_spotify_playlists(directory, directory / "report.txt", client, "ignored", playlist_master_paths=(master,), match_cache_path=directory / "matches.json", publish_state_cache_path=directory / "state.json", publisher_config=PublisherConfig("spotify", "", ""), request_control=client.request_control, spotify_session=session)
            with self.assertRaises(SpotifyAuthenticationError) as repeated:
                session.get_access_token()
            self.assertIs(caught.exception, repeated.exception)
            self.assertEqual(len(requests), 3)
            self.assertTrue(all(request.method == "GET" for request in requests))
            self.assertEqual(len(token_requests), 1)
            matches = json.loads((directory / "matches.json").read_text())["matches"]
            self.assertEqual(list(matches), ["0|1|artist|album|track 0"])
            report = (directory / "report.txt").read_text()
            self.assertIn("needs action", report)
            self.assertIn("Tracks: 1", report)
            self.assertIn("Token refresh attempts: 1", report)
            self.assertIn("Web API attempts: 3", report)
            self.assertIn("pending writes were not sent", report)
            self.assertNotIn("secret", report + str(caught.exception))

    def test_raw_override_on_reused_concrete_client_bypasses_provider(self):
        from publishers.spotify import publish_playlist
        from publishers.spotify.client import HttpResponse, SpotifyClient

        class Provider:
            def get_access_token(self, force_refresh=False):
                raise AssertionError("Raw-token runs must bypass renewal")

        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            requests = []

            def transport(request):
                requests.append(request)
                return HttpResponse(200, {}, '{"items":[],"id":"owner"}')

            client = SpotifyClient(transport=transport, token_provider=Provider())
            args = publish_playlist.build_spotify_publisher_namespace(env_file=directory / "env", playlist_output_dir=directory, report=directory / "report.txt", token_cache=directory / "token.json", match_cache=directory / "matches.json", publish_state_cache=directory / "state.json", publisher_config=directory / "publisher.json", progress=False, access_token="raw")
            with patch.object(publish_playlist, "load_spotify_settings", return_value=SpotifySettings("client", "http://localhost/callback")), patch.object(publish_playlist, "get_access_token_for_run") as startup:
                summary = publish_playlist.run_spotify_publish_from_args(args, playlist_master_paths=(), publisher_config=PublisherConfig("spotify", "", ""), spotify_client=client)
            startup.assert_not_called()
            self.assertEqual([request.headers["Authorization"] for request in requests], ["Bearer raw", "Bearer raw"])
            self.assertIsNone(summary.token_refresh_count)

    def test_revoked_live_session_stops_cli_once_with_report_and_refresh_count(self):
        from publishers.spotify import publish_playlist
        from publishers.spotify.client import HttpResponse, SpotifyClient
        from publishers.spotify.session import SpotifySession
        from publishers.spotify.token_cache import save_spotify_token

        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            master = directory / "Synthetic" / "Synthetic.csv"
            write_playlist_master(master, [playlist_row(str(index), "Album", f"Track {index}", "Artist") for index in range(3)])
            settings = SpotifySettings("client", "http://localhost/callback", directory / "token.json")
            token = SpotifyToken("old-secret", "refresh-secret", 1061, " ".join(DEFAULT_AUTHORIZE_SCOPES), "Bearer")
            save_spotify_token(settings.token_cache_path, token)
            now = [1000.0]
            requests = []
            token_requests = []

            def token_transport(request):
                token_requests.append(request)
                return HttpResponse(400, {}, '{"error":"invalid_grant","description":"response-secret"}')

            session = SpotifySession(settings, token, DEFAULT_AUTHORIZE_SCOPES, wall_time=lambda: now[0], token_transport=token_transport)

            def transport(request):
                requests.append(request)
                now[0] = 1002
                return HttpResponse(200, {}, '{"items":[],"id":"owner"}')

            original_init = SpotifyClient.__init__

            def initialize_client(instance, debug_log=None):
                original_init(instance, transport=transport, debug_log=debug_log)

            args = publish_playlist.build_spotify_publisher_namespace(env_file=directory / "env", playlist_output_dir=directory, report=directory / "report.txt", token_cache=settings.token_cache_path, match_cache=directory / "matches.json", publish_state_cache=directory / "state.json", publisher_config=directory / "publisher.json", progress=False)
            with (
                patch.object(publish_playlist, "parse_args", return_value=args),
                patch.object(publish_playlist, "load_spotify_settings", return_value=settings),
                patch.object(publish_playlist, "get_access_token_for_run", return_value="old-secret") as startup,
                patch.object(publish_playlist, "SpotifySession", return_value=session),
                patch.object(SpotifyClient, "__init__", initialize_client),
                patch("sys.stderr", new_callable=io.StringIO) as errors,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(publish_playlist.main([]), 1)
            startup.assert_called_once()
            self.assertEqual(len(requests), 1)
            self.assertEqual(len(token_requests), 1)
            report = args.report.read_text()
            self.assertIn("needs action", report)
            self.assertIn("Token refresh attempts: 1", report)
            self.assertIn("Web API attempts: 1", report)
            self.assertIn("Token refresh attempts: 1", errors.getvalue())
            self.assertIn(str(args.report), errors.getvalue())
            self.assertNotIn("secret", report + errors.getvalue())
            self.assertEqual(json.loads(args.match_cache.read_text())["matches"], {})

    def test_replace_authentication_preserves_partial_report_and_primary_failure(self):
        from publishers.spotify import publish_playlist
        from publishers.spotify.client import SpotifyAuthenticationError

        for save_failure in (False, True):
            with self.subTest(save_failure=save_failure), tempfile.TemporaryDirectory() as directory_name:
                directory = Path(directory_name)
                master = directory / "Synthetic" / "Synthetic.csv"
                write_playlist_master(master, [playlist_row(str(index), "Album", f"Track {index}", "Artist") for index in range(101)])
                client = PublishingSpotifyClient({f'track:"Track {index}" artist:"Artist" album:"Album"': (SpotifyTrackCandidate(f"spotify:track:{index}", f"Track {index}", ("Artist",), "Album"),) for index in range(101)})
                error = SpotifyAuthenticationError("Reauthorize")
                real_save = publish_playlist.save_spotify_track_match_cache

                def save_cache(path, cache):
                    if save_failure and client.replace_calls:
                        raise OSError("save-secret")
                    real_save(path, cache)

                with (
                    patch.object(client, "add_playlist_items", side_effect=error),
                    patch.object(publish_playlist, "save_spotify_track_match_cache", side_effect=save_cache),
                    self.assertRaises(SpotifyAuthenticationError),
                ):
                    publish_spotify_playlists(directory, directory / "report.txt", client, "raw", playlist_master_paths=(master,), match_cache_path=directory / "matches.json", publish_state_cache_path=directory / "state.json", publisher_config=PublisherConfig("spotify", "", ""), publisher_sync_mode="replace")
                report = (directory / "report.txt").read_text()
                self.assertIn("needs action", report)
                self.assertIn("sent first 100 URI(s)", report)
                self.assertEqual(len(client.replace_calls), 1)
                if save_failure:
                    self.assertIn("match cache checkpoint failed", report)
                self.assertNotIn("save-secret", report)

    def test_album_authentication_propagates_without_a_match_verdict(self):
        from publishers.spotify.client import SpotifyAuthenticationError

        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            master = directory / "Synthetic" / "Synthetic.csv"
            write_playlist_master(master, [playlist_row("111", "Album", f"Track {index}", "Artist", track_number=str(index)) for index in range(1, 4)])
            client = AlbumPublishingSpotifyClient((), {})
            with patch.object(client, "search_albums", side_effect=SpotifyAuthenticationError("Reauthorize")) as search, self.assertRaises(SpotifyAuthenticationError):
                publish_spotify_playlists(directory, directory / "report.txt", client, "raw", playlist_master_paths=(master,), match_cache_path=directory / "matches.json", publish_state_cache_path=directory / "state.json", apply=False)
            search.assert_called_once()
            self.assertEqual(client.searches, [])
            self.assertEqual(json.loads((directory / "matches.json").read_text())["matches"], {})
            self.assertIn("needs action", (directory / "report.txt").read_text())

    def test_authentication_stops_startup_search_creation_and_second_batch_once(self):
        from publishers.spotify.client import SpotifyAuthenticationError
        from publishers.spotify.publish_state import load_spotify_publish_state

        for stage in ("inventory", "user", "search", "create", "second_batch"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory_name:
                directory = Path(directory_name)
                master = directory / "Synthetic" / "Synthetic.csv"
                count = 105 if stage == "second_batch" else 3
                rows = [playlist_row(str(index), "Album", f"Track {index}", "Artist") for index in range(count)]
                write_playlist_master(master, rows)
                calls = []
                error = SpotifyAuthenticationError("Reauthorize before restarting")

                class Client(PublishingSpotifyClient):
                    def list_current_user_playlists(self, access_token):
                        calls.append("inventory")
                        if stage == "inventory":
                            raise error
                        return super().list_current_user_playlists(access_token)

                    def get_current_user_id(self, access_token):
                        calls.append("user")
                        if stage == "user":
                            raise error
                        return "current-user"

                    def search_tracks(self, access_token, query, limit=10):
                        calls.append("search")
                        if stage == "search" and len(self.searches) == 1:
                            raise error
                        return super().search_tracks(access_token, query, limit)

                    def create_playlist(self, access_token, name, public=False, description=""):
                        calls.append("create")
                        if stage == "create":
                            raise error
                        return super().create_playlist(access_token, name, public, description)

                    def add_playlist_items(self, access_token, playlist_id, uris, position=None):
                        calls.append("add")
                        if stage == "second_batch" and self.add_calls:
                            raise error
                        return super().add_playlist_items(access_token, playlist_id, uris, position)

                client = Client({f'track:"Track {index}" artist:"Artist" album:"Album"': (SpotifyTrackCandidate(f"spotify:track:{index}", f"Track {index}", ("Artist",), "Album"),) for index in range(count)})
                report = directory / "report.txt"
                cache = directory / "matches.json"
                state = directory / "state.json"
                with self.assertRaises(SpotifyAuthenticationError) as caught:
                    publish_spotify_playlists(directory, report, client, "raw", playlist_master_paths=(master,), match_cache_path=cache, publish_state_cache_path=state, publisher_config=PublisherConfig("spotify", "", ""))
                self.assertIs(caught.exception, error)
                text = report.read_text()
                self.assertIn("needs action", text)
                self.assertIn("pending writes were not sent", text)
                self.assertNotIn("no playlist writes were attempted", text)
                self.assertEqual(calls.count("search"), 2 if stage == "search" else count if stage in {"create", "second_batch"} else 0)
                matches = json.loads(cache.read_text())["matches"]
                self.assertEqual(len(matches), 1 if stage == "search" else count if stage in {"create", "second_batch"} else 0)
                if stage == "second_batch":
                    self.assertEqual(len(client.add_calls), 1)
                    self.assertEqual(len(load_spotify_publish_state(state)["Synthetic"]), 100)
                    self.assertIn("Added tracks: 100", text)
                    self.assertIn("publishing failed", text)
                else:
                    self.assertEqual(client.add_calls, [])

    def test_authentication_remains_primary_when_checkpoint_save_fails(self):
        from publishers.spotify import publish_playlist
        from publishers.spotify.client import SpotifyAuthenticationError

        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            error = SpotifyAuthenticationError("Reauthorize before restarting")
            client = PublishingSpotifyClient({})
            with (
                patch.object(client, "list_current_user_playlists", side_effect=error),
                patch.object(publish_playlist, "save_spotify_track_match_cache", side_effect=OSError("private-path-secret")),
                self.assertRaises(SpotifyAuthenticationError) as caught,
            ):
                publish_spotify_playlists(directory, directory / "report.txt", client, "raw", playlist_master_paths=(), match_cache_path=directory / "matches.json", publish_state_cache_path=directory / "state.json")
            self.assertIs(caught.exception, error)
            report = (directory / "report.txt").read_text()
            self.assertIn("match cache checkpoint failed", report)
            self.assertNotIn("private-path-secret", report)

    def test_normal_startup_constructs_session_and_reports_actual_refresh_attempts(self):
        from publishers.spotify import publish_playlist
        from publishers.spotify.client import HttpResponse, SpotifyClient
        from publishers.spotify.session import SpotifySession
        from publishers.spotify.token_cache import save_spotify_token

        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            settings = SpotifySettings("client", "http://localhost/callback", directory / "token.json")
            token = SpotifyToken("old", "refresh", 1061, " ".join(DEFAULT_AUTHORIZE_SCOPES), "Bearer")
            save_spotify_token(settings.token_cache_path, token)
            now = [1000.0]
            requests = []
            session = SpotifySession(settings, token, DEFAULT_AUTHORIZE_SCOPES, wall_time=lambda: now[0], token_transport=lambda request: HttpResponse(200, {}, '{"access_token":"fresh"}'))

            def transport(request):
                requests.append(request)
                now[0] = 1002
                return HttpResponse(200, {}, '{"items":[],"id":"owner"}')

            original_init = SpotifyClient.__init__

            def initialize_client(instance, debug_log=None):
                original_init(instance, transport=transport, debug_log=debug_log)
            args = publish_playlist.build_spotify_publisher_namespace(env_file=directory / "env", playlist_output_dir=directory, report=directory / "report.txt", token_cache=settings.token_cache_path, match_cache=directory / "matches.json", publish_state_cache=directory / "state.json", publisher_config=directory / "publisher.json", progress=False)
            with (
                patch.object(publish_playlist, "load_spotify_settings", return_value=settings),
                patch.object(publish_playlist, "get_access_token_for_run", return_value="old") as authorize,
                patch.object(publish_playlist, "SpotifySession", return_value=session) as construct,
                patch.object(SpotifyClient, "__init__", initialize_client),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                summary = publish_playlist.run_spotify_publish_from_args(args, playlist_master_paths=(), publisher_config=PublisherConfig("spotify", "", ""))
                publish_playlist.print_summary(summary)
            authorize.assert_called_once()
            construct.assert_called_once()
            self.assertEqual([request.headers["Authorization"] for request in requests], ["Bearer old", "Bearer fresh"])
            self.assertIn("Token refresh attempts: 1", args.report.read_text())
            self.assertIn("Token refresh attempts: 1", output.getvalue())


class SpotifyRequestPacingPublisherTests(unittest.TestCase):
    def test_legacy_summary_without_request_count_prints_unavailable(self):
        from publishers.spotify import publish_playlist

        summary = publish_summary_stub()
        delattr(type(summary), "request_count")
        with (
            patch.object(publish_playlist, "run_spotify_publish_from_args", return_value=summary),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(publish_playlist.main([]), 0)
        self.assertIn("Web API attempts: unavailable", output.getvalue())

    def test_request_interval_rejected_before_settings(self):
        from publishers.spotify import publish_playlist

        for interval in (-1.0, float("nan"), float("inf"), -float("inf")):
            with self.subTest(interval=interval):
                args = publish_playlist.build_spotify_publisher_namespace()
                args.request_interval_seconds = interval
                with (
                    patch.object(publish_playlist, "load_spotify_settings", side_effect=AssertionError("settings accessed before validation")) as settings,
                    patch.object(publish_playlist, "load_or_create_publisher_config") as config,
                    patch.object(publish_playlist, "get_access_token_for_run") as token,
                    self.assertRaisesRegex(ValueError, "finite.*non-negative"),
                ):
                    publish_playlist.run_spotify_publish_from_args(args)
                settings.assert_not_called()
                config.assert_not_called()
                token.assert_not_called()
                with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as failure:
                    publish_playlist.parse_args([f"--request-interval-seconds={interval}"])
                self.assertEqual(failure.exception.code, 2)

    def test_request_interval_builders_preserve_omitted_zero_and_positive_values(self):
        from publishers.spotify import publish_playlist

        for interval, expected in ((None, 0.0), (0.0, 0.0), (0.5, 0.5)):
            with self.subTest(interval=interval):
                argv = publish_playlist.build_spotify_publisher_argv(request_interval_seconds=interval)
                parsed = publish_playlist.parse_args(argv)
                namespace = publish_playlist.build_spotify_publisher_namespace(request_interval_seconds=interval)
                self.assertEqual(parsed.request_interval_seconds, expected)
                self.assertEqual(namespace.request_interval_seconds, expected)
                self.assertEqual("--request-interval-seconds" in argv, interval is not None)
                self.assertEqual(parsed.max_new_searches_per_run, 500)

    def test_report_counts_transport_attempts_separately_from_searches(self):
        self.run_connected_publish(fail_write=False, sync_mode="append")

    def test_normal_runner_paces_its_constructed_client(self):
        self.run_connected_publish(fail_write=False, sync_mode="append", construct_client=True)

    def test_failed_append_report_counts_the_failed_attempt(self):
        self.run_connected_publish(fail_write=True, sync_mode="append")

    def test_replace_report_counts_the_final_write(self):
        self.run_connected_publish(fail_write=False, sync_mode="replace")

    def test_budget_stop_report_counts_the_checkpoint_write(self):
        self.run_connected_publish(fail_write=False, sync_mode="append", budget_stop=True)

    def run_connected_publish(self, *, fail_write: bool, sync_mode: str, construct_client: bool = False, budget_stop: bool = False):
        from publishers.spotify import publish_playlist
        from publishers.spotify.client import HttpRequest, HttpResponse, SpotifyClient
        from publishers.spotify.request_control import SpotifyRequestControl
        from tests.spotify.test_request_control import FakeClock

        clock = FakeClock()
        control = SpotifyRequestControl(monotonic=clock.monotonic, sleep=clock.sleep)
        starts: list[float] = []
        requests: list[HttpRequest] = []
        remote_uris: list[str] = []
        search_attempts = 0
        track = {"uri": "spotify:track:alpha", "name": "Alpha One", "artists": [{"name": "Alpha Artist"}], "album": {"name": "Alpha Album"}}

        def transport(request: HttpRequest) -> HttpResponse:
            nonlocal search_attempts
            starts.append(clock.now)
            requests.append(request)
            if "/me/playlists?" in request.url:
                return HttpResponse(200, {}, json.dumps({"items": [{"id": "target", "name": "Synthetic", "owner": {"id": "owner"}, "public": False}], "next": None}))
            if request.url.endswith("/me"):
                return HttpResponse(200, {}, '{"id":"owner"}')
            if request.method == "GET" and "/playlists/target/items?" in request.url:
                return HttpResponse(200, {}, json.dumps({"items": [{"item": track} for _ in remote_uris], "next": None}))
            if "/search?" in request.url:
                search_attempts += 1
                if search_attempts == 1:
                    return HttpResponse(429, {"Retry-After": "4"}, "")
                return HttpResponse(200, {}, json.dumps({"tracks": {"items": [track]}}))
            if request.url.endswith("/playlists/target/items") and request.method in {"POST", "PUT"}:
                if fail_write:
                    return HttpResponse(500, {}, "synthetic write failure")
                assert request.body is not None
                remote_uris[:] = json.loads(request.body)["uris"]
                return HttpResponse(201 if request.method == "POST" else 200, {}, '{}')
            self.fail(f"Unexpected request: {request.method} {request.url}")

        client = SpotifyClient(transport=transport, sleep=clock.sleep, request_control=control)
        original_init = SpotifyClient.__init__

        def initialize_client(instance, debug_log=None):
            original_init(instance, transport=transport, sleep=clock.sleep, request_control=control, debug_log=debug_log)

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            master = directory / "playlists" / "Synthetic" / "Synthetic.csv"
            rows = [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")]
            if budget_stop:
                rows.append(playlist_row("222", "Beta Album", "Beta One", "Beta Artist"))
            write_playlist_master(master, rows)
            args = publish_playlist.build_spotify_publisher_namespace(
                env_file=directory / "env", playlist_output_dir=directory / "playlists",
                report=directory / "report.txt", token_cache=directory / "token.json",
                match_cache=directory / "match.json", publish_state_cache=directory / "state.json",
                publisher_config=directory / "publisher.json", access_token="synthetic-token",
                request_interval_seconds=1.0, publisher_sync_mode=sync_mode, progress=False,
                max_new_searches_per_run=1 if budget_stop else 500,
            )
            snapshots = []
            real_writer = publish_playlist.write_publish_report

            def write_report(path, summary):
                real_writer(path, summary)
                snapshots.append((summary.run_status, summary.request_count, path.read_text()))

            with (
                patch.object(publish_playlist, "load_spotify_settings", return_value=SpotifySettings("synthetic-client", "http://127.0.0.1:8765/callback")),
                patch.object(publish_playlist, "write_publish_report", side_effect=write_report),
                patch.object(SpotifyClient, "__init__", initialize_client),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                if fail_write:
                    with self.assertRaises(SpotifyApiError):
                        publish_playlist.run_spotify_publish_from_args(args, publisher_config=PublisherConfig("spotify", "", ""), spotify_client=client)
                    self.assertEqual(remote_uris, [])
                    self.assertIn("failed", snapshots[-1][0])
                else:
                    summary = publish_playlist.run_spotify_publish_from_args(args, publisher_config=PublisherConfig("spotify", "", ""), spotify_client=None if construct_client else client)
                    publish_playlist.print_summary(summary)
                    self.assertEqual(summary.request_count, 6)
                    self.assertEqual(summary.search_count, 1)
                    self.assertEqual(remote_uris, ["spotify:track:alpha"])
                    self.assertIn("Web API attempts: 6", output.getvalue())
                    if budget_stop:
                        self.assertIn("search budget reached", summary.run_status)
                        self.assertEqual(summary.track_count, 1)
                    elif sync_mode == "append":
                        self.assertTrue(any("checkpoint" in state and count == 6 for state, count, _ in snapshots))
                self.assertEqual(starts, [0, 1, 2, 3, 7, 8])
                self.assertEqual(snapshots[-1][1], 6)
                self.assertIn("Web API attempts: 6", args.report.read_text())
                self.assertIn("Spotify searches: 1", args.report.read_text())
                if not fail_write and sync_mode == "append" and not construct_client and not budget_stop:
                    # A second invocation reuses the cache/client, but reports only its own reads.
                    starts.clear()
                    requests.clear()
                    cached = publish_playlist.run_spotify_publish_from_args(args, publisher_config=PublisherConfig("spotify", "", ""), spotify_client=client)
                    self.assertEqual(starts, [9, 10, 11])
                    self.assertEqual(cached.request_count, 3)
                    self.assertEqual(cached.search_count, 0)
                    self.assertEqual(cached.cache_hit_count, 1)
                    self.assertTrue(all(request.method == "GET" for request in requests))

    def test_old_namespace_and_custom_client_keep_unavailable_metrics(self):
        from publishers.spotify import publish_playlist

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            args = publish_playlist.build_spotify_publisher_namespace(
                playlist_output_dir=directory, report=directory / "report.txt",
                match_cache=directory / "match.json", publish_state_cache=directory / "state.json",
                access_token="synthetic-token", dry_run=True, progress=False,
            )
            if hasattr(args, "request_interval_seconds"):
                delattr(args, "request_interval_seconds")
            with patch.object(publish_playlist, "load_spotify_settings", return_value=SpotifySettings("synthetic", "http://127.0.0.1:8765/callback")):
                summary = publish_playlist.run_spotify_publish_from_args(
                    args, playlist_master_paths=(), publisher_config=PublisherConfig("spotify", "", ""), spotify_client=PublishingSpotifyClient({}),
                )
            self.assertIsNone(summary.request_count)
            self.assertIn("Web API attempts: unavailable", args.report.read_text())


class SpotifyContinuousPublishingTests(unittest.TestCase):
    def setUp(self):
        from publishers.spotify import publish_playlist as module
        from publishers.spotify.client import HttpResponse, SpotifyClient
        from publishers.spotify.request_control import SpotifyRequestControl
        from publishers.spotify.session import SpotifySession
        from publishers.spotify.token_cache import save_spotify_token
        from tests.spotify.test_request_control import FakeClock
        import urllib.parse

        self.module = module
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.master = self.directory / "Synthetic" / "Synthetic.csv"
        self.names = ["alpha", "bravo", "charlie", "delta", "echo"]
        self.write_source(self.names)
        self.clock = FakeClock()
        self.remote = []
        self.created = False
        self.user_id = "owner"
        self.requests = []
        self.writes = []
        self.search_names = []
        self.responses = []
        self.reports = []
        self.refreshes = []
        self.before_transport = None
        self.on_sleep = None
        self.settings = SpotifySettings("synthetic-client", "http://127.0.0.1:8765/callback", self.directory / "token.json")
        self.token = SpotifyToken(access_token="TOKEN_SENTINEL", refresh_token="REFRESH_SENTINEL", expires_at=1700003600, scope=" ".join(DEFAULT_AUTHORIZE_SCOPES), token_type="Bearer")
        save_spotify_token(self.settings.token_cache_path, self.token)
        self.args = module.build_spotify_publisher_namespace(
            playlist_output_dir=self.directory, report=self.directory / "report.txt",
            token_cache=self.settings.token_cache_path, match_cache=self.directory / "match.json",
            publish_state_cache=self.directory / "state.json", continuous=True,
            max_new_searches_per_run=2, progress=False,
        )
        def sleep(seconds):
            if self.on_sleep:
                self.on_sleep(seconds)
            self.clock.sleep(seconds)
        self.control = SpotifyRequestControl(1, monotonic=self.clock.monotonic,
                                             wall_time=lambda: 1700000000 + self.clock.now, sleep=sleep)
        def token_transport(request):
            self.refreshes.append(self.clock.now)
            self.clock.now += 2
            return HttpResponse(200, {}, json.dumps({"access_token":"ROTATED_SENTINEL", "refresh_token":"ROTATED_REFRESH_SENTINEL", "token_type":"Bearer", "expires_in":3600}))
        def transport(request):
            parsed = urllib.parse.urlparse(request.url)
            query = urllib.parse.parse_qs(parsed.query)
            entry = (self.clock.now, request.method, parsed.path, json.loads(request.body) if request.body else None)
            self.requests.append(entry)
            if self.before_transport:
                override = self.before_transport(request, parsed, query)
                if override is not None:
                    return override
            if parsed.path == "/v1/me":
                payload = {"id": self.user_id}
            elif parsed.path == "/v1/me/playlists" and request.method == "GET":
                payload = {"items": [self.playlist_payload()] if self.created else [], "next":None}
            elif parsed.path == "/v1/me/playlists":
                self.created = True
                self.writes.append(entry)
                return HttpResponse(201, {}, json.dumps(self.playlist_payload()))
            elif parsed.path == "/v1/search":
                if query["type"] == ["album"]:
                    payload = {"albums":{"items":[]}}
                else:
                    name = next(name for name in self.names if f'track:"{name}"' in query["q"][0])
                    self.search_names.append(name)
                    payload = {"tracks":{"items":[self.track_payload(name)]}}
            elif request.method == "GET":
                payload = {"items":[{"track":self.track_payload(uri.split(":")[-1])} for uri in self.remote], "next":None}
            else:
                self.writes.append(entry)
                data = json.loads(request.body)
                if request.method == "PUT":
                    self.remote[:] = data["uris"]
                    return HttpResponse(200, {}, "{}")
                position = data.get("position", len(self.remote))
                self.remote[position:position] = data["uris"]
                return HttpResponse(201, {}, "{}")
            return HttpResponse(200, {}, json.dumps(payload))
        self.client = SpotifyClient(transport=transport, request_control=self.control, sleep=sleep)
        def session(settings, token, scopes):
            return SpotifySession(settings, token, scopes, wall_time=lambda:1700000000 + self.clock.now, token_transport=token_transport)
        self.startup = self.enter_patch(patch.object(module, "get_access_token_for_run", return_value="TOKEN_SENTINEL"))
        self.enter_patch(patch.object(module, "load_spotify_settings", return_value=self.settings))
        self.enter_patch(patch.object(module, "SpotifySession", side_effect=session))
        real_writer = module.write_publish_report
        def report(path, summary):
            real_writer(path, summary)
            self.reports.append(summary)
        self.enter_patch(patch.object(module, "write_publish_report", side_effect=report))

    def enter_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def write_source(self, names, release_id=""):
        write_playlist_master(self.master, [{"Track Name":name, "Artist Name":"Synthetic Artist", "Album Name":"Synthetic Album", "Release Id":release_id, "Track Number":str(index)} for index, name in enumerate(names, 1)])

    def playlist_payload(self):
        return {"id":"playlist", "name":"Synthetic", "owner":{"id":"owner"}, "public":False, "collaborative":False}

    def track_payload(self, name):
        return {"id":name, "uri":f"spotify:track:{name}", "name":name, "type":"track", "artists":[{"name":"Synthetic Artist"}], "album":{"name":"Synthetic Album", "id":"album"}}

    def run_publish(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            try:
                return self.module.run_spotify_publish_from_args(self.args, publisher_config=PublisherConfig("spotify", "", ""), spotify_client=self.client)
            finally:
                self.console = output.getvalue()

    def test_j1_three_capped_passes_full_final_coverage_and_exact_order_once(self):
        def observe_boundary(request, parsed, query):
            if parsed.path == "/v1/me" and len(self.requests) > 1:
                expected = 1 if len(self.remote) == 2 else 2
                self.assertIn(f"Invocation completed passes: {expected}", self.args.report.read_text())
            return None
        self.before_transport = observe_boundary
        summary = self.run_publish()
        self.assertEqual(self.remote, ["spotify:track:alpha", "spotify:track:bravo", "spotify:track:charlie", "spotify:track:delta", "spotify:track:echo"])
        self.assertEqual(self.search_names, ["alpha", "bravo", "charlie", "delta", "echo"])
        self.assertEqual(summary.completed_pass_count, 3)
        self.assertEqual(summary.stop_reason, "complete")
        self.assertEqual(summary.run_state, "complete")
        self.assertEqual(summary.track_count, 5)
        self.assertEqual(summary.cache_hit_count, 4)
        self.assertEqual(summary.search_count, 1)
        self.assertEqual(summary.request_count, 17)
        self.assertEqual(self.startup.call_count, 1)
        self.assertEqual([report.stop_reason for report in self.reports if report.stop_reason == "search_budget"], ["search_budget", "search_budget", "search_budget", "search_budget"])
        print("J1 trace:", json.dumps({"requests":self.requests,"remote":self.remote,"passes":summary.completed_pass_count,"coverage":summary.track_count}))

    def test_continuous_dry_run_and_uncapped_zero(self):
        self.args.apply = False
        self.args.dry_run = True
        self.args.max_new_searches_per_run = 0
        summary = self.run_publish()
        self.assertEqual(self.writes, [])
        self.assertEqual(summary.track_count, 5)
        self.assertEqual(summary.completed_pass_count, 1)
        self.assertEqual(summary.would_add_count, 5)

    def test_forbidden_flags_fail_before_settings_or_token_side_effects(self):
        for field, value in (("publisher_sync_mode", "replace"), ("refresh_match_cache", True), ("access_token", "TOKEN"), ("request_interval_seconds", 0), ("request_interval_seconds", float("inf"))):
            original = getattr(self.args, field)
            setattr(self.args, field, value)
            with patch.object(self.module, "load_spotify_settings") as settings, self.assertRaises(ValueError):
                self.run_publish()
            settings.assert_not_called()
            setattr(self.args, field, original)
        self.startup.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_j2_receipt_cooldown_expiry_slow_refresh_and_four_pauses(self):
        from publishers.spotify.client import HttpResponse
        from publishers.spotify.token_cache import load_spotify_token
        self.names = ["alpha"]
        self.write_source(self.names)
        self.args.max_new_searches_per_run = 0
        pauses = []
        checkpoint_delayed = False
        def response(request, parsed, query):
            if parsed.path == "/v1/search" and len(pauses) < 4:
                pauses.append(self.clock.now)
                return HttpResponse(429, {"Retry-After":"7200" if len(pauses) == 1 else "4"},
                                    '{"error":{"reason":"QUOTA_EXCEEDED"},"secret":"BODY_SENTINEL"}' if len(pauses) == 1 else "{}")
            return None
        self.before_transport = response
        real_writer = self.module.write_publish_report
        def report(path, summary):
            nonlocal checkpoint_delayed
            real_writer(path, summary)
            if summary.run_state == "waiting" and not checkpoint_delayed:
                checkpoint_delayed = True
                self.assertTrue(self.args.match_cache.exists())
                self.assertTrue(self.args.publish_state_cache.exists())
                self.clock.now += 2
        with patch.object(self.module, "write_publish_report", side_effect=report):
            summary = self.run_publish()
        self.assertEqual([entry[0] for entry in self.requests], [0,1,2,7204,7206,7208,7212,7216,7220,7228,7236,7244,7260,7276,7292,7308,7324])
        self.assertEqual(pauses, [2,7208,7220,7244])
        self.assertEqual(self.refreshes, [7202])
        saved = load_spotify_token(self.settings.token_cache_path)
        assert saved is not None
        self.assertEqual(saved.access_token, "ROTATED_SENTINEL")
        self.assertEqual((summary.request_count, summary.rate_limit_count, summary.quota_count, summary.token_refresh_count, summary.completed_pass_count, summary.wait_seconds), (17,4,1,1,1,7210))
        self.assertIn("2023-11-15T00:13:22+00:00", self.console)
        self.assertIn("7198 seconds remaining", self.console)
        self.assertNotIn("Synthetic", self.console)
        for sentinel in ("TOKEN_SENTINEL", "REFRESH_SENTINEL", "ROTATED_SENTINEL", "BODY_SENTINEL"):
            self.assertNotIn(sentinel, self.console + self.args.report.read_text())
        print("J2 trace:", json.dumps({"requests":self.requests,"pause_receipts":pauses,"refresh_starts":self.refreshes,"wait_seconds":summary.wait_seconds,"attempts":summary.request_count}))

    def test_j3_positional_pause_replans_after_remote_edit(self):
        from publishers.spotify.client import HttpResponse
        self.args.max_new_searches_per_run = 0
        self.run_publish()
        self.remote = ["spotify:track:bravo", "spotify:track:delta"]
        self.requests.clear()
        self.writes.clear()
        paused = False
        edited = False
        def response(request, parsed, query):
            nonlocal paused
            if request.method == "POST" and parsed.path.endswith("/items") and not paused:
                body = json.loads(request.body)
                if body["uris"] == ["spotify:track:charlie"]:
                    self.assertEqual(body["position"], 2)
                    paused = True
                    return HttpResponse(429, {"Retry-After":"4"}, "{}")
            return None
        def edit(seconds):
            nonlocal edited
            if not paused or edited:
                return
            self.assertEqual(self.remote, ["spotify:track:alpha", "spotify:track:bravo", "spotify:track:delta"])
            report = self.reports[-1]
            self.assertEqual(report.run_state, "waiting")
            self.assertEqual(report.added_count, 1)
            self.assertEqual(report.would_add_count, 2)
            self.assertTrue(self.args.match_cache.exists())
            self.assertTrue(self.args.publish_state_cache.exists())
            self.remote.insert(0, "spotify:track:zulu")
            edited = True
        self.before_transport = response
        self.on_sleep = edit
        summary = self.run_publish()
        self.assertTrue(edited)
        self.assertEqual(self.remote, ["spotify:track:zulu", "spotify:track:alpha", "spotify:track:bravo", "spotify:track:charlie", "spotify:track:delta", "spotify:track:echo"])
        bodies = [entry[3] for entry in self.requests if entry[1] == "POST"]
        self.assertEqual(bodies, [{"uris":["spotify:track:alpha"],"position":0}, {"uris":["spotify:track:charlie"],"position":2}, {"uris":["spotify:track:charlie"],"position":3}, {"uris":["spotify:track:echo"],"position":5}])
        self.assertEqual(summary.search_count, 0)
        self.assertEqual(summary.completed_pass_count, 1)
        self.assertEqual(sum(entry[2].endswith("/items") and entry[1]=="GET" for entry in self.requests), 2)
        print("J3 trace:", json.dumps({"requests":self.requests,"remote":self.remote,"writes":self.writes}))

    def test_budget_one_completed_no_candidate_album_with_cached_first_row(self):
        from publishers.spotify.match_cache import cache_track_match, save_spotify_track_match_cache
        self.names = ["alpha", "bravo", "charlie"]
        self.write_source(self.names, "release")
        self.args.max_new_searches_per_run = 1
        # Seed a literal first-row decision, leaving the other two genuinely uncached.
        tracks = self.module.read_playlist_tracks_from_master_paths((self.master,))
        cache = {}
        cache_track_match(cache, TrackMatchDecision(track=tracks[0], status=MATCHED, spotify_uri="spotify:track:alpha", reason="synthetic", candidate=SpotifyTrackCandidate("spotify:track:alpha", "alpha", ("Synthetic Artist",), "Synthetic Album")))
        save_spotify_track_match_cache(self.args.match_cache, cache)
        from publishers.spotify.client import HttpResponse
        self.before_transport = lambda request, parsed, query: HttpResponse(200, {}, '{"tracks":{"items":[]}}') if parsed.path == "/v1/search" and query["type"] == ["track"] else None
        summary = self.run_publish()
        self.assertEqual(summary.completed_pass_count, 3)
        self.assertEqual(summary.unmatched_count, 2)
        self.assertEqual(summary.run_status, "processing complete; matches need review")
        self.assertEqual(sum(entry[2] == "/v1/search" for entry in self.requests), 7)
        self.assertEqual(self.remote, ["spotify:track:alpha"])
        self.assertEqual(summary.stop_reason, "complete")

    def test_startup_refresh_is_included_in_invocation_total(self):
        from dataclasses import replace
        from publishers.spotify.client import HttpResponse
        from publishers.spotify.session import get_spotify_access_token
        from publishers.spotify.token_cache import save_spotify_token
        save_spotify_token(self.settings.token_cache_path, replace(self.token, expires_at=1700000000))
        startup_calls = []
        def transport(request):
            startup_calls.append(request)
            return HttpResponse(200, {}, json.dumps({"access_token":"STARTUP_TOKEN", "token_type":"Bearer", "expires_in":3600}))
        def startup(settings, force_reauthorize=False, on_refresh=None):
            return get_spotify_access_token(settings, DEFAULT_AUTHORIZE_SCOPES, now=1700000000, token_transport=transport, on_refresh=on_refresh)
        self.startup.side_effect = startup
        summary = self.run_publish()
        self.assertEqual(summary.token_refresh_count, 1)
        self.assertEqual(len(startup_calls), 1)
        self.assertEqual(self.refreshes, [])
        self.assertIn("Token refresh attempts: 1", self.args.report.read_text())

    def test_startup_pause_pins_account_before_inventory_and_rechecks_on_resume(self):
        from publishers.spotify.client import HttpResponse, SpotifyAuthenticationError
        paused = False
        def response(request, parsed, query):
            nonlocal paused
            if parsed.path == "/v1/me/playlists" and not paused:
                paused = True
                return HttpResponse(429, {"Retry-After":"4"}, "{}")
            return None
        self.before_transport = response
        self.on_sleep = lambda seconds: setattr(self, "user_id", "changed-owner") if paused else None
        with self.assertRaises(SpotifyAuthenticationError):
            self.run_publish()
        self.assertEqual([r[2] for r in self.requests], ["/v1/me", "/v1/me/playlists", "/v1/me"])
        self.assertEqual(self.writes, [])
        self.assertEqual(self.reports[-1].run_state, "needs_action")

    def test_selected_input_invalid_after_pause_stops_before_new_requests(self):
        from publishers.spotify.client import HttpResponse
        paused = False
        def response(request, parsed, query):
            nonlocal paused
            if parsed.path == "/v1/search" and not paused:
                paused = True
                return HttpResponse(429, {"Retry-After":"4"}, "{}")
            return None
        self.before_transport = response
        self.on_sleep = lambda seconds: self.master.write_text("invalid,header\n") if paused else None
        with self.assertRaises(ValueError):
            self.run_publish()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.reports[-1].run_state, "failed")

    def test_newly_discovered_input_is_not_selected_on_resume(self):
        from publishers.spotify.client import HttpResponse
        paused = False
        def response(request, parsed, query):
            nonlocal paused
            if parsed.path == "/v1/search" and not paused:
                paused = True
                return HttpResponse(429, {"Retry-After":"4"}, "{}")
            return None
        self.before_transport = response
        def new_source(seconds):
            if paused:
                write_playlist_master(self.directory / "Other" / "Other.csv", [{"Track Name":"unselected"}])
        self.on_sleep = new_source
        summary = self.run_publish()
        self.assertEqual(summary.track_count, 5)
        self.assertEqual(summary.playlist_count, 1)
        self.assertEqual(self.search_names, self.names)

    def test_quota_without_reset_stops_once_with_actionable_report(self):
        from publishers.spotify.client import HttpResponse
        self.before_transport = lambda request, parsed, query: HttpResponse(429, {}, '{"error":{"reason":"QUOTA_EXCEEDED"},"secret":"BODY_SENTINEL"}')
        with patch("sys.stderr", new_callable=io.StringIO) as error_output, self.assertRaises(SpotifyApiError):
            self.run_publish()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.control.wait_seconds, 0)
        self.assertEqual(self.reports[-1].run_state, "needs_action")
        self.assertEqual(self.reports[-1].quota_count, 1)
        self.assertIn("reset time was not provided", self.args.report.read_text() + error_output.getvalue())
        self.assertNotIn("BODY_SENTINEL", self.args.report.read_text() + error_output.getvalue())

    def test_pause_checkpoint_failure_is_terminal_without_wait_or_pending_flush(self):
        from publishers.spotify.client import HttpResponse
        self.before_transport = lambda request, parsed, query: HttpResponse(429, {"Retry-After":"4"}, "{}") if parsed.path == "/v1/search" else None
        with patch.object(self.module, "save_spotify_track_match_cache", side_effect=OSError("disk unavailable")), patch("sys.stderr", new_callable=io.StringIO), self.assertRaisesRegex(SpotifyApiError, "checkpoint failed"):
            self.run_publish()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.control.wait_seconds, 0)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.reports[-1].run_state, "failed")

    def test_wait_interrupt_retains_reason_deadline_even_if_report_write_fails(self):
        from publishers.spotify.client import HttpResponse
        self.before_transport = lambda request, parsed, query: HttpResponse(429, {"Retry-After":"7200"}, "{}")
        def interrupt(seconds):
            self.clock.now += 3
            raise KeyboardInterrupt("stopped")
        self.on_sleep = interrupt
        real_writer = self.module.write_publish_report
        real_open = io.open
        prior_report = []
        prior_files = []
        partial_writes = []
        def failing_open(*args, **kwargs):
            output = real_open(*args, **kwargs)
            real_write = output.write
            def partial_write(text):
                real_write(text[:20])
                output.flush()
                partial_writes.append(text[:20])
                raise OSError("report disk unavailable")
            output.write = partial_write
            return output
        def report(path, summary):
            if summary.run_state == "interrupted":
                prior_report.append(path.read_bytes())
                prior_files.extend(path.parent.iterdir())
                with patch("io.open", side_effect=failing_open):
                    real_writer(path, summary)
                return
            real_writer(path, summary)
        with patch.object(self.module, "write_publish_report", side_effect=report), patch("sys.stderr", new_callable=io.StringIO) as errors, self.assertRaises(KeyboardInterrupt):
            self.run_publish()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.control.wait_seconds, 3)
        self.assertEqual(len(partial_writes), 1)
        self.assertEqual(self.args.report.read_bytes(), prior_report[0])
        self.assertEqual(set(self.args.report.parent.iterdir()), set(prior_files))
        self.assertIn("2023-11-15T00:13:20+00:00", self.args.report.read_text())
        self.assertIn("2023-11-15T00:13:20+00:00", errors.getvalue())
        self.assertIn("report", errors.getvalue())

    def test_ctrl_c_during_pause_progress_update_stops_before_resume(self):
        self.assert_pause_cleanup_interrupt_stops("update")

    def test_ctrl_c_during_pause_progress_finish_stops_before_resume(self):
        self.assert_pause_cleanup_interrupt_stops("finish")

    def test_ctrl_c_during_pause_response_logging_retains_deadline(self):
        self.assert_pause_cleanup_interrupt_stops("log_debug")

    def assert_pause_cleanup_interrupt_stops(self, method):
        from publishers.spotify.client import HttpResponse
        original_run = self.module.run_spotify_publish_from_args
        self.args.progress = True
        paused = False
        interrupted = False
        def response(request, parsed, query):
            nonlocal paused
            if parsed.path == "/v1/search" and not paused:
                paused = True
                return HttpResponse(429, {"Retry-After": "7200"}, "{}")
            return None
        def cleanup(*args, **kwargs):
            nonlocal interrupted
            if paused and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
        self.before_transport = response
        with (
            patch.object(self.client if method == "log_debug" else ProgressReporter, method, side_effect=cleanup),
            patch.object(self.module, "parse_args", return_value=self.args),
            patch.object(self.module, "run_spotify_publish_from_args", side_effect=lambda args, **kwargs: original_run(args, publisher_config=PublisherConfig("spotify", "", ""), spotify_client=self.client, **kwargs)),
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO) as errors,
        ):
            result = self.module.main([])
        self.assertEqual(result, 130)
        self.assertTrue(interrupted)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.control.wait_seconds, 0)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.reports[-1].run_state, "interrupted")

        self.assertIn("2023-11-15T00:13:22+00:00", self.args.report.read_text())
        self.assertIn("2023-11-15T00:13:22+00:00", errors.getvalue())


    def test_search_api_failure_stops_once_without_search_storm(self):
        from publishers.spotify.client import HttpResponse
        self.before_transport = lambda request, parsed, query: HttpResponse(500, {}, "BODY_SENTINEL") if parsed.path == "/v1/search" else None
        with self.assertRaises(SpotifyApiError):
            self.run_publish()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.writes, [])
        self.assertNotIn("BODY_SENTINEL", self.console + self.args.report.read_text())
        self.assertEqual(self.reports[-1].run_state, "failed")

    def test_album_attempt_full_key_invalidation_and_interrupted_attempt_retry(self):
        from dataclasses import replace
        from publishers.spotify.client import HttpResponse, SpotifyRateLimitPauseError
        from publishers.spotify.match_cache import cache_track_match, save_spotify_track_match_cache
        from publishers.spotify.matching import UNMATCHED
        self.names = ["alpha", "bravo", "charlie"]
        self.write_source(self.names, "release")
        attempts = set()
        album_calls = []
        interrupt = False
        def response(request, parsed, query):
            if parsed.path == "/v1/search" and query["type"] == ["album"]:
                album_calls.append(query["q"])
                if interrupt:
                    return HttpResponse(429, {"Retry-After":"4"}, "{}")
            return None
        self.before_transport = response
        def run_pass(index, limit=10, candidate=None):
            match_path = self.directory / f"match-{index}.json"
            if candidate:
                tracks = self.module.read_playlist_tracks_from_master_paths((self.master,))
                cache = {}
                cache_track_match(cache, TrackMatchDecision(tracks[0], UNMATCHED, "", "synthetic", review_candidates=(candidate,)))
                save_spotify_track_match_cache(match_path, cache)
            return self.module.publish_spotify_playlists(self.directory, self.args.report, self.client, "TOKEN_SENTINEL",
                        playlist_master_paths=(self.master,), match_cache_path=match_path,
                        publish_state_cache_path=self.args.publish_state_cache, apply=False,
                        search_limit=limit, max_new_searches_per_run=1, completed_album_attempts=attempts)
        first = run_pass(1)
        self.assertEqual((first.stop_reason, first.track_count, first.resume_progress_made), ("search_budget",0,True))
        second = run_pass(2)
        self.assertEqual((second.stop_reason, second.track_count), ("search_budget",1))
        self.assertEqual(len(album_calls), 1)
        run_pass(3, limit=9)
        with patch.object(self.module, "MATCHER_VERSION", MATCHER_VERSION+1):
            run_pass(4)
        candidate = SpotifyTrackCandidate("spotify:track:hint", "hint-one", ("Synthetic Artist",), "Synthetic Album")
        run_pass(5, candidate=candidate)
        run_pass(6, candidate=replace(candidate, name="hint-two"))
        self.master.write_text(self.master.read_text().replace("Synthetic Album", "Changed Album"))
        run_pass(7)
        self.assertEqual(len(album_calls), 6)
        self.assertEqual(len(attempts), 6)
        attempts.clear()
        interrupt = True
        self.control.continuous = True
        with self.assertRaises(SpotifyRateLimitPauseError):
            run_pass(8)
        self.assertEqual(attempts, set())
        interrupt = False
        run_pass(9)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(album_calls), 8)

    def test_create_pause_resumes_from_inventory_without_repeating_searches(self):
        from publishers.spotify.client import HttpResponse
        paused = False
        def response(request, parsed, query):
            nonlocal paused
            if request.method == "POST" and parsed.path == "/v1/me/playlists" and not paused:
                paused = True
                return HttpResponse(429, {"Retry-After":"4"}, "{}")
            return None
        self.before_transport = response
        self.args.max_new_searches_per_run = 0
        summary = self.run_publish()
        self.assertEqual(self.search_names, self.names)
        self.assertEqual(len(self.remote), 5)
        self.assertEqual(summary.completed_pass_count, 1)
        self.assertEqual(sum(r[2] == "/v1/me/playlists" and r[1] == "GET" for r in self.requests), 2)
        self.assertEqual(sum(r[1] == "POST" for r in self.requests), 3)

    def test_album_page_pause_retries_incomplete_attempt_before_retaining(self):
        from publishers.spotify.client import HttpResponse
        self.names = ["alpha", "bravo", "charlie"]
        self.write_source(self.names, "release")
        self.args.max_new_searches_per_run = 0
        pages = []
        album_searches = []
        def response(request, parsed, query):
            if parsed.path == "/v1/search" and query["type"] == ["album"]:
                album_searches.append(self.clock.now)
                return HttpResponse(200, {}, json.dumps({"albums":{"items":[{"id":"album", "name":"Synthetic Album", "artists":[{"name":"Synthetic Artist"}], "total_tracks":3}]}}))
            if parsed.path == "/v1/albums/album/tracks":
                pages.append(self.clock.now)
                if len(pages) == 1:
                    return HttpResponse(429, {"Retry-After":"4"}, "{}")
                return HttpResponse(200, {}, json.dumps({"items":[dict(self.track_payload(name), track_number=i, disc_number=1) for i,name in enumerate(self.names,1)], "next":None}))
            return None
        self.before_transport = response
        summary = self.run_publish()
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(album_searches), 2)
        self.assertEqual(self.search_names, [])
        self.assertEqual(self.remote, ["spotify:track:alpha", "spotify:track:bravo", "spotify:track:charlie"])
        self.assertEqual(summary.completed_pass_count, 1)

    def test_second_append_batch_pause_retains_first_hundred_without_flushing_pending(self):
        from publishers.spotify.client import HttpResponse
        self.names = [f"song{index:03}" for index in range(101)]
        self.write_source(self.names)
        self.args.max_new_searches_per_run = 0
        paused = False
        observed = False
        def response(request, parsed, query):
            nonlocal paused
            if request.method == "POST" and parsed.path.endswith("/items") and len(self.remote) == 100 and not paused:
                paused = True
                return HttpResponse(429, {"Retry-After":"4"}, "{}")
            return None
        def observe(seconds):
            nonlocal observed
            if paused and not observed:
                self.assertEqual(self.remote, [f"spotify:track:song{index:03}" for index in range(100)])
                self.assertEqual(self.reports[-1].added_count, 100)
                self.assertEqual(self.reports[-1].would_add_count, 1)
                observed = True
        self.before_transport = response
        self.on_sleep = observe
        summary = self.run_publish()
        self.assertTrue(observed)
        self.assertEqual(self.remote, [f"spotify:track:song{index:03}" for index in range(101)])
        self.assertEqual(len(self.search_names), 101)
        self.assertEqual(summary.cache_hit_count, 101)
        self.assertEqual(summary.completed_pass_count, 1)
        self.assertEqual([len(r[3]["uris"]) for r in self.writes if r[2].endswith("/items")], [100,1])

    def test_no_cacheable_progress_budget_stops_without_another_pass(self):
        with patch.object(self.module, "cache_track_match"), self.assertRaisesRegex(SpotifyApiError, "no retained progress"):
            self.run_publish()
        self.assertEqual(self.search_names, ["alpha", "bravo"])
        self.assertEqual(self.reports[-1].run_state, "failed")

    def test_main_connected_completion_error_and_wait_interrupt_exit_codes(self):
        from publishers.spotify.client import HttpResponse
        original = self.module.run_spotify_publish_from_args
        def run(args, **kwargs):
            return original(args, publisher_config=PublisherConfig("spotify", "", ""), spotify_client=self.client, **kwargs)
        argv = self.module.build_spotify_publisher_argv(continuous=True, playlist_output_dir=self.directory,
            report=self.args.report, token_cache=self.args.token_cache, match_cache=self.args.match_cache,
            publish_state_cache=self.args.publish_state_cache, no_progress=True, dry_run=True,
            max_new_searches_per_run=0)
        with patch.object(self.module, "run_spotify_publish_from_args", side_effect=run), patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO) as errors:
            self.assertEqual(self.module.main(argv), 0)
            self.before_transport = lambda request, parsed, query: HttpResponse(500, {}, "BODY_SENTINEL")
            self.assertEqual(self.module.main(argv), 1)
            self.before_transport = lambda request, parsed, query: HttpResponse(429, {"Retry-After":"7200"}, "{}")
            def interrupt(seconds):
                if self.reports[-1].run_state == "waiting":
                    raise KeyboardInterrupt
            self.on_sleep = interrupt
            self.assertEqual(self.module.main(argv), 130)
        report = self.args.report.read_text()
        self.assertEqual(self.reports[-1].run_state, "interrupted")
        self.assertIn("Resume at:", report)
        self.assertIn("Interrupted:", errors.getvalue())
        self.assertNotIn("BODY_SENTINEL", output.getvalue() + errors.getvalue() + report)
        self.assertEqual(self.writes, [])

    def test_exact_last_row_at_cap_completes_without_an_extra_pass(self):
        self.names = ["alpha"]
        self.write_source(self.names)
        self.args.max_new_searches_per_run = 1
        summary = self.run_publish()
        self.assertEqual(summary.stop_reason, "complete")
        self.assertEqual(summary.completed_pass_count, 1)
        self.assertEqual(summary.track_count, 1)
        self.assertEqual(sum(r[2] == "/v1/me" for r in self.requests), 1)

    def test_fallback_resets_only_after_retained_pass_progress(self):
        from publishers.spotify.client import HttpResponse
        pauses = 0
        after_progress = False
        def response(request, parsed, query):
            nonlocal pauses, after_progress
            if parsed.path == "/v1/search":
                if pauses < 5:
                    pauses += 1
                    return HttpResponse(429, {}, "[]")
                if len(self.remote) == 2 and not after_progress:
                    after_progress = True
                    return HttpResponse(429, {}, "{}")
            return None
        self.before_transport = response
        summary = self.run_publish()
        self.assertEqual(summary.wait_seconds, 1860)
        self.assertEqual(summary.rate_limit_count, 6)
        self.assertEqual(summary.quota_count, 0)
        self.assertEqual(summary.completed_pass_count, 3)
        self.assertEqual(self.control.minimum_interval_seconds, 30)

    def test_continuous_fake_client_does_not_invent_transport_metrics(self):
        summary = self.module.run_spotify_publish_from_args(self.args, playlist_master_paths=(),
            publisher_config=PublisherConfig("spotify", "", ""), spotify_client=PublishingSpotifyClient({}))
        self.assertIsNone(summary.request_count)
        self.assertIsNone(summary.rate_limit_count)
        self.assertEqual(summary.completed_pass_count, 1)

    def test_failed_pause_report_still_prints_deadline_and_never_waits(self):
        from publishers.spotify.client import HttpResponse
        self.before_transport = lambda request, parsed, query: HttpResponse(429, {"Retry-After":"7200"}, "{}")
        with patch.object(self.module, "write_publish_report", side_effect=OSError("report unavailable")), patch("sys.stderr", new_callable=io.StringIO) as errors, self.assertRaises(SpotifyApiError):
            self.run_publish()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.control.wait_seconds, 0)
        self.assertIn("2023-11-15T00:13:20+00:00", errors.getvalue())

    def test_mixed_malformed_remote_page_stops_before_search_or_write(self):
        from publishers.spotify.client import HttpResponse
        self.created = True
        self.remote = ["spotify:track:alpha"]
        missing_uri = self.track_payload("bravo")
        del missing_uri["uri"]
        def response(request, parsed, query):
            if request.method == "GET" and parsed.path.endswith("/items"):
                return HttpResponse(200, {}, json.dumps({"items":[{"track":self.track_payload("alpha")}, malformed], "next":None}))
            return None
        self.before_transport = response
        for malformed in ({"track": missing_uri}, {"track": {"type": "track"}},
                          "malformed", {}, {"track": {}}, {"track": "malformed"}, {"item": "malformed"}):
            with self.subTest(malformed=malformed):
                self.requests.clear()
                with self.assertRaisesRegex(SpotifyApiError, "Malformed Spotify playlist item"):
                    self.run_publish()
                self.assertEqual(len(self.requests), 3)
                self.assertEqual(self.search_names, [])
                self.assertEqual(self.writes, [])
                self.assertEqual(self.reports[-1].run_state, "failed")

    def test_remote_null_and_nontrack_entries_keep_existing_handling(self):
        from publishers.spotify.client import HttpResponse
        self.names = ["alpha"]
        self.write_source(self.names)
        self.created = True
        self.remote = ["spotify:track:alpha"]
        def response(request, parsed, query):
            if request.method == "GET" and parsed.path.endswith("/items"):
                return HttpResponse(200, {}, json.dumps({"items":[{"track":self.track_payload("alpha")}, {"track":None}, {"item":None}, {"item":{"type":"episode","uri":"spotify:episode:synthetic","name":"Synthetic Episode"}}], "next":None}))
            return None
        self.before_transport = response
        summary = self.run_publish()
        self.assertEqual(summary.run_state, "complete")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.remote, ["spotify:track:alpha"])

    def test_bounded_final_report_and_console_are_complete_with_processing_checkpoints(self):
        self.names = ["alpha"]
        self.write_source(self.names)
        self.args.continuous = False
        self.args.max_new_searches_per_run = 0
        for apply, mode in ((True,"append"), (False,"append"), (True,"replace")):
            with self.subTest(apply=apply, mode=mode):
                self.args.apply = apply
                self.args.dry_run = not apply
                self.args.publisher_sync_mode = mode
                self.created = False
                self.remote.clear()
                self.reports.clear()
                summary = self.run_publish()
                self.assertEqual(summary.run_state, "complete")
                self.assertIn("- Run state: complete", self.args.report.read_text())
                if apply:
                    self.assertTrue(any(report.run_state == "processing" for report in self.reports[:-1]))
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.module.print_summary(summary)
                self.assertIn("Run state: complete", output.getvalue())
                self.assertNotIn("Run state: processing", output.getvalue())

    def test_builders_and_parser_continuous_defaults(self):
        args = self.module.parse_args(self.module.build_spotify_publisher_argv(continuous=True, no_progress=True))
        self.assertTrue(args.continuous)
        self.assertEqual(args.request_interval_seconds, 1.0)
        self.assertEqual(self.module.parse_args([]).request_interval_seconds, 0.0)
        self.assertFalse(self.module.parse_args([]).continuous)



if __name__ == "__main__":
    unittest.main()
