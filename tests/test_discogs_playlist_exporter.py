import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_playlist_exporter as exporter  # noqa: E402
from shared.progress import ProgressReporter  # noqa: E402


def read_csv_file(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


class PlaylistExporterTests(unittest.TestCase):
    def test_reports_row_progress_while_exporting_playlist_csvs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "enriched-collection.csv"
            output_directory = directory / "playlists"
            report_path = directory / "playlist-export-report.txt"
            input_path.write_text(
                "release_id,Artist,Title,Released,Format,Playlists\n"
                '111,Alpha Artist,Alpha Album,1997,"Vinyl, LP, Album",Discogs - Breakbeat\n'
                '222,Beta Artist,Beta Album,2001,"CD, Album",Discogs - House\n',
                encoding="utf-8",
            )
            lookups = {
                "111": exporter.ReleaseTracklistLookup(
                    release_id="111",
                    artist_name="Alpha Artist",
                    album_name="Alpha Album",
                    record_year="1997",
                    tracks=(exporter.DiscogsTrack(position="A1", title="Alpha One", artist_name="Alpha Artist"),),
                    notes=(),
                ),
                "222": exporter.ReleaseTracklistLookup(
                    release_id="222",
                    artist_name="Beta Artist",
                    album_name="Beta Album",
                    record_year="2001",
                    tracks=(exporter.DiscogsTrack(position="1", title="Beta One", artist_name="Beta Artist"),),
                    notes=(),
                ),
            }
            progress_stream = TerminalStream()
            progress = ProgressReporter(stream=progress_stream, label="Exporting playlist rows")

            exporter.export_playlist_csvs(
                input_path=input_path,
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookups[row["release_id"]],
                progress=progress,
            )

            progress_text = progress_stream.getvalue()
            self.assertIn("\rExporting playlist rows [", progress_text)
            self.assertIn("1/2", progress_text)
            self.assertIn("50%", progress_text)
            self.assertIn("2/2", progress_text)
            self.assertIn("100%", progress_text)
            self.assertTrue(progress_text.endswith("\n"))

    def test_exports_one_tunemymusic_csv_per_playlist_with_discogs_track_rows(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "enriched-collection.csv"
            output_directory = directory / "playlists"
            report_path = directory / "playlist-export-report.txt"
            input_path.write_text(
                "release_id,Artist,Title,Released,Format,Playlists\n"
                '111,Alpha Artist,Alpha Album,1997,"Vinyl, LP, Album",Discogs - Breakbeat\n'
                '222,Beta Artist,Beta Album,2001,"CD, Album","Discogs - Breakbeat, Discogs - House"\n',
                encoding="utf-8",
            )
            lookups = {
                "111": exporter.ReleaseTracklistLookup(
                    release_id="111",
                    artist_name="Alpha Artist",
                    album_name="Alpha Album",
                    record_year="1997",
                    tracks=(
                        exporter.DiscogsTrack(position="A1", title="Alpha One", artist_name="Alpha Artist"),
                        exporter.DiscogsTrack(position="A2", title="Alpha Two", artist_name="Guest Artist"),
                    ),
                    notes=(),
                ),
                "222": exporter.ReleaseTracklistLookup(
                    release_id="222",
                    artist_name="Beta Artist",
                    album_name="Beta Album",
                    record_year="2001",
                    tracks=(
                        exporter.DiscogsTrack(position="1", title="Beta One", artist_name="Beta Artist"),
                    ),
                    notes=(),
                ),
            }

            summary = exporter.export_playlist_csvs(
                input_path=input_path,
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookups[row["release_id"]],
            )

            breakbeat_path = output_directory / "Discogs - Breakbeat.csv"
            house_path = output_directory / "Discogs - House.csv"
            breakbeat_rows = read_csv_file(breakbeat_path)
            house_rows = read_csv_file(house_path)

            self.assertEqual(summary.playlist_count, 2)
            self.assertEqual(summary.track_row_count, 4)
            self.assertEqual(summary.fallback_row_count, 0)
            self.assertEqual(summary.skipped_unassigned_count, 0)
            self.assertEqual(list(breakbeat_rows[0].keys()), list(exporter.TUNEMYMUSIC_COLUMNS))
            self.assertEqual([row["Position"] for row in breakbeat_rows], ["1", "2", "3"])
            self.assertEqual([row["Record Rank"] for row in breakbeat_rows], ["1", "1", "2"])
            self.assertEqual([row["Track Number"] for row in breakbeat_rows], ["1", "2", "1"])
            self.assertEqual(breakbeat_rows[1]["Artist Name"], "Guest Artist")
            self.assertEqual(breakbeat_rows[2]["Album Name"], "Beta Album")
            self.assertEqual(breakbeat_rows[2]["Record Year"], "2001")
            self.assertEqual(breakbeat_rows[2]["Release Type"], "Album")
            self.assertIn("Beta One", breakbeat_rows[2]["Spotify Search Query"])
            self.assertIn("Beta Artist", breakbeat_rows[2]["Spotify Search Query"])
            self.assertIn("Beta Album", breakbeat_rows[2]["Spotify Search Query"])
            self.assertEqual(breakbeat_rows[2]["Source URL"], "https://www.discogs.com/release/222")
            self.assertEqual(len(house_rows), 1)
            self.assertEqual(house_rows[0]["Track Name"], "Beta One")
            self.assertIn(str(breakbeat_path), report_path.read_text(encoding="utf-8"))
            self.assertIn(str(house_path), report_path.read_text(encoding="utf-8"))

    def test_falls_back_to_release_rows_and_reports_uncertain_exports(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "enriched-collection.csv"
            output_directory = directory / "playlists"
            report_path = directory / "playlist-export-report.txt"
            input_path.write_text(
                "release_id,Artist,Title,Released,Format,Playlists\n"
                '111,Alpha Artist,Alpha Album,1997,"Vinyl, LP, Album",Discogs - Breakbeat\n'
                ',Missing Artist,Missing Album,1998,"CD, Album",Discogs - Breakbeat\n'
                '333,Skipped Artist,Skipped Album,1999,"CD, Album",\n',
                encoding="utf-8",
            )
            empty_lookup = exporter.ReleaseTracklistLookup(
                release_id="111",
                artist_name="Alpha Artist",
                album_name="Alpha Album",
                record_year="1997",
                tracks=(),
                notes=("no Discogs tracklist found",),
            )

            summary = exporter.export_playlist_csvs(
                input_path=input_path,
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: empty_lookup,
            )

            rows = read_csv_file(output_directory / "Discogs - Breakbeat.csv")
            report_text = report_path.read_text(encoding="utf-8")

            self.assertEqual(summary.track_row_count, 2)
            self.assertEqual(summary.fallback_row_count, 2)
            self.assertEqual(summary.skipped_unassigned_count, 1)
            self.assertEqual([row["Track Name"] for row in rows], ["Alpha Album", "Missing Album"])
            self.assertEqual(rows[0]["Version Note"], "Release-level fallback row because no Discogs tracklist found.")
            self.assertEqual(rows[1]["Version Note"], "Release-level fallback row because release_id is missing.")
            self.assertIn("Release ID 111: no Discogs tracklist found", report_text)
            self.assertIn("Row 2: release_id is missing", report_text)
            self.assertIn("Skipped rows without playlists: 1", report_text)

    def test_discogs_payload_parser_flattens_sub_tracks_and_prefers_track_artists(self):
        row = {
            "release_id": "444",
            "Artist": "Release Artist",
            "Title": "Release Album",
            "Released": "2002-04-01",
        }
        payload = {
            "artists": [{"name": "Payload Artist"}],
            "title": "Payload Album",
            "year": 2002,
            "tracklist": [
                {
                    "position": "A",
                    "type_": "index",
                    "title": "Suite",
                    "sub_tracks": [
                        {
                            "position": "A1",
                            "type_": "track",
                            "title": "Part One",
                            "artists": [{"name": "Track Artist"}],
                        },
                    ],
                },
                {"position": "B1", "type_": "track", "title": "Part Two"},
                {"position": "", "type_": "heading", "title": "Bonus"},
            ],
        }

        lookup = exporter.release_tracklist_from_payload("444", payload, row)

        self.assertEqual(lookup.artist_name, "Payload Artist")
        self.assertEqual(lookup.album_name, "Payload Album")
        self.assertEqual(lookup.record_year, "2002")
        self.assertEqual(
            lookup.tracks,
            (
                exporter.DiscogsTrack(position="A1", title="Part One", artist_name="Track Artist"),
                exporter.DiscogsTrack(position="B1", title="Part Two", artist_name="Payload Artist"),
            ),
        )

    def test_parse_args_enables_progress_by_default_and_can_disable_it(self):
        default_args = exporter.parse_args([])
        quiet_args = exporter.parse_args(["--no-progress"])

        self.assertTrue(default_args.progress)
        self.assertFalse(quiet_args.progress)


class TerminalStream(io.StringIO):
    def isatty(self):
        return True


if __name__ == "__main__":
    unittest.main()
