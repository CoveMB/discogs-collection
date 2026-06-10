import csv
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_playlist_exporter as exporter  # noqa: E402
import discogs_playlist_splitter as splitter  # noqa: E402


def playlist_row(release_id: str, track_number: int) -> dict[str, str]:
    return {
        "Release Id": release_id,
        "Album Name": f"Album {release_id or 'missing'}",
        "Track Number": str(track_number),
        "Track Name": f"Track {track_number}",
        "Artist Name": f"Artist {release_id or 'missing'}",
        "Spotify Search Query": f"Artist {release_id or 'missing'} Track {track_number}",
    }


def read_csv_file(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


def write_master(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=exporter.TUNEMYMUSIC_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def write_split(path: Path, rows: list[dict[str, str]]) -> None:
    write_master(path, rows)


class PlaylistSplitterTests(unittest.TestCase):
    def test_500_rows_produce_one_chunk_ranged_1_to_500(self):
        rows = [playlist_row(str(row_number), 1) for row_number in range(1, 501)]

        plan = splitter.plan_split_chunks(rows)

        self.assertEqual(len(plan.chunks), 1)
        self.assertEqual(plan.chunks[0].start_row_number, 1)
        self.assertEqual(plan.chunks[0].end_row_number, 500)
        self.assertEqual(len(plan.chunks[0].rows), 500)
        self.assertEqual(plan.warnings, ())

    def test_501_one_row_releases_produce_two_chunks(self):
        rows = [playlist_row(str(row_number), 1) for row_number in range(1, 502)]

        plan = splitter.plan_split_chunks(rows)

        self.assertEqual(
            [(chunk.start_row_number, chunk.end_row_number) for chunk in plan.chunks],
            [(1, 500), (501, 501)],
        )

    def test_multi_row_release_group_that_exactly_fills_limit_stays_in_chunk(self):
        rows = [playlist_row(str(row_number), 1) for row_number in range(1, 498)]
        rows.extend([playlist_row("498", 1), playlist_row("498", 2), playlist_row("498", 3)])

        plan = splitter.plan_split_chunks(rows)

        self.assertEqual(
            [(chunk.start_row_number, chunk.end_row_number) for chunk in plan.chunks],
            [(1, 500)],
        )

    def test_multi_row_release_group_that_exceeds_limit_starts_next_chunk(self):
        rows = [playlist_row(str(row_number), 1) for row_number in range(1, 499)]
        rows.extend([playlist_row("499", 1), playlist_row("499", 2), playlist_row("499", 3)])

        plan = splitter.plan_split_chunks(rows)

        self.assertEqual(
            [(chunk.start_row_number, chunk.end_row_number) for chunk in plan.chunks],
            [(1, 498), (499, 501)],
        )

    def test_single_release_larger_than_max_rows_is_split_with_warning(self):
        rows = [playlist_row("111", row_number) for row_number in range(1, 502)]

        plan = splitter.plan_split_chunks(rows)

        self.assertEqual(
            [(chunk.start_row_number, chunk.end_row_number) for chunk in plan.chunks],
            [(1, 500), (501, 501)],
        )
        self.assertTrue(any("111" in warning for warning in plan.warnings))

    def test_blank_release_id_rows_are_one_row_groups_with_warnings(self):
        rows = [
            playlist_row("111", 1),
            playlist_row("", 1),
            playlist_row("", 2),
            playlist_row("222", 1),
        ]

        plan = splitter.plan_split_chunks(rows, max_rows=2)

        self.assertEqual(
            [(chunk.start_row_number, chunk.end_row_number) for chunk in plan.chunks],
            [(1, 2), (3, 4)],
        )
        self.assertEqual(len(plan.warnings), 2)
        self.assertTrue(all("blank Release Id" in warning for warning in plan.warnings))

    def test_max_rows_less_than_one_raises_value_error(self):
        with self.assertRaises(ValueError):
            splitter.plan_split_chunks([], max_rows=0)

    def test_max_rows_over_default_limit_is_allowed_for_other_playlist_tools(self):
        rows = [playlist_row(str(row_number), 1) for row_number in range(1, 601)]

        plan = splitter.plan_split_chunks(rows, max_rows=600)

        self.assertEqual(len(plan.chunks), 1)
        self.assertEqual(plan.chunks[0].start_row_number, 1)
        self.assertEqual(plan.chunks[0].end_row_number, 600)
        self.assertEqual(len(plan.chunks[0].rows), 600)

    def test_write_regenerated_splits_removes_stale_csvs_and_preserves_non_csv_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_folder = directory / "playlists" / "House"
            master_path = playlist_folder / "House.csv"
            splits_directory = playlist_folder / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
            stale_csv = splits_directory / "old.csv"
            preserved_text = splits_directory / "notes.txt"
            write_master(
                master_path,
                [
                    playlist_row("111", 1),
                    playlist_row("222", 1),
                    playlist_row("333", 1),
                ],
            )
            splits_directory.mkdir()
            stale_csv.write_text("stale\n", encoding="utf-8")
            preserved_text.write_text("keep\n", encoding="utf-8")

            summary = splitter.write_regenerated_splits(master_path, max_rows=2)

            self.assertFalse(stale_csv.exists())
            self.assertTrue(preserved_text.exists())
            self.assertEqual(
                [path.name for path in summary.written_split_paths],
                ["1-2.csv", "3-3.csv"],
            )
            self.assertEqual(
                [path.name for path in summary.regenerated_split_paths],
                ["old.csv"],
            )
            self.assertEqual(
                [path.name for path in summary.preserved_split_paths],
                ["notes.txt"],
            )
            self.assertEqual(len(read_csv_file(splits_directory / "1-2.csv")), 2)
            self.assertEqual(len(read_csv_file(splits_directory / "3-3.csv")), 1)

    def test_write_regenerated_splits_rejects_master_without_tunemymusic_columns(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            master_path = directory / "playlists" / "House" / "House.csv"
            master_path.parent.mkdir(parents=True)
            master_path.write_text("Release Id,Album Name\n111,Alpha Album\n", encoding="utf-8")

            with self.assertRaises(ValueError) as context:
                splitter.write_regenerated_splits(master_path, max_rows=2)

            self.assertIn("missing TuneMyMusic columns", str(context.exception))

    def test_read_existing_split_files_sorts_range_filenames_by_row_order(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            splits_directory = Path(temporary_directory) / "splits"
            write_split(splits_directory / "11-12.csv", [playlist_row("111", 1), playlist_row("112", 1)])
            write_split(splits_directory / "1-10.csv", [playlist_row(str(row_number), 1) for row_number in range(1, 11)])

            existing_splits, warnings = splitter.read_existing_split_files(splits_directory)

            self.assertEqual([(split.start_row_number, split.end_row_number) for split in existing_splits], [(1, 10), (11, 12)])
            self.assertEqual(warnings, ())

    def test_read_existing_split_files_ignores_unparseable_csv_with_warning(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            splits_directory = Path(temporary_directory) / "splits"
            write_split(splits_directory / "1-1.csv", [playlist_row("111", 1)])
            write_split(splits_directory / "notes.csv", [playlist_row("222", 1)])

            existing_splits, warnings = splitter.read_existing_split_files(splits_directory)

            self.assertEqual([split.path.name for split in existing_splits], ["1-1.csv"])
            self.assertEqual(len(warnings), 1)
            self.assertIn("notes.csv", warnings[0])

    def test_read_existing_split_files_invalid_ranges_raise_value_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            splits_directory = Path(temporary_directory) / "splits"
            write_split(splits_directory / "0-10.csv", [playlist_row("111", 1)])

            with self.assertRaises(ValueError):
                splitter.read_existing_split_files(splits_directory)

        with tempfile.TemporaryDirectory() as temporary_directory:
            splits_directory = Path(temporary_directory) / "splits"
            write_split(splits_directory / "1-10.csv", [playlist_row("111", 1)])
            write_split(splits_directory / "10-12.csv", [playlist_row("222", 1)])

            with self.assertRaises(ValueError):
                splitter.read_existing_split_files(splits_directory)

    def test_read_existing_split_files_gapped_ranges_raise_value_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            splits_directory = Path(temporary_directory) / "splits"
            write_split(splits_directory / "1-1.csv", [playlist_row("111", 1)])
            write_split(splits_directory / "3-3.csv", [playlist_row("333", 1)])

            with self.assertRaises(ValueError) as context:
                splitter.read_existing_split_files(splits_directory)

            self.assertIn("gap", str(context.exception))
            self.assertIn("2", str(context.exception))

    def test_read_existing_split_files_row_count_mismatch_raises_value_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            splits_directory = Path(temporary_directory) / "splits"
            write_split(splits_directory / "1-10.csv", [playlist_row("111", 1)])

            with self.assertRaises(ValueError) as context:
                splitter.read_existing_split_files(splits_directory)

            self.assertIn("row count", str(context.exception))
            self.assertIn("1-10.csv", str(context.exception))

    def test_cli_default_returns_one_for_gapped_stable_split_ranges(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            splits_directory = output_directory / "House" / "splits"
            write_master(
                output_directory / "House" / "House.csv",
                [playlist_row("111", 1), playlist_row("222", 1), playlist_row("333", 1), playlist_row("444", 1)],
            )
            write_split(splits_directory / "1-1.csv", [playlist_row("111", 1)])
            write_split(splits_directory / "3-3.csv", [playlist_row("333", 1)])
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = splitter.main(
                    [
                        "--output-dir",
                        str(output_directory),
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("Error:", stderr.getvalue())
            self.assertIn("gap", stderr.getvalue())
            self.assertFalse((splits_directory / "4-4.csv").exists())

    def test_cli_default_returns_one_for_stable_split_row_count_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            splits_directory = output_directory / "House" / "splits"
            write_master(output_directory / "House" / "House.csv", [playlist_row(str(row_number), 1) for row_number in range(1, 12)])
            write_split(splits_directory / "1-10.csv", [playlist_row("111", 1)])
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = splitter.main(
                    [
                        "--output-dir",
                        str(output_directory),
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("Error:", stderr.getvalue())
            self.assertIn("row count", stderr.getvalue())
            self.assertFalse((splits_directory / "11-11.csv").exists())

    def test_write_stable_splits_preserves_existing_file_and_writes_new_range_for_appended_rows(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_folder = directory / "playlists" / "House"
            master_path = playlist_folder / "House.csv"
            splits_directory = playlist_folder / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
            first_rows = [playlist_row(str(row_number), 1) for row_number in range(1, 11)]
            master_rows = first_rows + [playlist_row("11", 1), playlist_row("12", 1)]
            write_master(master_path, master_rows)
            write_split(splits_directory / "1-10.csv", first_rows)
            original_content = (splits_directory / "1-10.csv").read_text(encoding="utf-8")

            summary = splitter.write_stable_splits(master_path, max_rows=500)

            self.assertEqual((splits_directory / "1-10.csv").read_text(encoding="utf-8"), original_content)
            self.assertTrue((splits_directory / "11-12.csv").exists())
            self.assertEqual([path.name for path in summary.preserved_split_paths], ["1-10.csv"])
            self.assertEqual([path.name for path in summary.written_split_paths], ["11-12.csv"])
            self.assertEqual(summary.regenerated_split_paths, ())

    def test_write_stable_splits_does_not_append_into_last_existing_split_even_with_capacity(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_folder = directory / "playlists" / "House"
            master_path = playlist_folder / "House.csv"
            splits_directory = playlist_folder / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
            first_rows = [playlist_row("111", 1)]
            write_master(master_path, first_rows + [playlist_row("222", 1)])
            write_split(splits_directory / "1-1.csv", first_rows)

            splitter.write_stable_splits(master_path, max_rows=500)

            self.assertEqual(len(read_csv_file(splits_directory / "1-1.csv")), 1)
            self.assertTrue((splits_directory / "2-2.csv").exists())

    def test_write_stable_splits_warns_when_first_new_row_continues_last_preserved_release(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_folder = directory / "playlists" / "House"
            master_path = playlist_folder / "House.csv"
            splits_directory = playlist_folder / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
            preserved_rows = [playlist_row("111", 1), playlist_row("111", 2)]
            master_rows = preserved_rows + [playlist_row("111", 3)]
            write_master(master_path, master_rows)
            write_split(splits_directory / "1-2.csv", preserved_rows)
            original_content = (splits_directory / "1-2.csv").read_text(encoding="utf-8")

            summary = splitter.write_stable_splits(master_path, max_rows=500)

            self.assertEqual((splits_directory / "1-2.csv").read_text(encoding="utf-8"), original_content)
            self.assertTrue((splits_directory / "3-3.csv").exists())
            self.assertTrue(any("111" in warning and "--regenerate" in warning for warning in summary.warnings))
            self.assertTrue(any("1-2" in warning and "3-3" in warning for warning in summary.warnings))

    def test_write_stable_splits_warns_and_preserves_existing_mismatched_split_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_folder = directory / "playlists" / "House"
            master_path = playlist_folder / "House.csv"
            splits_directory = playlist_folder / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
            master_rows = [playlist_row(str(row_number), 1) for row_number in range(1, 13)]
            write_master(master_path, master_rows)
            mismatched_rows = [playlist_row(str(row_number), 1) for row_number in range(1, 11)]
            mismatched_rows[0]["Track Name"] = "Frozen Track"
            write_split(splits_directory / "1-10.csv", mismatched_rows)
            original_content = (splits_directory / "1-10.csv").read_text(encoding="utf-8")

            summary = splitter.write_stable_splits(master_path, max_rows=500)

            self.assertEqual((splits_directory / "1-10.csv").read_text(encoding="utf-8"), original_content)
            self.assertTrue(any("1-10.csv" in warning for warning in summary.warnings))
            self.assertTrue((splits_directory / "11-12.csv").exists())

    def test_write_stable_splits_reports_unparseable_csv_in_splits_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_folder = directory / "playlists" / "House"
            master_path = playlist_folder / "House.csv"
            splits_directory = playlist_folder / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
            write_master(master_path, [playlist_row("111", 1), playlist_row("222", 1)])
            write_split(splits_directory / "1-1.csv", [playlist_row("111", 1)])
            write_split(splits_directory / "review.csv", [playlist_row("333", 1)])

            summary = splitter.write_stable_splits(master_path, max_rows=500)

            self.assertTrue(any("review.csv" in warning for warning in summary.warnings))
            self.assertTrue((splits_directory / "2-2.csv").exists())

    def test_write_stable_splits_without_existing_splits_behaves_like_regeneration(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            master_path = directory / "playlists" / "House" / "House.csv"
            write_master(master_path, [playlist_row("111", 1), playlist_row("222", 1), playlist_row("333", 1)])

            summary = splitter.write_stable_splits(master_path, max_rows=2)

            self.assertEqual([path.name for path in summary.written_split_paths], ["1-2.csv", "3-3.csv"])
            self.assertEqual(summary.preserved_split_paths, ())

    def test_write_stable_splits_with_only_non_range_csv_writes_all_chunks_and_preserves_review_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_folder = directory / "playlists" / "House"
            master_path = playlist_folder / "House.csv"
            splits_directory = playlist_folder / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
            review_file = splits_directory / "review.csv"
            write_master(master_path, [playlist_row("111", 1), playlist_row("222", 1)])
            write_split(review_file, [playlist_row("333", 1)])
            original_content = review_file.read_text(encoding="utf-8")

            summary = splitter.write_stable_splits(master_path, max_rows=1)

            self.assertEqual(review_file.read_text(encoding="utf-8"), original_content)
            self.assertEqual([path.name for path in summary.written_split_paths], ["1-1.csv", "2-2.csv"])
            self.assertTrue(any("review.csv" in warning for warning in summary.warnings))

    def test_write_stable_splits_oversized_first_new_release_group_splits_and_warns(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_folder = directory / "playlists" / "House"
            master_path = playlist_folder / "House.csv"
            splits_directory = playlist_folder / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
            write_master(master_path, [playlist_row("100", 1)] + [playlist_row("200", row_number) for row_number in range(1, 502)])
            write_split(splits_directory / "1-1.csv", [playlist_row("100", 1)])

            summary = splitter.write_stable_splits(master_path, max_rows=500)

            self.assertEqual([path.name for path in summary.written_split_paths], ["2-501.csv", "502-502.csv"])
            self.assertTrue(any("200" in warning for warning in summary.warnings))

    def test_write_regenerated_splits_rejects_symlinked_splits_directory_without_deleting_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_folder = directory / "playlists" / "House"
            master_path = playlist_folder / "House.csv"
            outside_directory = directory / "outside-splits"
            victim_path = outside_directory / "victim.csv"
            write_master(master_path, [playlist_row("111", 1)])
            outside_directory.mkdir()
            victim_path.write_text("do not delete\n", encoding="utf-8")
            (playlist_folder / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME).symlink_to(
                outside_directory,
                target_is_directory=True,
            )

            with self.assertRaises(ValueError) as context:
                splitter.write_regenerated_splits(master_path, max_rows=1)

            self.assertIn("symlink", str(context.exception))
            self.assertTrue(victim_path.exists())

    def test_write_stable_splits_rejects_symlinked_splits_directory_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_folder = directory / "playlists" / "House"
            master_path = playlist_folder / "House.csv"
            outside_directory = directory / "outside-splits"
            victim_path = outside_directory / "victim.csv"
            write_master(master_path, [playlist_row("111", 1)])
            outside_directory.mkdir()
            victim_path.write_text("do not delete\n", encoding="utf-8")
            (playlist_folder / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME).symlink_to(
                outside_directory,
                target_is_directory=True,
            )

            with self.assertRaises(ValueError) as context:
                splitter.write_stable_splits(master_path, max_rows=1)

            self.assertIn("symlink", str(context.exception))
            self.assertTrue(victim_path.exists())

    def test_write_stable_splits_rejects_symlinked_playlist_folder_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            outside_folder = directory / "outside-playlist"
            output_directory.mkdir()
            write_master(outside_folder / "Linked.csv", [playlist_row("111", 1)])
            (output_directory / "Linked").symlink_to(outside_folder, target_is_directory=True)

            with self.assertRaises(ValueError) as context:
                splitter.update_playlist_splits(output_directory, "all", max_rows=1)

            self.assertIn("symlink", str(context.exception))
            self.assertFalse((outside_folder / "splits").exists())

    def test_cli_default_without_regenerate_runs_stable_update(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            splits_directory = output_directory / "House" / "splits"
            first_rows = [playlist_row("111", 1)]
            write_master(output_directory / "House" / "House.csv", first_rows + [playlist_row("222", 1)])
            write_split(splits_directory / "1-1.csv", first_rows)

            result = splitter.main(
                [
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--max-rows",
                    "500",
                ]
            )

            self.assertEqual(result, 0)
            self.assertTrue((splits_directory / "2-2.csv").exists())
            self.assertIn("Preserved split CSVs", report_path.read_text(encoding="utf-8"))

    def test_cli_accepts_max_rows_over_default_limit_for_other_playlist_tools(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            splits_directory = output_directory / "House" / "splits"
            write_master(
                output_directory / "House" / "House.csv",
                [playlist_row(str(row_number), 1) for row_number in range(1, 502)],
            )
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = splitter.main(
                    [
                        "--output-dir",
                        str(output_directory),
                        "--report",
                        str(report_path),
                        "--max-rows",
                        "501",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertTrue((splits_directory / "1-501.csv").exists())
            self.assertTrue(report_path.exists())

    def test_cli_default_without_regenerate_stable_updates_all_playlist_folders(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            house_splits_directory = output_directory / "House" / "splits"
            techno_splits_directory = output_directory / "Techno" / "splits"
            write_master(output_directory / "House" / "House.csv", [playlist_row("111", 1), playlist_row("222", 1)])
            write_master(output_directory / "Techno" / "Techno.csv", [playlist_row("333", 1), playlist_row("444", 1)])
            write_split(house_splits_directory / "1-1.csv", [playlist_row("111", 1)])
            write_split(techno_splits_directory / "1-1.csv", [playlist_row("333", 1)])
            (output_directory / "No Master").mkdir(parents=True)

            result = splitter.main(
                [
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--max-rows",
                    "500",
                ]
            )

            self.assertEqual(result, 0)
            self.assertTrue((house_splits_directory / "2-2.csv").exists())
            self.assertTrue((techno_splits_directory / "2-2.csv").exists())
            self.assertFalse((output_directory / "No Master" / "splits").exists())
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("- House:", report_text)
            self.assertIn("- Techno:", report_text)
            self.assertIn("Playlists processed: 2", report_text)

    def test_cli_default_report_lists_preserved_written_and_warning_entries(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            splits_directory = output_directory / "House" / "splits"
            master_rows = [playlist_row("111", 1), playlist_row("222", 1)]
            mismatched_rows = [playlist_row("111", 1)]
            mismatched_rows[0]["Track Name"] = "Frozen Track"
            write_master(output_directory / "House" / "House.csv", master_rows)
            write_split(splits_directory / "1-1.csv", mismatched_rows)
            write_split(splits_directory / "review.csv", [playlist_row("333", 1)])

            result = splitter.main(
                [
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--max-rows",
                    "500",
                ]
            )

            self.assertEqual(result, 0)
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Playlist folder:", report_text)
            self.assertIn("Master CSV:", report_text)
            self.assertIn("Preserved split CSVs:", report_text)
            self.assertIn(str(splits_directory / "1-1.csv"), report_text)
            self.assertIn("New split CSVs written:", report_text)
            self.assertIn(str(splits_directory / "2-2.csv"), report_text)
            self.assertIn("Warnings:", report_text)
            self.assertIn("existing split content differs", report_text)
            self.assertIn("review.csv", report_text)

    def test_cli_regenerate_exact_folder_target_works(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            write_master(
                output_directory / "House" / "House.csv",
                [playlist_row("111", 1), playlist_row("222", 1), playlist_row("333", 1)],
            )

            result = splitter.main(
                [
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--regenerate",
                    "House",
                    "--max-rows",
                    "2",
                ]
            )

            self.assertEqual(result, 0)
            self.assertTrue((output_directory / "House" / "splits" / "1-2.csv").exists())
            self.assertTrue((output_directory / "House" / "splits" / "3-3.csv").exists())
            self.assertIn("House.csv", report_path.read_text(encoding="utf-8"))

    def test_cli_regenerate_display_name_target_uses_safe_filename(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            folder_name = exporter.safe_playlist_filename('Discogs: "Breakbeat"')
            write_master(
                output_directory / folder_name / f"{folder_name}.csv",
                [playlist_row("111", 1), playlist_row("222", 1)],
            )

            result = splitter.main(
                [
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--regenerate",
                    'Discogs: "Breakbeat"',
                    "--max-rows",
                    "1",
                ]
            )

            self.assertEqual(result, 0)
            self.assertTrue((output_directory / folder_name / "splits" / "1-1.csv").exists())
            self.assertTrue((output_directory / folder_name / "splits" / "2-2.csv").exists())

    def test_cli_regenerate_all_processes_every_playlist_folder_with_master(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            write_master(output_directory / "House" / "House.csv", [playlist_row("111", 1)])
            write_master(output_directory / "Techno" / "Techno.csv", [playlist_row("222", 1)])
            (output_directory / "No Master").mkdir(parents=True)

            result = splitter.main(
                [
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--regenerate",
                    "all",
                    "--max-rows",
                    "1",
                ]
            )

            self.assertEqual(result, 0)
            self.assertTrue((output_directory / "House" / "splits" / "1-1.csv").exists())
            self.assertTrue((output_directory / "Techno" / "splits" / "1-1.csv").exists())
            self.assertFalse((output_directory / "No Master" / "splits").exists())

    def test_cli_regenerate_all_still_rebuilds_existing_splits(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            splits_directory = output_directory / "House" / "splits"
            write_master(output_directory / "House" / "House.csv", [playlist_row("111", 1), playlist_row("222", 1)])
            write_split(splits_directory / "1-1.csv", [playlist_row("stale", 1)])

            result = splitter.main(
                [
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--regenerate",
                    "all",
                    "--max-rows",
                    "2",
                ]
            )

            self.assertEqual(result, 0)
            self.assertFalse((splits_directory / "1-1.csv").exists())
            self.assertTrue((splits_directory / "1-2.csv").exists())

    def test_cli_regenerate_report_lists_regenerated_split_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            splits_directory = output_directory / "House" / "splits"
            stale_split_path = splits_directory / "1-1.csv"
            write_master(output_directory / "House" / "House.csv", [playlist_row("111", 1), playlist_row("222", 1)])
            write_split(stale_split_path, [playlist_row("stale", 1)])

            result = splitter.main(
                [
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--regenerate",
                    "all",
                    "--max-rows",
                    "2",
                ]
            )

            self.assertEqual(result, 0)
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Regenerated split CSVs:", report_text)
            self.assertIn(str(stale_split_path), report_text)
            self.assertIn("New split CSVs written:", report_text)
            self.assertIn(str(splits_directory / "1-2.csv"), report_text)

    def test_cli_rejects_relative_path_regenerate_target_without_touching_outside_playlist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            outside_folder = directory / "outside"
            write_master(outside_folder / "outside.csv", [playlist_row("111", 1)])
            output_directory.mkdir()
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = splitter.main(
                    [
                        "--output-dir",
                        str(output_directory),
                        "--report",
                        str(report_path),
                        "--regenerate",
                        "../outside",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("Error:", stderr.getvalue())
            self.assertFalse((outside_folder / "splits").exists())

    def test_cli_rejects_dot_dot_regenerate_target_without_touching_parent_splits(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            parent_master_path = directory / "...csv"
            parent_splits_directory = directory / "splits"
            parent_stale_csv = parent_splits_directory / "victim.csv"
            write_master(parent_master_path, [playlist_row("111", 1)])
            output_directory.mkdir()
            parent_splits_directory.mkdir()
            parent_stale_csv.write_text("do not delete\n", encoding="utf-8")
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = splitter.main(
                    [
                        "--output-dir",
                        str(output_directory),
                        "--report",
                        str(report_path),
                        "--regenerate",
                        "..",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("Error:", stderr.getvalue())
            self.assertTrue(parent_stale_csv.exists())
            self.assertFalse((parent_splits_directory / "1-1.csv").exists())

    def test_cli_rejects_absolute_regenerate_target_without_touching_outside_playlist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            outside_folder = directory / "outside"
            write_master(outside_folder / "outside.csv", [playlist_row("111", 1)])
            output_directory.mkdir()
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = splitter.main(
                    [
                        "--output-dir",
                        str(output_directory),
                        "--report",
                        str(report_path),
                        "--regenerate",
                        str(outside_folder),
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("Error:", stderr.getvalue())
            self.assertFalse((outside_folder / "splits").exists())

    def test_cli_regenerate_all_rejects_symlinked_playlist_folder_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            outside_folder = directory / "outside-playlist"
            output_directory.mkdir()
            write_master(outside_folder / "Linked.csv", [playlist_row("111", 1)])
            (output_directory / "Linked").symlink_to(outside_folder, target_is_directory=True)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = splitter.main(
                    [
                        "--output-dir",
                        str(output_directory),
                        "--report",
                        str(report_path),
                        "--regenerate",
                        "all",
                        "--max-rows",
                        "1",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("Error:", stderr.getvalue())
            self.assertIn("symlink", stderr.getvalue())
            self.assertFalse((outside_folder / "splits").exists())

    def test_cli_returns_one_and_prints_error_to_stderr_for_missing_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            report_path = directory / "split-report.txt"
            output_directory.mkdir()
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = splitter.main(
                    [
                        "--output-dir",
                        str(output_directory),
                        "--report",
                        str(report_path),
                        "--regenerate",
                        "Missing Playlist",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("Error:", stderr.getvalue())
            self.assertIn("Missing Playlist", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
