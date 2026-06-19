import re
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))


class SharedDebugLogTests(unittest.TestCase):
    def test_none_path_disables_debug_logging(self):
        from shared.debug_log import build_debug_logger

        self.assertIsNone(build_debug_logger(None))

    def test_build_debug_logger_creates_parent_directory_truncates_and_timestamps_lines(self):
        from shared.debug_log import build_debug_logger

        with tempfile.TemporaryDirectory() as temporary_directory:
            debug_log_path = Path(temporary_directory) / "logs" / "debug.log"
            debug_log_path.parent.mkdir()
            debug_log_path.write_text("stale log line\n", encoding="utf-8")

            debug_log = build_debug_logger(debug_log_path)
            self.assertIsNotNone(debug_log)
            debug_log("first event")
            debug_log("second event")

            debug_text = debug_log_path.read_text(encoding="utf-8")

        self.assertNotIn("stale log line", debug_text)
        self.assertRegex(
            debug_text,
            re.compile(
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 first event\n"
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 second event\n$"
            ),
        )


if __name__ == "__main__":
    unittest.main()
