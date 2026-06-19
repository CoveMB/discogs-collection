import csv
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_playlist_exporter as exporter  # noqa: E402
from shared.progress import ProgressReporter  # noqa: E402


def read_csv_file(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


class PlaylistExporterTests(unittest.TestCase):
    def test_parse_args_defaults_to_script_report_path(self):
        with patch("shared.reports.readable_timestamp", return_value="2026-06-10_14-30-00"):
            args = exporter.parse_args([])

        self.assertEqual(args.report, Path("reports/2026-06-10_14-30-00_discogs_playlist_exporter.txt"))

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

            breakbeat_path = output_directory / "Discogs - Breakbeat" / "Discogs - Breakbeat.csv"
            house_path = output_directory / "Discogs - House" / "Discogs - House.csv"
            breakbeat_rows = read_csv_file(breakbeat_path)
            house_rows = read_csv_file(house_path)

            self.assertEqual(summary.playlist_count, 2)
            self.assertEqual(summary.track_row_count, 4)
            self.assertEqual(summary.fallback_row_count, 0)
            self.assertEqual(summary.skipped_unassigned_count, 0)
            self.assertEqual(list(breakbeat_rows[0].keys()), list(exporter.TUNEMYMUSIC_COLUMNS))
            self.assertEqual(
                list(breakbeat_rows[0].keys()),
                [
                    "Release Id",
                    "Album Name",
                    "Track Number",
                    "Track Name",
                    "Artist Name",
                    "Spotify Search Query",
                ],
            )
            self.assertEqual([row["Release Id"] for row in breakbeat_rows], ["111", "111", "222"])
            self.assertEqual([row["Track Number"] for row in breakbeat_rows], ["1", "2", "1"])
            self.assertEqual(breakbeat_rows[1]["Artist Name"], "Guest Artist")
            self.assertEqual(breakbeat_rows[2]["Album Name"], "Beta Album")
            self.assertEqual(breakbeat_rows[2]["Spotify Search Query"], "Beta Artist Beta One Beta Album")
            self.assertEqual(len(house_rows), 1)
            self.assertEqual(house_rows[0]["Track Name"], "Beta One")
            self.assertIn(str(breakbeat_path), report_path.read_text(encoding="utf-8"))
            self.assertIn(str(house_path), report_path.read_text(encoding="utf-8"))
            self.assertFalse((output_directory / "Discogs - Breakbeat.csv").exists())
            self.assertFalse((output_directory / "Discogs - House.csv").exists())

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

            rows = read_csv_file(output_directory / "Discogs - Breakbeat" / "Discogs - Breakbeat.csv")
            report_text = report_path.read_text(encoding="utf-8")

            self.assertEqual(summary.track_row_count, 2)
            self.assertEqual(summary.fallback_row_count, 2)
            self.assertEqual(summary.skipped_unassigned_count, 1)
            self.assertEqual([row["Track Name"] for row in rows], ["Alpha Album", "Missing Album"])
            self.assertEqual([row["Release Id"] for row in rows], ["111", ""])
            self.assertIn("Release ID 111: no Discogs tracklist found", report_text)
            self.assertIn("Row 2: release_id is missing", report_text)
            self.assertIn("Skipped rows without playlists: 1", report_text)

    def test_report_lists_added_and_removed_releases_per_playlist_without_changing_rewrite_behavior(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "enriched-collection.csv"
            output_directory = directory / "playlists"
            report_path = directory / "playlist-export-report.txt"
            existing_playlist_path = output_directory / "Discogs - Breakbeat" / "Discogs - Breakbeat.csv"
            existing_playlist_path.parent.mkdir(parents=True)
            existing_playlist_path.write_text(
                "Release Id,Album Name,Track Number,Track Name,Artist Name,Spotify Search Query\n"
                "111,Alpha Album,1,Alpha One,Alpha Artist,Alpha Artist Alpha One Alpha Album\n"
                "333,Gamma Album,1,Gamma One,Gamma Artist,Gamma Artist Gamma One Gamma Album\n",
                encoding="utf-8",
            )
            input_path.write_text(
                "release_id,Artist,Title,Released,Format,Playlists\n"
                '111,Alpha Artist,Alpha Album,1997,"Vinyl, LP, Album",Discogs - Breakbeat\n'
                '222,Beta Artist,Beta Album,2001,"CD, Album",Discogs - Breakbeat\n',
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

            summary = exporter.export_playlist_csvs(
                input_path=input_path,
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookups[row["release_id"]],
            )

            rewritten_rows = read_csv_file(existing_playlist_path)
            report_text = report_path.read_text(encoding="utf-8")

            self.assertEqual([row["Release Id"] for row in rewritten_rows], ["111", "222"])
            self.assertEqual(summary.track_row_count, 2)
            self.assertEqual(
                [entry.track_name for entry in summary.playlist_release_changes[0].added_tracks],
                ["Beta One"],
            )
            self.assertIn("Playlist release changes", report_text)
            self.assertIn("- Discogs - Breakbeat:", report_text)
            self.assertIn("Added releases:", report_text)
            self.assertIn("222 | Beta Artist | Beta Album | 1 track row", report_text)
            self.assertIn("Removed releases:", report_text)
            self.assertIn("333 | Gamma Artist | Gamma Album | 1 track row", report_text)

    def test_report_lists_stale_playlist_file_releases_as_removed_without_deleting_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "enriched-collection.csv"
            output_directory = directory / "playlists"
            report_path = directory / "playlist-export-report.txt"
            stale_path = output_directory / "Discogs - Old" / "Discogs - Old.csv"
            stale_path.parent.mkdir(parents=True)
            stale_path.write_text(
                "Release Id,Album Name,Track Number,Track Name,Artist Name,Spotify Search Query\n"
                "999,Old Album,1,Old Track,Old Artist,Old Artist Old Track Old Album\n",
                encoding="utf-8",
            )
            input_path.write_text(
                "release_id,Artist,Title,Released,Format,Playlists\n"
                '111,Alpha Artist,Alpha Album,1997,"Vinyl, LP, Album",Discogs - Breakbeat\n',
                encoding="utf-8",
            )
            lookup = exporter.ReleaseTracklistLookup(
                release_id="111",
                artist_name="Alpha Artist",
                album_name="Alpha Album",
                record_year="1997",
                tracks=(exporter.DiscogsTrack(position="A1", title="Alpha One", artist_name="Alpha Artist"),),
                notes=(),
            )

            exporter.export_playlist_csvs(
                input_path=input_path,
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookup,
            )

            report_text = report_path.read_text(encoding="utf-8")
            self.assertTrue(stale_path.exists())
            self.assertIn("- Discogs - Old:", report_text)
            self.assertIn("previous playlist file was not regenerated in this run; file left unchanged", report_text)
            self.assertIn("999 | Old Artist | Old Album | 1 track row", report_text)

    def test_stale_root_playlist_file_is_not_reported_as_current_generated_playlist_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "enriched-collection.csv"
            output_directory = directory / "playlists"
            report_path = directory / "playlist-export-report.txt"
            output_directory.mkdir()
            stale_root_path = output_directory / "Discogs - Old.csv"
            stale_root_path.write_text(
                "Release Id,Album Name,Track Number,Track Name,Artist Name,Spotify Search Query\n"
                "999,Old Album,1,Old Track,Old Artist,Old Artist Old Track Old Album\n",
                encoding="utf-8",
            )
            input_path.write_text(
                "release_id,Artist,Title,Released,Format,Playlists\n"
                '111,Alpha Artist,Alpha Album,1997,"Vinyl, LP, Album",Discogs - Breakbeat\n',
                encoding="utf-8",
            )
            lookup = exporter.ReleaseTracklistLookup(
                release_id="111",
                artist_name="Alpha Artist",
                album_name="Alpha Album",
                record_year="1997",
                tracks=(exporter.DiscogsTrack(position="A1", title="Alpha One", artist_name="Alpha Artist"),),
                notes=(),
            )

            summary = exporter.export_playlist_csvs(
                input_path=input_path,
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookup,
            )

            report_text = report_path.read_text(encoding="utf-8")
            self.assertTrue(stale_root_path.exists())
            self.assertNotIn("- Discogs - Old:", report_text)
            self.assertNotIn("999 | Old Artist | Old Album | 1 track row", report_text)
            self.assertEqual(
                [change.playlist_name for change in summary.playlist_release_changes],
                ["Discogs - Breakbeat"],
            )

    def test_existing_playlist_folder_with_case_variant_is_reused_and_not_reported_stale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "enriched-collection.csv"
            output_directory = directory / "playlists"
            report_path = directory / "playlist-export-report.txt"
            existing_playlist_path = output_directory / "House" / "House.csv"
            existing_playlist_path.parent.mkdir(parents=True)
            existing_playlist_path.write_text(
                "Release Id,Album Name,Track Number,Track Name,Artist Name,Spotify Search Query\n"
                "111,Alpha Album,1,Old Alpha One,Alpha Artist,Alpha Artist Old Alpha One Alpha Album\n",
                encoding="utf-8",
            )
            input_path.write_text(
                "release_id,Artist,Title,Released,Format,Playlists\n"
                '111,Alpha Artist,Alpha Album,1997,"Vinyl, LP, Album",house\n',
                encoding="utf-8",
            )
            lookup = exporter.ReleaseTracklistLookup(
                release_id="111",
                artist_name="Alpha Artist",
                album_name="Alpha Album",
                record_year="1997",
                tracks=(exporter.DiscogsTrack(position="A1", title="Alpha One", artist_name="Alpha Artist"),),
                notes=(),
            )

            summary = exporter.export_playlist_csvs(
                input_path=input_path,
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookup,
            )

            report_text = report_path.read_text(encoding="utf-8")
            self.assertEqual(summary.playlist_files[0].path, existing_playlist_path)
            self.assertEqual(read_csv_file(existing_playlist_path)[0]["Track Name"], "Alpha One")
            self.assertNotIn("previous playlist file was not regenerated in this run", report_text)
            self.assertNotIn("Removed releases:", report_text)

    def test_release_change_report_handles_missing_release_ids_by_artist_and_album(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "enriched-collection.csv"
            output_directory = directory / "playlists"
            report_path = directory / "playlist-export-report.txt"
            existing_playlist_path = output_directory / "Discogs - Breakbeat" / "Discogs - Breakbeat.csv"
            existing_playlist_path.parent.mkdir(parents=True)
            existing_playlist_path.write_text(
                "Release Id,Album Name,Track Number,Track Name,Artist Name,Spotify Search Query\n"
                ",Old Missing Album,1,Old Missing Album,Old Missing Artist,Old Missing Artist Old Missing Album Old Missing Album\n",
                encoding="utf-8",
            )
            input_path.write_text(
                "release_id,Artist,Title,Released,Format,Playlists\n"
                ',New Missing Artist,New Missing Album,1997,"Vinyl, LP, Album",Discogs - Breakbeat\n',
                encoding="utf-8",
            )

            exporter.export_playlist_csvs(
                input_path=input_path,
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: (_ for _ in ()).throw(
                    AssertionError("missing release_id should not be looked up")
                ),
            )

            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("missing release_id | New Missing Artist | New Missing Album | 1 track row", report_text)
            self.assertIn("missing release_id | Old Missing Artist | Old Missing Album | 1 track row", report_text)

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

    def test_playlist_path_helpers_use_safe_folder_master_paths_and_case_insensitive_suffixes(self):
        output_directory = Path("collection/playlists")

        folder_paths = exporter.build_playlist_folder_paths(
            ["House", "house", 'Discogs: "Breakbeat"'],
            output_directory,
        )
        master_paths = exporter.build_playlist_paths(
            ["House", "house", 'Discogs: "Breakbeat"'],
            output_directory,
        )

        self.assertEqual(folder_paths["House"], output_directory / "House")
        self.assertEqual(folder_paths["house"], output_directory / "house (2)")
        self.assertEqual(folder_paths['Discogs: "Breakbeat"'], output_directory / "Discogs_ _Breakbeat_")
        self.assertEqual(exporter.playlist_master_path(output_directory / "House"), output_directory / "House" / "House.csv")
        self.assertEqual(master_paths["House"], output_directory / "House" / "House.csv")
        self.assertEqual(master_paths["house"], output_directory / "house (2)" / "house (2).csv")

    def test_print_summary_includes_playlist_release_changes(self):
        summary = exporter.PlaylistExportSummary(
            input_rows=1,
            playlist_count=1,
            track_row_count=1,
            fallback_row_count=0,
            skipped_unassigned_count=0,
            input_path=Path("collection/enriched-collection.csv"),
            output_directory=Path("collection/playlists"),
            report_path=Path("reports/playlists_report.txt"),
            playlist_files=(
                exporter.PlaylistExportFile(
                    playlist_name="Discogs - Breakbeat",
                    path=Path("collection/playlists/Discogs - Breakbeat/Discogs - Breakbeat.csv"),
                    row_count=1,
                ),
            ),
            playlist_release_changes=(
                exporter.PlaylistReleaseChange(
                    playlist_name="Discogs - Breakbeat",
                    path=Path("collection/playlists/Discogs - Breakbeat/Discogs - Breakbeat.csv"),
                    added_releases=(
                        exporter.ReleaseReportEntry(
                            key=("release_id", "222", ""),
                            release_id="222",
                            artist_name="Beta Artist",
                            album_name="Beta Album",
                            track_row_count=1,
                        ),
                    ),
                    removed_releases=(),
                    added_tracks=(
                        exporter.TrackReportEntry(
                            key=("222", "beta album", "beta artist", "1", "beta one"),
                            release_id="222",
                            artist_name="Beta Artist",
                            album_name="Beta Album",
                            track_number="1",
                            track_name="Beta One",
                        ),
                    ),
                ),
            ),
            review_notes=(),
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exporter.print_summary(summary)

        self.assertNotIn("Added Tracks By Playlist", output.getvalue())
        self.assertNotIn("Beta Artist | Beta One | Beta Album | track 1 | Release ID 222", output.getvalue())
        self.assertNotIn("- Discogs - Breakbeat: collection/playlists/Discogs - Breakbeat.csv", output.getvalue())
        self.assertIn("Playlist Release Changes\n------------------------", output.getvalue())
        self.assertIn("222 | Beta Artist | Beta Album | 1 track row", output.getvalue())

    def test_format_playlist_release_change_lines_collapses_unchanged_playlists(self):
        lines = exporter.format_playlist_release_change_lines(
            (
                exporter.PlaylistReleaseChange(
                    playlist_name="Discogs - Techno",
                    path=Path("collection/playlists/Discogs - Techno/Discogs - Techno.csv"),
                    added_releases=(),
                    removed_releases=(),
                ),
            )
        )

        self.assertEqual(lines, ["- None"])


class TerminalStream(io.StringIO):
    def isatty(self):
        return True


if __name__ == "__main__":
    unittest.main()
