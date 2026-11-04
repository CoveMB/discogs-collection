import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import shared.reports as reports  # noqa: E402
from shared.reports import format_report_section, print_report_section  # noqa: E402


class ReportTests(unittest.TestCase):
    def test_text_report_replaces_existing_content_with_utf8_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "report.txt"
            reports.write_text_report(path, ["old content"])
            reports.write_text_report(path, (line for line in ("Résumé", "Next line")))
            self.assertEqual(path.read_bytes(), "Résumé\nNext line".encode("utf-8"))
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_text_report_replace_retains_previous_bytes_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.txt"
            path.write_text("previous deadline")
            error = OSError("replacement unavailable")
            with patch.object(Path, "replace", side_effect=error), self.assertRaises(OSError) as caught:
                reports.write_text_report(path, ["new content"])
            self.assertIs(caught.exception, error)
            self.assertEqual(path.read_text(), "previous deadline")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_script_report_path_uses_timestamp_then_script_name(self):
        script_report_path = reports.script_report_path
        self.assertIsNotNone(script_report_path)

        with patch("shared.reports.readable_timestamp", return_value="2026-06-10_14-30-00"):
            path = script_report_path(Path("scripts/discogs_style_enricher.py"))

        self.assertEqual(path, Path("reports/2026-06-10_14-30-00_discogs_style_enricher.txt"))

    def test_print_report_section_writes_formatted_section_lines(self):
        lines = ["Input rows: 2", "Output rows: 2"]

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            print_report_section("Processed", lines)

        self.assertEqual(stdout.getvalue().splitlines(), format_report_section("Processed", lines))


if __name__ == "__main__":
    unittest.main()
