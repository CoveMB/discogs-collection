import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_playlist_mapper as mapper  # noqa: E402


def read_csv_text(csv_text):
    return list(csv.DictReader(io.StringIO(csv_text)))


def write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def sample_config():
    return {
        "playlist_prefix": "Discogs - ",
        "excluded_terms": ["Electronic", "Electro"],
        "playlists": {
            "Bossanova": ["Bossa Nova", "Bossanova"],
            "Breakbeat": ["Breakbeat", "Breaks"],
            "House": ["House", "Deep House", "Acid House"],
        },
    }


class PlaylistMapperTests(unittest.TestCase):
    def test_bossa_nova_aliases_map_to_configured_playlist_label(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "Bossa Nova, Bossanova", "Genre": ""},
                config,
            ),
            "Discogs - Bossanova",
        )

    def test_breakbeat_aliases_map_to_configured_playlist_label(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "Breakbeat, Breaks", "Genre": ""},
                config,
            ),
            "Discogs - Breakbeat",
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
            "Discogs - House",
        )

    def test_genre_mapping_is_used_only_when_style_has_no_mapped_playlist(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "Electronic", "Genre": "Breakbeat"},
                config,
            ),
            "Discogs - Breakbeat",
        )

    def test_one_row_can_get_multiple_ordered_playlists(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "House, Breaks", "Genre": ""},
                config,
            ),
            "Discogs - Breakbeat, Discogs - House",
        )

    def test_duplicate_source_terms_do_not_duplicate_playlists(self):
        config = mapper.normalize_playlist_config(sample_config())

        self.assertEqual(
            mapper.map_row_playlists(
                {"Style": "Breakbeat, Breaks, breakbeat", "Genre": ""},
                config,
            ),
            "Discogs - Breakbeat",
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

    def test_existing_columns_and_row_order_are_preserved(self):
        rows = read_csv_text(
            "Catalog#,Artist,Style,Genre,Custom\n"
            "A1,First,House,Electronic,keep\n"
            "B2,Second,,Breaks,also keep\n"
        )
        fieldnames = ["Catalog#", "Artist", "Style", "Genre", "Custom"]
        config = mapper.normalize_playlist_config(sample_config())

        output_fieldnames, output_rows = mapper.add_playlist_mappings(fieldnames, rows, config)

        self.assertEqual(
            output_fieldnames,
            ["Catalog#", "Artist", "Style", "Genre", "Playlists", "Custom"],
        )
        self.assertEqual([row["Artist"] for row in output_rows], ["First", "Second"])
        self.assertEqual(output_rows[0]["Custom"], "keep")
        self.assertEqual(output_rows[1]["Custom"], "also keep")
        self.assertEqual(output_rows[0]["Playlists"], "Discogs - House")
        self.assertEqual(output_rows[1]["Playlists"], "Discogs - Breakbeat")

    def test_ambiguous_config_fails_clearly(self):
        payload = sample_config()
        payload["playlists"]["House"].append("Breakbeat")

        with self.assertRaisesRegex(ValueError, "raw term appears under multiple playlist labels: Breakbeat"):
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
                    ]
                )

            self.assertEqual(exit_code, 0)
            output_rows = read_csv_text(output_path.read_text(encoding="utf-8"))
            self.assertEqual(output_rows[0]["Playlists"], "Discogs - House")
            self.assertEqual(output_rows[1]["Playlists"], "Discogs - Breakbeat")
            self.assertEqual(output_rows[2]["Playlists"], "Discogs - House")

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

            self.assertEqual(exit_code, 0)
            self.assertIn(f"Output: {output_path}", stdout.getvalue())
            self.assertIn(f"Report: {report_path}", stdout.getvalue())
            self.assertIn("Discogs playlist mapping report", report_text)
            self.assertIn(f"Input: {input_path}", report_text)
            self.assertIn(f"Output: {output_path}", report_text)
            self.assertIn(f"Config: {config_path}", report_text)
            self.assertIn("- 111: First Artist - First Title -> Discogs - House", report_text)
            self.assertIn("- 222: Second Artist - Second Title -> Discogs - Breakbeat", report_text)
            self.assertIn("- 333: Third Artist - Third Title -> Discogs - House", report_text)
            self.assertIn("- 444: Fourth Artist - Fourth Title -> None", report_text)

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

    def test_parse_args_defaults_to_playlist_report_path(self):
        with patch.object(mapper, "readable_timestamp", return_value="2026-06-05_15-30-00"):
            args = mapper.parse_args(["--input", "collection/enriched-collection.csv"])

        self.assertEqual(args.output, Path("collection/enriched-collection.csv"))
        self.assertEqual(
            args.report,
            Path("reports/enriched-collection_2026-06-05_15-30-00_playlist_report.txt"),
        )


if __name__ == "__main__":
    unittest.main()
