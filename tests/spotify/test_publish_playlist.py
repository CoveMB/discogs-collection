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
    SpotifyApiError,
    SpotifyPlaylist,
    SpotifyPlaylistItem,
    SpotifyRateLimitDeferredError,
    SpotifyRateLimitRetriesExhaustedError,
)
from publishers.spotify.env import SpotifySettings
from publishers.spotify.matching import PlaylistTrack, SpotifyTrackCandidate
from publishers.spotify.authorization_flow import DEFAULT_AUTHORIZE_SCOPES
from publishers.spotify.publish_playlist import dry_run_spotify_playlist_publish, publish_spotify_playlists
from publishers.spotify.publish_state import record_spotify_publish_state_track, save_spotify_publish_state
from discogs_playlist_exporter import safe_playlist_filename
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


class FlakySpotifyClient:
    def search_tracks(self, access_token, query, limit=10):
        if "Alpha One" in query:
            raise SpotifyApiError("Spotify search failed with status 500: temporary failure")
        return (
            SpotifyTrackCandidate(
                uri="spotify:track:beta",
                name="Beta One",
                artists=("Beta Artist",),
                album_name="Beta Album",
            ),
        )


class DeferredRateLimitSpotifyClient:
    def __init__(self):
        self.searches = []

    def search_tracks(self, access_token, query, limit=10):
        self.searches.append((access_token, query, limit))
        raise SpotifyRateLimitDeferredError(retry_after_seconds=9999, max_wait_seconds=480)


class MultilineErrorSpotifyClient:
    def search_tracks(self, access_token, query, limit=10):
        raise SpotifyApiError('Spotify search failed with status 429: {\n  "error": "Too many requests"\n}')


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
        return ("snapshot-add",) if uris else ()

    def replace_playlist_items(self, access_token, playlist_id, uris):
        self.replace_calls.append((playlist_id, tuple(uris)))
        self.playlist_items_by_id[playlist_id] = [
            self.playlist_item_for_uri(uri)
            for uri in uris
        ]
        return "snapshot-replace"

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
        return super().add_playlist_items(access_token, playlist_id, uris, position=position)


class SearchErrorPublishingClient(PublishingSpotifyClient):
    def search_tracks(self, access_token, query, limit=10):
        if "Beta One" in query:
            raise SpotifyApiError("Spotify search failed with status 500: temporary failure")
        return super().search_tracks(access_token, query, limit)


class FailingReplaceRemainderPublishClient(PublishingSpotifyClient):
    def add_playlist_items(self, access_token, playlist_id, uris, position=None):
        if self.replace_calls:
            raise SpotifyApiError("Spotify playlist add items failed with status 500: temporary failure")
        return super().add_playlist_items(access_token, playlist_id, uris, position=position)


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
        return ("snapshot-add",) if uris else ()

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


