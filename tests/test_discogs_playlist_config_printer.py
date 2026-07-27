import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import sample_playlist_config as sample_config, write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_playlist_config_printer as printer  # noqa: E402


class PlaylistConfigPrinterTests(unittest.TestCase):
    def test_cli_prints_human_readable_playlist_association_rules_and_current_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "playlist-map.json"
            write_json(config_path, sample_config())

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = printer.main(["--config", str(config_path)])

            output = stdout.getvalue()

            self.assertEqual(exit_code, 0)
            self.assertIn(f"Playlist config: {config_path}", output)
            self.assertIn("How playlist association works:", output)
            self.assertIn("1. Split Style and Genre into comma-separated Discogs terms.", output)
            self.assertIn("4. Check Style aliases first.", output)
            self.assertIn("6. Use Genre aliases only when Style creates no playlist.", output)
            self.assertIn("8. Allow the same raw term under multiple playlist labels.", output)
            self.assertIn("Excluded raw Discogs terms\n--------------------------", output)
            self.assertIn("- Electronic", output)
            self.assertIn("- Electro", output)
            self.assertIn("Playlist labels and raw Discogs terms\n-------------------------------------", output)
            self.assertIn("- Bossanova -> Bossanova", output)
            self.assertIn("Raw Discogs terms: Bossa Nova, Bossanova", output)
            self.assertIn("- Breakbeat -> Breakbeat", output)
            self.assertIn("Raw Discogs terms: Breakbeat, Breaks", output)

    def test_cli_prints_ordered_and_authoritative_empty_configured_release_playlists(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "playlist-map.json"
            payload = sample_config()
            payload["release_playlists"] = {
                "My Latest Discovery": ["4390198", "123456"],
                "Clear This Playlist": [],
            }
            write_json(config_path, payload)

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = printer.main(["--config", str(config_path)])

            output = stdout.getvalue()

            self.assertEqual(exit_code, 0)
            self.assertIn("Configured release playlists\n----------------------------", output)
            self.assertIn("- My Latest Discovery\n  Release IDs: 4390198, 123456", output)
            self.assertIn("- Clear This Playlist\n  Release IDs: None (authoritative empty playlist)", output)

    def test_cli_creates_missing_config_and_prints_blank_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config" / "playlist-map.json"

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = printer.main(["--config", str(config_path)])

            output = stdout.getvalue()

            self.assertEqual(exit_code, 0)
            self.assertTrue(config_path.exists())
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                {
                    "excluded_terms": [],
                    "playlists": {},
                    "release_playlists": {},
                },
            )
            self.assertIn("Status: Created blank playlist config.", output)
            self.assertIn("- None configured.", output)
            self.assertIn("No playlists configured yet.", output)

    def test_main_reports_os_errors_with_shared_cli_boundary(self):
        with (
            patch.object(printer, "ensure_playlist_config_file", side_effect=OSError("disk full")),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            exit_code = printer.main(["--config", "config/playlist-map.json"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "Error: disk full\n")


if __name__ == "__main__":
    unittest.main()
