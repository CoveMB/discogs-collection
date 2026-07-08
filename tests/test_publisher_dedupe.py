import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.dedupe import (  # noqa: E402
    ProviderPlaylist,
    ProviderPlaylistItem,
    plan_playlist_dedupe,
)


class PublisherDedupeTests(unittest.TestCase):
    def test_no_duplicates_keeps_all_items(self):
        playlist = ProviderPlaylist(provider="spotify", playlist_id="playlist-house", name="Discogs - House")

        plans = plan_playlist_dedupe(
            (playlist,),
            {
                "playlist-house": (
                    ProviderPlaylistItem(
                        playlist_id="playlist-house",
                        playlist_name="Discogs - House",
                        uri="spotify:track:alpha",
                        position=0,
                        added_at="2026-01-01T00:00:00Z",
                    ),
                    ProviderPlaylistItem(
                        playlist_id="playlist-house",
                        playlist_name="Discogs - House",
                        uri="spotify:track:beta",
                        position=1,
                        added_at="2026-01-02T00:00:00Z",
                    ),
                )
            },
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].item_count, 2)
        self.assertEqual(plans[0].duplicate_count, 0)
        self.assertEqual(plans[0].removals, ())

    def test_duplicate_uri_keeps_earliest_added_item(self):
        playlist = ProviderPlaylist(provider="spotify", playlist_id="playlist-house", name="Discogs - House")
        later_duplicate = ProviderPlaylistItem(
            playlist_id="playlist-house",
            playlist_name="Discogs - House",
            uri="spotify:track:alpha",
            position=0,
            added_at="2026-01-02T00:00:00Z",
            name="Alpha",
        )
        first_added = ProviderPlaylistItem(
            playlist_id="playlist-house",
            playlist_name="Discogs - House",
            uri="spotify:track:alpha",
            position=1,
            added_at="2026-01-01T00:00:00Z",
            name="Alpha",
        )

        plans = plan_playlist_dedupe(
            (playlist,),
            {"playlist-house": (later_duplicate, first_added)},
        )

        self.assertEqual(plans[0].duplicate_count, 1)
        self.assertEqual(plans[0].removals[0].item, later_duplicate)
        self.assertEqual(plans[0].removals[0].kept_item, first_added)
        self.assertEqual(plans[0].removals[0].reason, "same track URI already kept from an earlier added item")

    def test_missing_or_tied_added_at_keeps_lowest_position(self):
        playlist = ProviderPlaylist(provider="spotify", playlist_id="playlist-house", name="Discogs - House")
        first_position = ProviderPlaylistItem(
            playlist_id="playlist-house",
            playlist_name="Discogs - House",
            uri="spotify:track:alpha",
            position=0,
            added_at="",
        )
        second_position = ProviderPlaylistItem(
            playlist_id="playlist-house",
            playlist_name="Discogs - House",
            uri="spotify:track:alpha",
            position=1,
            added_at="not-a-timestamp",
        )

        plans = plan_playlist_dedupe(
            (playlist,),
            {"playlist-house": (second_position, first_position)},
        )

        self.assertEqual(plans[0].removals[0].item, second_position)
        self.assertEqual(plans[0].removals[0].kept_item, first_position)

    def test_three_duplicate_uri_items_keep_lowest_position_when_any_timestamp_is_uncertain(self):
        playlist = ProviderPlaylist(provider="spotify", playlist_id="playlist-house", name="Discogs - House")
        first_position = ProviderPlaylistItem(
            playlist_id="playlist-house",
            playlist_name="Discogs - House",
            uri="spotify:track:alpha",
            position=0,
            added_at="",
        )
        second_position = ProviderPlaylistItem(
            playlist_id="playlist-house",
            playlist_name="Discogs - House",
            uri="spotify:track:alpha",
            position=1,
            added_at="2026-01-01T00:00:00Z",
        )
        third_position = ProviderPlaylistItem(
            playlist_id="playlist-house",
            playlist_name="Discogs - House",
            uri="spotify:track:alpha",
            position=2,
            added_at="not-a-timestamp",
        )

        plans = plan_playlist_dedupe(
            (playlist,),
            {"playlist-house": (third_position, second_position, first_position)},
        )

        self.assertEqual(plans[0].duplicate_count, 2)
        self.assertEqual([removal.item.position for removal in plans[0].removals], [1, 2])
        self.assertEqual([removal.kept_item for removal in plans[0].removals], [first_position, first_position])

    def test_blank_uri_items_are_never_deduped(self):
        playlist = ProviderPlaylist(provider="spotify", playlist_id="playlist-house", name="Discogs - House")

        plans = plan_playlist_dedupe(
            (playlist,),
            {
                "playlist-house": (
                    ProviderPlaylistItem(
                        playlist_id="playlist-house",
                        playlist_name="Discogs - House",
                        uri="",
                        position=0,
                    ),
                    ProviderPlaylistItem(
                        playlist_id="playlist-house",
                        playlist_name="Discogs - House",
                        uri="",
                        position=1,
                    ),
                )
            },
        )

        self.assertEqual(plans[0].duplicate_count, 0)
        self.assertEqual(plans[0].removals, ())

    def test_duplicates_are_scoped_to_each_playlist(self):
        house = ProviderPlaylist(provider="spotify", playlist_id="playlist-house", name="Discogs - House")
        techno = ProviderPlaylist(provider="spotify", playlist_id="playlist-techno", name="Discogs - Techno")

        plans = plan_playlist_dedupe(
            (house, techno),
            {
                "playlist-house": (
                    ProviderPlaylistItem(
                        playlist_id="playlist-house",
                        playlist_name="Discogs - House",
                        uri="spotify:track:alpha",
                        position=0,
                    ),
                ),
                "playlist-techno": (
                    ProviderPlaylistItem(
                        playlist_id="playlist-techno",
                        playlist_name="Discogs - Techno",
                        uri="spotify:track:alpha",
                        position=0,
                    ),
                ),
            },
        )

        self.assertEqual([plan.duplicate_count for plan in plans], [0, 0])


if __name__ == "__main__":
    unittest.main()
