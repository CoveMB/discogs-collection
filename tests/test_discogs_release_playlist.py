import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_playlist_exporter as exporter  # noqa: E402
import discogs_release_playlist as release_playlist  # noqa: E402


def read_csv_file(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


def lookup_for(release_id: str) -> exporter.ReleaseTracklistLookup:
    return exporter.ReleaseTracklistLookup(
        release_id=release_id,
        artist_name=f"Artist {release_id}",
        album_name=f"Album {release_id}",
        record_year="1997",
        tracks=(
            exporter.DiscogsTrack(
                position="A1",
                title=f"Track {release_id}",
                artist_name=f"Artist {release_id}",
            ),
        ),
        notes=(),
    )


def publish_summary_stub():
    return SimpleNamespace(
        report_path=Path("reports/spotify_playlist_publish_report.txt"),
        track_count=1,
        cache_hit_count=0,
        search_count=1,
        matched_count=1,
        already_present_count=0,
        would_add_count=1,
        added_count=0,
        would_include_count=0,
        included_count=0,
        duplicate_in_source_count=0,
        ambiguous_count=0,
        unmatched_count=0,
        error_count=0,
    )


class DiscogsReleasePlaylistTests(unittest.TestCase):
    def test_create_release_playlist_dedupes_ids_and_writes_isolated_master_without_collection_master(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"

            summary = release_playlist.create_release_playlist(
                playlist_name="Friday Picks",
                release_ids=("111", "222", "111"),
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookup_for(row["release_id"]),
            )

            master_path = output_directory / "Friday Picks" / "Friday Picks.csv"
            rows = read_csv_file(master_path)

            self.assertEqual(summary.release_ids, ("111", "222"))
            self.assertEqual(summary.duplicate_release_ids, ("111",))
            self.assertEqual(summary.master_path, master_path)
            self.assertEqual([row["Release Id"] for row in rows], ["111", "222"])
            self.assertFalse((directory / "collection" / "enriched-collection.csv").exists())
            self.assertIn("Duplicate release IDs skipped: 1", report_path.read_text(encoding="utf-8"))

    def test_create_release_playlist_reuses_playlist_name_by_overwriting_existing_master(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"

            release_playlist.create_release_playlist(
                playlist_name="Friday Picks",
                release_ids=("111",),
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookup_for(row["release_id"]),
            )
            release_playlist.create_release_playlist(
                playlist_name="Friday Picks",
                release_ids=("222",),
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookup_for(row["release_id"]),
            )

            rows = read_csv_file(output_directory / "Friday Picks" / "Friday Picks.csv")
            self.assertEqual([row["Release Id"] for row in rows], ["222"])

    def test_create_release_playlist_rejects_path_control_playlist_name_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"

            with self.assertRaisesRegex(ValueError, "playlist name resolves outside"):
                release_playlist.create_release_playlist(
                    playlist_name="..",
                    release_ids=("111",),
                    output_directory=output_directory,
                    report_path=report_path,
                    lookup_tracklist=lambda row: lookup_for(row["release_id"]),
                )

            self.assertFalse((directory / "collection" / "playlists" / "...csv").exists())
            self.assertFalse(report_path.exists())

    def test_create_release_playlist_rejects_distinct_names_that_share_sanitized_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"

            release_playlist.create_release_playlist(
                playlist_name="Friday/Picks",
                release_ids=("111",),
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookup_for(row["release_id"]),
            )

            with self.assertRaisesRegex(ValueError, "already used by playlist name"):
                release_playlist.create_release_playlist(
                    playlist_name="Friday:Picks",
                    release_ids=("222",),
                    output_directory=output_directory,
                    report_path=report_path,
                    lookup_tracklist=lambda row: lookup_for(row["release_id"]),
                )

            rows = read_csv_file(output_directory / "Friday_Picks" / "Friday_Picks.csv")
            self.assertEqual([row["Release Id"] for row in rows], ["111"])

    def test_run_release_playlist_uses_default_publisher_config_but_publishes_without_prefix_suffix(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"
            publisher_report_path = directory / "reports" / "spotify-publish.txt"
            publisher_config_path = directory / "config" / "publisher.json"
            publisher_config_path.parent.mkdir(parents=True)
            publisher_config_path.write_text(
                json.dumps(
                    {
                        "default_publisher": "spotify",
                        "playlist_prefix": "Discogs - ",
                        "playlist_suffix": "!",
                    }
                ),
                encoding="utf-8",
            )
            args = release_playlist.parse_args(
                [
                    "--name",
                    "Friday/Picks",
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--publisher-report",
                    str(publisher_report_path),
                    "--publisher-config",
                    str(publisher_config_path),
                    "--publishing-dry-run",
                    "--no-progress",
                    "111",
                ]
            )

            with (
                patch(
                    "discogs_release_playlist.exporter.make_cached_tracklist_lookup",
                    return_value=lambda row: lookup_for(row["release_id"]),
                ),
                patch(
                    "discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args",
                    return_value=publish_summary_stub(),
                ) as publish,
            ):
                summary = release_playlist.run_release_playlist(args)

            expected_master_path = output_directory / "Friday_Picks" / "Friday_Picks.csv"
            publisher_args = publish.call_args.args[0]
            publisher_config = publish.call_args.kwargs["publisher_config"]
            self.assertEqual(summary.publisher, "spotify")
            self.assertEqual(publish.call_args.kwargs["playlist_master_paths"], (expected_master_path,))
            self.assertEqual(publish.call_args.kwargs["playlist_names_by_master_path"], {expected_master_path: "Friday/Picks"})
            self.assertEqual(publisher_config.playlist_prefix, "")
            self.assertEqual(publisher_config.playlist_suffix, "")
            self.assertEqual(publisher_args.report, publisher_report_path)
            self.assertFalse(publisher_args.apply)

    def test_publisher_flag_overrides_default_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"
            publisher_config_path = directory / "config" / "publisher.json"
            publisher_config_path.parent.mkdir(parents=True)
            publisher_config_path.write_text(
                json.dumps(
                    {
                        "default_publisher": "spotify",
                        "playlist_prefix": "Discogs - ",
                        "playlist_suffix": "",
                    }
                ),
                encoding="utf-8",
            )
            args = release_playlist.parse_args(
                [
                    "--name",
                    "Friday Picks",
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--publisher-config",
                    str(publisher_config_path),
                    "--publisher",
                    "none",
                    "--no-progress",
                    "111",
                ]
            )

            with (
                patch(
                    "discogs_release_playlist.exporter.make_cached_tracklist_lookup",
                    return_value=lambda row: lookup_for(row["release_id"]),
                ),
                patch("discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args") as publish,
            ):
                summary = release_playlist.run_release_playlist(args)

            self.assertEqual(summary.publisher, "none")
            publish.assert_not_called()

    def test_invalid_default_publisher_config_stops_before_tracklist_lookup_and_output_writes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"
            publisher_config_path = directory / "config" / "publisher.json"
            publisher_config_path.parent.mkdir(parents=True)
            publisher_config_path.write_text('{"default_publisher": "apple"}', encoding="utf-8")
            args = release_playlist.parse_args(
                [
                    "--name",
                    "Friday Picks",
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--publisher-config",
                    str(publisher_config_path),
                    "--no-progress",
                    "111",
                ]
            )

            with patch("discogs_release_playlist.exporter.make_cached_tracklist_lookup") as make_lookup:
                with self.assertRaisesRegex(ValueError, "default_publisher"):
                    release_playlist.run_release_playlist(args)

            make_lookup.assert_not_called()
            self.assertFalse(output_directory.exists())
            self.assertFalse(report_path.exists())


if __name__ == "__main__":
    unittest.main()
