import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import read_csv_text, sample_playlist_config as sample_config, write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_playlist_mapper as mapper  # noqa: E402


class PlaylistMapperTests(unittest.TestCase):
    def test_release_playlists_distinguishes_omitted_null_and_empty_object(self):
        omitted_payload: dict[str, object] = dict(sample_config())
        del omitted_payload["release_playlists"]
        empty_payload: dict[str, object] = dict(sample_config())
        empty_payload["release_playlists"] = {}
        named_empty_payload: dict[str, object] = dict(sample_config())
        named_empty_payload["release_playlists"] = {"Clear This Playlist": []}
        null_payload: dict[str, object] = dict(sample_config())
        null_payload["release_playlists"] = None

        self.assertEqual(mapper.normalize_playlist_config(omitted_payload).release_playlists, ())
        self.assertEqual(mapper.normalize_playlist_config(empty_payload).release_playlists, ())
        named_empty_definitions = mapper.normalize_playlist_config(named_empty_payload).release_playlists
        self.assertEqual(
            [(definition.name, definition.release_ids) for definition in named_empty_definitions],
            [("Clear This Playlist", ())],
        )
        with self.assertRaisesRegex(ValueError, "release_playlists must be an object"):
            mapper.normalize_playlist_config(null_payload)

    def test_legacy_config_defaults_to_no_release_playlists(self):
        payload: dict[str, object] = dict(sample_config())
        del payload["release_playlists"]

        config = mapper.normalize_playlist_config(payload)

        self.assertEqual(config.release_playlists, ())

    def test_release_playlists_preserve_playlist_and_release_order_and_allow_empty(self):
        payload: dict[str, object] = dict(sample_config())
        payload["release_playlists"] = {
            "My Latest Discovery": ["4390198", "123456"],
            "Clear This Playlist": [],
        }

        config = mapper.normalize_playlist_config(payload)

        self.assertEqual(
            [(definition.name, definition.release_ids) for definition in config.release_playlists],
            [
                ("My Latest Discovery", ("4390198", "123456")),
                ("Clear This Playlist", ()),
            ],
        )

    def test_release_playlist_ids_must_be_unique_positive_integer_strings(self):
        invalid_values = (
            [4390198],
            ["0"],
            ["-1"],
            ["1.5"],
            [""],
            ["111", "111"],
        )
        for release_ids in invalid_values:
            with self.subTest(release_ids=release_ids):
                payload: dict[str, object] = dict(sample_config())
                payload["release_playlists"] = {"My Playlist": release_ids}

                with self.assertRaises(ValueError):
                    mapper.normalize_playlist_config(payload)

    def test_release_playlist_sanitized_folder_collisions_are_rejected(self):
        payload: dict[str, object] = dict(sample_config())
        payload["release_playlists"] = {
            "Friday/Picks": ["111"],
            "Friday:Picks": ["222"],
        }

        with self.assertRaisesRegex(ValueError, "same release playlist folder"):
            mapper.normalize_playlist_config(payload)

    def test_release_playlist_names_must_resolve_to_strict_direct_child_folders(self):
        for playlist_name in (".", ".."):
            with self.subTest(playlist_name=playlist_name):
                payload: dict[str, object] = dict(sample_config())
                payload["release_playlists"] = {playlist_name: ["111"]}

                with self.assertRaisesRegex(ValueError, "strict direct child"):
                    mapper.normalize_playlist_config(payload)

    def test_release_playlist_membership_does_not_change_enriched_playlist_column(self):
        payload: dict[str, object] = dict(sample_config())
        payload["release_playlists"] = {"Selected Releases": ["111"]}
        config = mapper.normalize_playlist_config(payload)

        output_fieldnames, output_rows = mapper.add_playlist_mappings(
            ["release_id", "Artist", "Style", "Genre"],
            [{"release_id": "111", "Artist": "Artist", "Style": "", "Genre": ""}],
            config,
        )

        self.assertIn("Playlists", output_fieldnames)
        self.assertEqual(output_rows[0]["Playlists"], "")

    def test_bossa_nova_aliases_map_to_configured_playlist_label(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "Bossa Nova, Bossanova", "Genre": ""},
                config,
            ),
            "Bossanova",
        )

    def test_breakbeat_aliases_map_to_configured_playlist_label(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "Breakbeat, Breaks", "Genre": ""},
                config,
            ),
            "Breakbeat",
        )

    def test_excluded_terms_never_create_playlists(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "Electronic, Electro", "Genre": ""},
                config,
            ),
            "",
        )

    def test_style_mapping_takes_precedence_over_genre_mapping(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "House", "Genre": "Breakbeat"},
                config,
            ),
            "House",
        )

    def test_genre_mapping_is_used_only_when_style_has_no_mapped_playlist(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "Electronic", "Genre": "Breakbeat"},
                config,
            ),
            "Breakbeat",
        )

    def test_one_row_can_get_multiple_ordered_playlists(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "House, Breaks", "Genre": ""},
                config,
            ),
            "Breakbeat, House",
        )

    def test_duplicate_source_terms_do_not_duplicate_playlists(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "Breakbeat, Breaks, breakbeat", "Genre": ""},
                config,
            ),
            "Breakbeat",
        )

    def test_unmapped_rows_get_blank_playlists(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "Modern Classical", "Genre": "Classical"},
                config,
            ),
            "",
        )

    def test_existing_columns_and_row_order_are_preserved_with_release_id_first(self):
        rows = read_csv_text(
            "Catalog#,Artist,Style,Genre,release_id,Custom\n"
            "A1,First,House,Electronic,111,keep\n"
            "B2,Second,,Breaks,222,also keep\n"
        )
        fieldnames = ["Catalog#", "Artist", "Style", "Genre", "release_id", "Custom"]
        config = mapper.normalize_playlist_config(sample_config())

        output_fieldnames, output_rows = mapper.add_playlist_mappings(fieldnames, rows, config)

        self.assertEqual(
            output_fieldnames,
            ["release_id", "Catalog#", "Artist", "Style", "Genre", "Playlists", "Custom"],
        )
        self.assertEqual([row["Artist"] for row in output_rows], ["First", "Second"])
        self.assertEqual(output_rows[0]["Custom"], "keep")
        self.assertEqual(output_rows[1]["Custom"], "also keep")
        self.assertEqual(output_rows[0]["release_id"], "111")
        self.assertEqual(output_rows[1]["release_id"], "222")
        self.assertEqual(output_rows[0]["Playlists"], "House")
        self.assertEqual(output_rows[1]["Playlists"], "Breakbeat")

    def test_one_raw_term_can_create_multiple_playlist_labels(self):
        payload = sample_config()
        payload["playlists"]["House"].append("Breakbeat")

        config = mapper.normalize_playlist_config(payload)

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "Breakbeat", "Genre": ""},
                config,
            ),
            "Breakbeat, House",
        )

    def test_playlist_prefix_config_is_rejected_for_playlist_map(self):
        payload: dict[str, object] = dict(sample_config())
        payload["playlist_prefix"] = "Discogs - "

        with self.assertRaisesRegex(ValueError, "unknown playlist config key: playlist_prefix"):
            mapper.normalize_playlist_config(payload)

    def test_excluded_playlist_overlap_fails_clearly(self):
        payload = sample_config()
        payload["excluded_terms"].append("Breakbeat")

        with self.assertRaisesRegex(ValueError, "raw term appears in excluded_terms and playlists: Breakbeat"):
            mapper.normalize_playlist_config(payload)

    def test_cli_writes_expected_csv_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "enriched.csv"
            output_path = directory / "mapped.csv"
            report_path = directory / "playlist-report.txt"
            config_path = directory / "playlist-map.json"
            input_path.write_text(
                "Catalog#,Artist,Style,Genre\n"
                "A1,First,House,Electronic\n"
                "B2,Second,,Breaks\n"
                "C3,Third,Electronic,House\n",
                encoding="utf-8",
            )
            write_json(config_path, sample_config())

            with patch("sys.stdout", new_callable=io.StringIO):
                exit_code = mapper.main(
                    [
                        "--input",
                        str(input_path),
                        "--output",
                        str(output_path),
                        "--config",
                        str(config_path),
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            output_rows = read_csv_text(output_path.read_text(encoding="utf-8"))
            self.assertEqual(output_rows[0]["Playlists"], "House")
            self.assertEqual(output_rows[1]["Playlists"], "Breakbeat")
            self.assertEqual(output_rows[2]["Playlists"], "House")

    def test_cli_writes_report_listing_every_release_playlist_association(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "enriched.csv"
            output_path = directory / "mapped.csv"
            report_path = directory / "playlist-report.txt"
            config_path = directory / "playlist-map.json"
            input_path.write_text(
                "release_id,Artist,Title,Style,Genre\n"
                "111,First Artist,First Title,House,Electronic\n"
                "222,Second Artist,Second Title,,Breaks\n"
                "333,Third Artist,Third Title,Electronic,House\n"
                "444,Fourth Artist,Fourth Title,Classical,\n",
                encoding="utf-8",
            )
            write_json(config_path, sample_config())

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = mapper.main(
                    [
                        "--input",
                        str(input_path),
                        "--output",
                        str(output_path),
                        "--config",
                        str(config_path),
                        "--report",
                        str(report_path),
                    ]
                )

            report_text = report_path.read_text(encoding="utf-8")
            stdout_text = stdout.getvalue()

            self.assertEqual(exit_code, 0)
            self.assertIn(f"Output: {output_path}", stdout_text)
            self.assertIn(f"Report: {report_path}", stdout_text)
            self.assertIn(
                "Release to playlist associations\n"
                "--------------------------------\n"
                "- Release ID: 111\n"
                "  Artist: First Artist\n"
                "  Title: First Title\n"
                "  Playlists: House",
                stdout_text,
            )
            self.assertIn(
                "- Release ID: 444\n"
                "  Artist: Fourth Artist\n"
                "  Title: Fourth Title\n"
                "  Playlists: None",
                stdout_text,
            )
            self.assertIn("Discogs playlist mapping report", report_text)
            self.assertIn(f"Input: {input_path}", report_text)
            self.assertIn(f"Output: {output_path}", report_text)
            self.assertIn(f"Config: {config_path}", report_text)
            self.assertIn(
                "Discogs playlist mapping report\n"
                "===============================\n"
                "\n"
                "Summary\n"
                "-------\n"
                "- Input rows: 4\n"
                "- Output rows: 4\n"
                "\n"
                "Files\n"
                "-----",
                report_text,
            )
            self.assertIn(
                "- Release ID: 111\n"
                "  Artist: First Artist\n"
                "  Title: First Title\n"
                "  Playlists: House",
                report_text,
            )
            self.assertIn(
                "- Release ID: 222\n"
                "  Artist: Second Artist\n"
                "  Title: Second Title\n"
                "  Playlists: Breakbeat",
                report_text,
            )
            self.assertIn(
                "- Release ID: 333\n"
                "  Artist: Third Artist\n"
                "  Title: Third Title\n"
                "  Playlists: House",
                report_text,
            )
            self.assertIn(
                "- Release ID: 444\n"
                "  Artist: Fourth Artist\n"
                "  Title: Fourth Title\n"
                "  Playlists: None",
                report_text,
            )

    def test_cli_stops_after_creating_missing_config_when_stdin_is_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "enriched.csv"
            output_path = directory / "mapped.csv"
            config_path = directory / "config" / "playlist-map.json"
            input_path.write_text(
                "Artist,Style,Genre\n"
                "First,House,Electronic\n",
                encoding="utf-8",
            )

            with (
                patch("builtins.input", side_effect=EOFError),
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                exit_code = mapper.main(
                    [
                        "--input",
                        str(input_path),
                        "--output",
                        str(output_path),
                        "--config",
                        str(config_path),
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertTrue(config_path.exists())
            self.assertFalse(output_path.exists())
            self.assertIn("fill the playlist map config", stderr.getvalue())

    def test_cli_reports_missing_enriched_collection_clearly(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "collection" / "enriched-collection.csv"
            config_path = directory / "playlist-map.json"
            write_json(config_path, sample_config())

            with (
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                exit_code = mapper.main(
                    [
                        "--input",
                        str(input_path),
                        "--config",
                        str(config_path),
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertIn(
                f"No enriched collection found {input_path.parent} process your collection first with "
                "python3 scripts/discogs_style_enricher.py",
                stderr.getvalue(),
            )

    def test_main_reports_csv_errors_with_shared_cli_boundary(self):
        with (
            patch.object(mapper, "parse_args", return_value=object()),
            patch.object(mapper, "run_playlist_mapping", side_effect=csv.Error("bad csv")),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            exit_code = mapper.main([])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "Error: bad csv\n")

    def test_parse_args_defaults_to_playlist_report_path(self):
        with patch("shared.reports.readable_timestamp", return_value="2026-06-05_15-30-00"):
            args = mapper.parse_args(["--input", "collection/enriched-collection.csv"])

        self.assertEqual(args.output, Path("collection/enriched-collection.csv"))
        self.assertEqual(
            args.report,
            Path("reports/2026-06-05_15-30-00_discogs_playlist_mapper.txt"),
        )


if __name__ == "__main__":
    unittest.main()