def playlist_row(release_id: str, album_name: str, track_name: str, artist_name: str) -> dict[str, str]:
    return {
        "Release Id": release_id,
        "Album Name": album_name,
        "Track Number": "1",
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


def playlist_track(playlist_name: str, release_id: str, album_name: str, track_name: str, artist_name: str) -> PlaylistTrack:
    return PlaylistTrack(
        playlist_name=playlist_name,
        release_id=release_id,
        album_name=album_name,
        track_number="1",
        track_name=track_name,
        artist_name=artist_name,
        spotify_search_query=f"{artist_name} {track_name} {album_name}",
    )


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

    def test_dry_run_reports_row_progress_while_searching_spotify_tracks(self):
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
            report_path = directory / "reports" / "spotify-dry-run.txt"
            client = FakeSpotifyClient({})
            progress_stream = TerminalStream()
            progress = ProgressReporter(stream=progress_stream, label="Searching Spotify tracks")

            dry_run_spotify_playlist_publish(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                progress=progress,
            )

            progress_text = progress_stream.getvalue()
            self.assertIn("\rSearching Spotify tracks [", progress_text)
            self.assertIn("1/2", progress_text)
            self.assertIn("50%", progress_text)
            self.assertIn("2/2", progress_text)
            self.assertIn("100%", progress_text)
            self.assertTrue(progress_text.endswith("\n"))

    def test_dry_run_reads_playlist_csvs_matches_tracks_and_writes_review_report(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Discogs - Breakbeat" / "Discogs - Breakbeat.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-dry-run.txt"
            client = FakeSpotifyClient(
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

            summary = dry_run_spotify_playlist_publish(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
            )

            self.assertEqual(summary.playlist_count, 1)
            self.assertEqual(summary.track_count, 1)
            self.assertEqual(summary.matched_count, 1)
            self.assertEqual(summary.unmatched_count, 0)
            self.assertEqual(summary.ambiguous_count, 0)
            self.assertEqual(
                client.searches,
                [('access-token', 'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"', 10)],
            )
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Spotify playlist dry-run report", report_text)
            self.assertIn("Matched tracks: 1", report_text)
            self.assertIn("Discogs - Breakbeat | 111 | 1 | Alpha Artist | Alpha One | matched | spotify:track:alpha", report_text)

    def test_dry_run_records_search_errors_and_still_writes_report(self):
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
            report_path = directory / "reports" / "spotify-dry-run.txt"

            summary = dry_run_spotify_playlist_publish(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=FlakySpotifyClient(),
                access_token="access-token",
            )

            self.assertEqual(summary.track_count, 2)
            self.assertEqual(summary.matched_count, 1)
            self.assertEqual(summary.error_count, 1)
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Search errors: 1", report_text)
            self.assertIn("Discogs - Breakbeat | 111 | 1 | Alpha Artist | Alpha One | error | no Spotify URI", report_text)
            self.assertIn("Search errors", report_text)
            self.assertLess(report_text.index("Search errors\n-------------"), report_text.index("Track match decisions\n---------------------"))
            self.assertIn('Search query: track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"', report_text)
            self.assertIn("Error: Spotify search failed with status 500: temporary failure", report_text)
            self.assertIn("temporary failure", report_text)
            self.assertIn("Discogs - Breakbeat | 222 | 1 | Beta Artist | Beta One | matched | spotify:track:beta", report_text)

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
        self.assertEqual(first_summary.search_count, 4)
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
                                "matcher_version": 1,
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
                                "matcher_version": 1,
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

        self.assertEqual(len(client.searches), 3)
        self.assertEqual(summary.search_count, 3)
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
        self.assertIn("Closest Spotify result: spotify:track:wrong | Alpha Artist | Wrong Track | Alpha Album", report_text)
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
        self.assertIn("Run status: checkpoint - append progress saved after 100 searches; run still in progress", report_text)
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

    def test_publish_uses_discogs_prefix_when_config_is_omitted(self):
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

        self.assertEqual(client.created_playlists, [("Discogs - House", False, "Generated from Discogs collection")])

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
        self.assertIn("publishing failed: Spotify playlist add items failed with status 500: temporary failure", report_text)

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

    def test_dry_run_report_keeps_multiline_search_errors_readable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist")],
            )
            report_path = directory / "reports" / "spotify-dry-run.txt"

            dry_run_spotify_playlist_publish(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=MultilineErrorSpotifyClient(),
                access_token="access-token",
            )

            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn('Error: Spotify search failed with status 429: { "error": "Too many requests" }', report_text)
            self.assertNotIn('Error: Spotify search failed with status 429: {\n', report_text)

    def test_dry_run_stops_when_spotify_defers_rate_limit_for_too_long(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "Deep Techno" / "Deep Techno.csv",
                [
                    playlist_row("111", "Alpha Album", "Alpha One", "Alpha Artist"),
                    playlist_row("222", "Beta Album", "Beta One", "Beta Artist"),
                ],
            )
            report_path = directory / "reports" / "spotify-dry-run.txt"
            client = DeferredRateLimitSpotifyClient()
            debug_lines = []

            with self.assertRaisesRegex(SpotifyRateLimitDeferredError, "Retry-After is 2 hours 46 minutes 39 seconds"):
                dry_run_spotify_playlist_publish(
                    playlist_output_directory=playlist_directory,
                    report_path=report_path,
                    spotify_client=client,
                    access_token="access-token",
                    debug_log=debug_lines.append,
                )

            self.assertEqual(len(client.searches), 1)
            self.assertFalse(report_path.exists())
            self.assertIn("track_search_deferred index=1", debug_lines)

    def test_dry_run_report_details_ambiguous_and_unmatched_tracks_for_manual_review(self):
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
            report_path = directory / "reports" / "spotify-dry-run.txt"
            client = FakeSpotifyClient(
                {
                    'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha-1",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                        SpotifyTrackCandidate(
                            uri="spotify:track:alpha-2",
                            name="Alpha One",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                        ),
                    ),
                    'track:"Beta One" artist:"Beta Artist" album:"Beta Album"': (
                        SpotifyTrackCandidate(
                            uri="spotify:track:beta-two",
                            name="Beta Two",
                            artists=("Beta Artist",),
                            album_name="Beta Album",
                        ),
                    ),
                }
            )

            summary = dry_run_spotify_playlist_publish(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
            )

            self.assertEqual(summary.ambiguous_count, 1)
            self.assertEqual(summary.unmatched_count, 1)
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Ambiguous tracks needing review", report_text)
            self.assertIn("Discogs - Breakbeat | 111 | 1 | Alpha Artist | Alpha One | Alpha Album", report_text)
            self.assertIn('Search query: track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"', report_text)
            self.assertIn("Why: 2 candidates matched track, artist, and album", report_text)
            self.assertIn("spotify:track:alpha-1 | Alpha Artist | Alpha One | Alpha Album", report_text)
            self.assertIn("spotify:track:alpha-2 | Alpha Artist | Alpha One | Alpha Album", report_text)
            self.assertIn("Unmatched tracks needing review", report_text)
            self.assertIn("Discogs - Breakbeat | 222 | 1 | Beta Artist | Beta One | Beta Album", report_text)
            self.assertIn('Search query: track:"Beta One" artist:"Beta Artist" album:"Beta Album"', report_text)
            self.assertIn("Why: no candidates matched track, artist, and album", report_text)
            self.assertIn("Closest Spotify result: spotify:track:beta-two | Beta Artist | Beta Two | Beta Album", report_text)
            self.assertIn("Track name: different (Discogs: Beta One; Spotify: Beta Two)", report_text)

    def test_dry_run_only_reads_selected_playlist_tracks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(
                playlist_directory / "House" / "House.csv",
                [playlist_row("111", "House Album", "House One", "House Artist")],
            )
            write_playlist_master(
                playlist_directory / "Techno" / "Techno.csv",
                [playlist_row("222", "Techno Album", "Techno One", "Techno Artist")],
            )
            report_path = directory / "reports" / "spotify-dry-run.txt"
            client = FakeSpotifyClient({})

            summary = dry_run_spotify_playlist_publish(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                playlist_selectors=("House",),
            )

        self.assertEqual(summary.track_count, 1)
        self.assertEqual(len(client.searches), 3)
        self.assertIn('track:"House One"', client.searches[0][1])
        self.assertNotIn("Techno One", " ".join(search[1] for search in client.searches))

    def test_dry_run_accepts_multiple_playlist_selectors_by_display_name_and_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            breakbeat_folder = playlist_directory / safe_playlist_filename('Discogs: "Breakbeat"')
            house_folder = playlist_directory / "House"
            write_playlist_master(
                breakbeat_folder / f"{breakbeat_folder.name}.csv",
                [playlist_row("111", "Breakbeat Album", "Breakbeat One", "Breakbeat Artist")],
            )
            write_playlist_master(
                house_folder / "House.csv",
                [playlist_row("222", "House Album", "House One", "House Artist")],
            )
            report_path = directory / "reports" / "spotify-dry-run.txt"
            client = FakeSpotifyClient({})

            summary = dry_run_spotify_playlist_publish(
                playlist_output_directory=playlist_directory,
                report_path=report_path,
                spotify_client=client,
                access_token="access-token",
                playlist_selectors=('Discogs: "Breakbeat"', str(house_folder / "House.csv")),
            )

        self.assertEqual(summary.track_count, 2)
        self.assertEqual(len(client.searches), 6)

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


if __name__ == "__main__":
    unittest.main()
