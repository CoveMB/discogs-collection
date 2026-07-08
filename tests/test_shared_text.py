import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from shared.text import clean_cell, display_report_value  # noqa: E402


class SharedTextTests(unittest.TestCase):
    def test_clean_cell_stringifies_blank_safe_values_and_strips_whitespace(self):
        self.assertEqual(clean_cell("  value  "), "value")
        self.assertEqual(clean_cell(None), "")
        self.assertEqual(clean_cell(123), "123")

    def test_display_report_value_collapses_whitespace_and_marks_blank_values(self):
        self.assertEqual(display_report_value("  one \n two  "), "one two")
        self.assertEqual(display_report_value(None), "(blank)")
        self.assertEqual(display_report_value("   "), "(blank)")


if __name__ == "__main__":
    unittest.main()
