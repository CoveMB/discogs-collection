import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from shared.reports import format_report_section, print_report_section  # noqa: E402


class ReportTests(unittest.TestCase):
    def test_print_report_section_writes_formatted_section_lines(self):
        lines = ["Input rows: 2", "Output rows: 2"]

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            print_report_section("Processed", lines)

        self.assertEqual(stdout.getvalue().splitlines(), format_report_section("Processed", lines))


if __name__ == "__main__":
    unittest.main()
