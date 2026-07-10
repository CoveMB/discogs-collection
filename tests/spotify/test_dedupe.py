import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.client import SpotifyApiError, SpotifyPlaylist, SpotifyPlaylistItem  # noqa: E402
from publishers.spotify.dedupe import (  # noqa: E402
    dedupe_spotify_managed_playlists,
    publisher_local_name_from_target,
)
from shared.publisher_config import PublisherConfig  # noqa: E402


class FakeSpotifyDedupeClient:
    def __init__(self, playlists=(), playlist_items_by_id=None, current_user_id="current-user"):
        self.playlists = tuple(playlists)
        self.playlist_items_by_id = {
            playlist_id: list(items)
            for playlist_id, items in (playlist_items_by_id or {}).items()
        }
        self.current_user_id = current_user_id
        self.remove_calls = []
        self.replace_calls = []
        self.add_calls = []
        self.item_fetch_calls = []

    def list_current_user_playlists(self, access_token):
        return self.playlists

    def get_current_user_id(self, access_token):
        return self.current_user_id

    def get_playlist_items(self, access_token, playlist_id):
        self.item_fetch_calls.append(playlist_id)
        return tuple(self.playlist_items_by_id.get(playlist_id, ()))

    def remove_playlist_items(self, access_token, playlist_id, items, snapshot_id=""):
        self.remove_calls.append((playlist_id, tuple(items), snapshot_id))
        removed_positions = {item.position for item in items}
        self.playlist_items_by_id[playlist_id] = [
            item
            for item in self.playlist_items_by_id.get(playlist_id, ())
            if item.position not in removed_positions
        ]
        return f"{snapshot_id}-after-remove"

    def replace_playlist_items(self, access_token, playlist_id, uris):
        self.replace_calls.append((playlist_id, tuple(uris)))
        self.playlist_items_by_id[playlist_id] = list(
            SpotifyPlaylistItem(uri=uri, name="", artists=(), album_name="", position=position)
            for position, uri in enumerate(uris)
        )
        return "snapshot-after-replace"

    def add_playlist_items(self, access_token, playlist_id, uris):
        self.add_calls.append((playlist_id, tuple(uris)))
        existing_items = tuple(self.playlist_items_by_id.get(playlist_id, ()))
        appended_items = tuple(
            SpotifyPlaylistItem(uri=uri, name="", artists=(), album_name="", position=len(existing_items) + offset)
            for offset, uri in enumerate(uris)
        )
        self.playlist_items_by_id[playlist_id] = list(existing_items + appended_items)
        return ("snapshot-after-add",) if uris else ()


class SpotifyAllUriRemoveDedupeClient(FakeSpotifyDedupeClient):
    def remove_playlist_items(self, access_token, playlist_id, items, snapshot_id=""):
        self.remove_calls.append((playlist_id, tuple(items), snapshot_id))
        removed_uris = {item.uri for item in items}
        self.playlist_items_by_id[playlist_id] = [
            item
            for item in self.playlist_items_by_id.get(playlist_id, ())
            if item.uri not in removed_uris
        ]
        return f"{snapshot_id}-after-remove"


class FailingAddSpotifyDedupeClient(FakeSpotifyDedupeClient):
    def add_playlist_items(self, access_token, playlist_id, uris):
        self.add_calls.append((playlist_id, tuple(uris)))
        raise SpotifyApiError("append after replace failed")


