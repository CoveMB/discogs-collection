from __future__ import annotations

import unittest

from scripts.dev_checks import check_text_content, is_checkable_text_path


class DevChecksTest(unittest.TestCase):
    def test_check_text_content_accepts_clean_text(self):
        self.assertEqual(check_text_content("README.md", b"clean line\n"), [])

    def test_check_text_content_reports_missing_final_newline(self):
        findings = check_text_content("README.md", b"clean line")

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].format(), "README.md:1: missing final newline")

    def test_check_text_content_reports_trailing_whitespace(self):
        findings = check_text_content("README.md", b"bad line \nnext\t\n")

        self.assertEqual([finding.format() for finding in findings], [
            "README.md:1: trailing whitespace",
            "README.md:2: trailing whitespace",
        ])

    def test_check_text_content_reports_non_utf8_text(self):
        findings = check_text_content("README.md", b"\xff\n")

        self.assertEqual(findings[0].format(), "README.md: file is not valid UTF-8")

    def test_is_checkable_text_path_includes_project_text_files(self):
        self.assertTrue(is_checkable_text_path("scripts/dev_checks.py"))
        self.assertTrue(is_checkable_text_path(".githooks/pre-commit"))
        self.assertTrue(is_checkable_text_path("AGENTS.md"))

    def test_is_checkable_text_path_excludes_private_workflow_data(self):
        self.assertFalse(is_checkable_text_path("collection/enriched-collection.csv"))
        self.assertFalse(is_checkable_text_path("config/playlist-map.json"))
        self.assertFalse(is_checkable_text_path("reports/run_report.txt"))


if __name__ == "__main__":
    unittest.main()