class SpotifyDedupeTests(unittest.TestCase):
    def test_publisher_local_name_from_target_requires_non_empty_managed_name(self):
        config = PublisherConfig(default_publisher="spotify", playlist_prefix="Discogs - ", playlist_suffix="")

        self.assertEqual(publisher_local_name_from_target("Discogs - House", config), "House")
        self.assertIsNone(publisher_local_name_from_target("House", config))
        self.assertIsNone(publisher_local_name_from_target("Discogs - ", config))

    def test_dry_run_filters_to_owned_private_repo_managed_playlists(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = FakeSpotifyDedupeClient(
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                        collaborative=False,
                        snapshot_id="snapshot-house",
                    ),
                    SpotifyPlaylist(
                        playlist_id="playlist-other-owner",
                        name="Discogs - Techno",
                        url="",
                        owner_id="other-user",
                        public=False,
                    ),
                    SpotifyPlaylist(
                        playlist_id="playlist-unmanaged",
                        name="Manual Playlist",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                    SpotifyPlaylist(
                        playlist_id="playlist-public",
                        name="Discogs - Public",
                        url="",
                        owner_id="current-user",
                        public=True,
                    ),
                ),
                playlist_items_by_id={
                    "playlist-house": (
                        SpotifyPlaylistItem(
                            uri="spotify:track:alpha",
                            name="Alpha",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                            added_at="2026-01-01T00:00:00Z",
                            position=0,
                        ),
                        SpotifyPlaylistItem(
                            uri="spotify:track:alpha",
                            name="Alpha",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                            added_at="2026-01-02T00:00:00Z",
                            position=1,
                        ),
                    )
                },
            )

            summary = dedupe_spotify_managed_playlists(
                spotify_client=client,
                access_token="access-token",
                report_path=report_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
            )

            self.assertEqual(summary.provider_playlist_count, 4)
            self.assertEqual(summary.eligible_playlist_count, 1)
            self.assertEqual(summary.track_count, 2)
            self.assertEqual(summary.duplicate_count, 1)
            self.assertEqual(summary.removed_count, 0)
            self.assertEqual(client.remove_calls, [])
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Spotify managed playlist dedupe dry-run report", report_text)
            self.assertIn("Eligible playlists: 1", report_text)
            self.assertIn("Skipped playlists: 3", report_text)
            self.assertIn("Discogs - House | remove | 2 | spotify:track:alpha", report_text)
            self.assertIn("Manual Playlist | skipped | not_publisher_managed", report_text)

    def test_unknown_privacy_status_is_skipped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = FakeSpotifyDedupeClient(
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=None,
                    ),
                )
            )

            summary = dedupe_spotify_managed_playlists(
                spotify_client=client,
                access_token="access-token",
                report_path=report_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
            )

            self.assertEqual(summary.eligible_playlist_count, 0)
            self.assertEqual(summary.skipped_playlist_count, 1)
            self.assertEqual(summary.skipped_playlists[0].reason, "not_private_playlist")

    def test_playlist_selectors_limit_dry_run_to_multiple_local_playlist_names(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = FakeSpotifyDedupeClient(
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                    SpotifyPlaylist(
                        playlist_id="playlist-techno",
                        name="Discogs - Techno",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                    SpotifyPlaylist(
                        playlist_id="playlist-breakbeat",
                        name="Discogs - Breakbeat",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                ),
                playlist_items_by_id={
                    "playlist-house": (
                        SpotifyPlaylistItem(uri="spotify:track:house", name="House", artists=("Artist",), album_name="Album", position=0),
                        SpotifyPlaylistItem(uri="spotify:track:house", name="House", artists=("Artist",), album_name="Album", position=1),
                    ),
                    "playlist-techno": (
                        SpotifyPlaylistItem(uri="spotify:track:techno", name="Techno", artists=("Artist",), album_name="Album", position=0),
                        SpotifyPlaylistItem(uri="spotify:track:techno", name="Techno", artists=("Artist",), album_name="Album", position=1),
                    ),
                    "playlist-breakbeat": (
                        SpotifyPlaylistItem(uri="spotify:track:breakbeat", name="Breakbeat", artists=("Artist",), album_name="Album", position=0),
                        SpotifyPlaylistItem(uri="spotify:track:breakbeat", name="Breakbeat", artists=("Artist",), album_name="Album", position=1),
                    ),
                },
            )

            summary = dedupe_spotify_managed_playlists(
                spotify_client=client,
                access_token="access-token",
                report_path=report_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                playlist_selectors=("House", "Techno"),
            )

            self.assertEqual(summary.provider_playlist_count, 3)
            self.assertEqual(summary.eligible_playlist_count, 2)
            self.assertEqual(summary.track_count, 4)
            self.assertEqual(summary.duplicate_count, 2)
            self.assertEqual(client.item_fetch_calls, ["playlist-house", "playlist-techno"])
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Playlist selectors: House, Techno", report_text)
            self.assertIn("Selected playlists: Discogs - House, Discogs - Techno", report_text)
            self.assertIn("Other eligible playlists not checked: 1", report_text)
            self.assertIn("Discogs - House | remove | 2 | spotify:track:house", report_text)
            self.assertIn("Discogs - Techno | remove | 2 | spotify:track:techno", report_text)
            self.assertNotIn("spotify:track:breakbeat", report_text)

    def test_playlist_selectors_deduplicate_multiple_selectors_for_same_playlist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = FakeSpotifyDedupeClient(
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
                        SpotifyPlaylistItem(uri="spotify:track:house", name="House", artists=("Artist",), album_name="Album", position=0),
                    ),
                },
            )

            summary = dedupe_spotify_managed_playlists(
                spotify_client=client,
                access_token="access-token",
                report_path=report_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                playlist_selectors=("House", "Discogs - House", "playlist-house"),
            )

            self.assertEqual(summary.eligible_playlist_count, 1)
            self.assertEqual(client.item_fetch_calls, ["playlist-house"])

    def test_playlist_selector_accepts_target_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = FakeSpotifyDedupeClient(
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
                        SpotifyPlaylistItem(uri="spotify:track:house", name="House", artists=("Artist",), album_name="Album", position=0),
                    ),
                },
            )

            summary = dedupe_spotify_managed_playlists(
                spotify_client=client,
                access_token="access-token",
                report_path=report_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                playlist_selectors=("Discogs - House",),
            )

            self.assertEqual(summary.eligible_playlist_count, 1)
            self.assertEqual(client.item_fetch_calls, ["playlist-house"])

    def test_playlist_selector_accepts_playlist_id(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = FakeSpotifyDedupeClient(
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
                        SpotifyPlaylistItem(
                            uri="spotify:track:house",
                            name="House",
                            artists=("Artist",),
                            album_name="Album",
                            position=0,
                        ),
                    ),
                },
            )

            summary = dedupe_spotify_managed_playlists(
                spotify_client=client,
                access_token="access-token",
                report_path=report_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=False,
                playlist_selectors=("playlist-house",),
            )

            self.assertEqual(summary.eligible_playlist_count, 1)
            self.assertEqual(client.item_fetch_calls, ["playlist-house"])

    def test_playlist_selector_rejects_skipped_playlist_match(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = FakeSpotifyDedupeClient(
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-manual",
                        name="Manual Playlist",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                )
            )

            with self.assertRaisesRegex(ValueError, "not_publisher_managed"):
                dedupe_spotify_managed_playlists(
                    spotify_client=client,
                    access_token="access-token",
                    report_path=report_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    apply=False,
                    playlist_selectors=("Manual Playlist",),
                )

            self.assertEqual(client.item_fetch_calls, [])

    def test_playlist_selector_rejects_ambiguous_eligible_matches(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = FakeSpotifyDedupeClient(
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house-a",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                    SpotifyPlaylist(
                        playlist_id="playlist-house-b",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                )
            )

            with self.assertRaisesRegex(ValueError, "ambiguous"):
                dedupe_spotify_managed_playlists(
                    spotify_client=client,
                    access_token="access-token",
                    report_path=report_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    apply=False,
                    playlist_selectors=("House",),
                )

            self.assertEqual(client.item_fetch_calls, [])

    def test_apply_replaces_playlist_with_deduped_final_uri_order(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = SpotifyAllUriRemoveDedupeClient(
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                        snapshot_id="snapshot-house",
                    ),
                ),
                playlist_items_by_id={
                    "playlist-house": (
                        SpotifyPlaylistItem(
                            uri="spotify:track:alpha",
                            name="Alpha",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                            added_at="2026-01-01T00:00:00Z",
                            position=0,
                        ),
                        SpotifyPlaylistItem(
                            uri="spotify:track:alpha",
                            name="Alpha",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                            added_at="2026-01-02T00:00:00Z",
                            position=1,
                        ),
                        SpotifyPlaylistItem(
                            uri="spotify:track:beta",
                            name="Beta",
                            artists=("Beta Artist",),
                            album_name="Beta Album",
                            added_at="2026-01-03T00:00:00Z",
                            position=2,
                        ),
                    )
                },
            )

            summary = dedupe_spotify_managed_playlists(
                spotify_client=client,
                access_token="access-token",
                report_path=report_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=True,
            )

            self.assertEqual(summary.duplicate_count, 1)
            self.assertEqual(summary.removed_count, 1)
            self.assertEqual(client.remove_calls, [])
            self.assertEqual(client.replace_calls, [("playlist-house", ("spotify:track:alpha", "spotify:track:beta"))])
            self.assertEqual(client.add_calls, [])
            self.assertEqual(
                [item.uri for item in client.playlist_items_by_id["playlist-house"]],
                ["spotify:track:alpha", "spotify:track:beta"],
            )

    def test_playlist_selector_apply_removes_only_selected_playlist_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = FakeSpotifyDedupeClient(
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                        snapshot_id="snapshot-house",
                    ),
                    SpotifyPlaylist(
                        playlist_id="playlist-techno",
                        name="Discogs - Techno",
                        url="",
                        owner_id="current-user",
                        public=False,
                        snapshot_id="snapshot-techno",
                    ),
                ),
                playlist_items_by_id={
                    "playlist-house": (
                        SpotifyPlaylistItem(uri="spotify:track:house", name="House", artists=("Artist",), album_name="Album", position=0),
                        SpotifyPlaylistItem(uri="spotify:track:house", name="House", artists=("Artist",), album_name="Album", position=1),
                    ),
                    "playlist-techno": (
                        SpotifyPlaylistItem(uri="spotify:track:techno", name="Techno", artists=("Artist",), album_name="Album", position=0),
                        SpotifyPlaylistItem(uri="spotify:track:techno", name="Techno", artists=("Artist",), album_name="Album", position=1),
                    ),
                },
            )

            summary = dedupe_spotify_managed_playlists(
                spotify_client=client,
                access_token="access-token",
                report_path=report_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=True,
                playlist_selectors=("Techno",),
            )

            self.assertEqual(summary.duplicate_count, 1)
            self.assertEqual(summary.removed_count, 1)
            self.assertEqual(client.remove_calls, [])
            self.assertEqual([call[0] for call in client.replace_calls], ["playlist-techno"])
            self.assertEqual([item.position for item in client.playlist_items_by_id["playlist-house"]], [0, 1])
            self.assertEqual([item.uri for item in client.playlist_items_by_id["playlist-techno"]], ["spotify:track:techno"])

    def test_apply_removes_all_duplicate_positions_when_uri_appears_more_than_twice(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = FakeSpotifyDedupeClient(
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                        snapshot_id="snapshot-house",
                    ),
                ),
                playlist_items_by_id={
                    "playlist-house": (
                        SpotifyPlaylistItem(
                            uri="spotify:track:alpha",
                            name="Alpha",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                            added_at="",
                            position=0,
                        ),
                        SpotifyPlaylistItem(
                            uri="spotify:track:alpha",
                            name="Alpha",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                            added_at="2026-01-01T00:00:00Z",
                            position=1,
                        ),
                        SpotifyPlaylistItem(
                            uri="spotify:track:alpha",
                            name="Alpha",
                            artists=("Alpha Artist",),
                            album_name="Alpha Album",
                            added_at="not-a-timestamp",
                            position=2,
                        ),
                    )
                },
            )

            summary = dedupe_spotify_managed_playlists(
                spotify_client=client,
                access_token="access-token",
                report_path=report_path,
                publisher_config=PublisherConfig(
                    default_publisher="spotify",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
                apply=True,
            )

        self.assertEqual(summary.duplicate_count, 2)
        self.assertEqual(summary.removed_count, 2)
        self.assertEqual(client.remove_calls, [])
        self.assertEqual(client.replace_calls, [("playlist-house", ("spotify:track:alpha",))])
        self.assertEqual([item.position for item in client.playlist_items_by_id["playlist-house"]], [0])

    def test_failed_apply_report_does_not_claim_removals_when_append_after_replace_fails(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            playlist_items = []
            for index in range(101):
                playlist_items.extend(
                    (
                        SpotifyPlaylistItem(
                            uri=f"spotify:track:{index}",
                            name=f"Track {index}",
                            artists=("Artist",),
                            album_name="Album",
                            added_at="2026-01-01T00:00:00Z",
                            position=index * 2,
                        ),
                        SpotifyPlaylistItem(
                            uri=f"spotify:track:{index}",
                            name=f"Track {index}",
                            artists=("Artist",),
                            album_name="Album",
                            added_at="2026-01-02T00:00:00Z",
                            position=index * 2 + 1,
                        ),
                    )
                )
            client = FailingAddSpotifyDedupeClient(
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="Discogs - House",
                        url="",
                        owner_id="current-user",
                        public=False,
                        snapshot_id="snapshot-house",
                    ),
                ),
                playlist_items_by_id={"playlist-house": tuple(playlist_items)},
            )

            with self.assertRaisesRegex(SpotifyApiError, "append after replace failed"):
                dedupe_spotify_managed_playlists(
                    spotify_client=client,
                    access_token="access-token",
                    report_path=report_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="Discogs - ",
                        playlist_suffix="",
                    ),
                    apply=True,
                )
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(client.remove_calls, [])
        self.assertEqual(len(client.replace_calls), 1)
        self.assertEqual(len(client.replace_calls[0][1]), 100)
        self.assertEqual(len(client.add_calls), 1)
        self.assertEqual(len(client.add_calls[0][1]), 1)
        self.assertIn("Run status: failed during apply", report_text)
        self.assertIn("Duplicate tracks planned for removal: 101", report_text)
        self.assertIn("Duplicate tracks removed: 0", report_text)

    def test_empty_prefix_and_suffix_abort_before_fetching_items(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "reports" / "dedupe.txt"
            client = FakeSpotifyDedupeClient(
                playlists=(
                    SpotifyPlaylist(
                        playlist_id="playlist-house",
                        name="House",
                        url="",
                        owner_id="current-user",
                        public=False,
                    ),
                )
            )

            with self.assertRaisesRegex(ValueError, "playlist_prefix or playlist_suffix"):
                dedupe_spotify_managed_playlists(
                    spotify_client=client,
                    access_token="access-token",
                    report_path=report_path,
                    publisher_config=PublisherConfig(
                        default_publisher="spotify",
                        playlist_prefix="",
                        playlist_suffix="",
                    ),
                    apply=False,
                )


if __name__ == "__main__":
    unittest.main()
