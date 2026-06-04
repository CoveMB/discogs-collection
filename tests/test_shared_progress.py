import io
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from shared.progress import ProgressReporter  # noqa: E402


class TerminalStream(io.StringIO):
    def isatty(self):
        return True


class NonTerminalStream(io.StringIO):
    def isatty(self):
        return False


class ProgressReporterTests(unittest.TestCase):
    def test_updates_same_terminal_line_with_configurable_label_width_and_percentage(self):
        stream = TerminalStream()
        progress = ProgressReporter(stream=stream, width=10, label="Processing rows")

        progress.start(total=4)
        progress.update(current=2)
        progress.update(current=4)
        progress.finish()

        output = stream.getvalue()
        self.assertIn("\rProcessing rows [----------] 0/4 0%", output)
        self.assertIn("\rProcessing rows [#####-----] 2/4 50%", output)
        self.assertIn("\rProcessing rows [##########] 4/4 100%", output)
        self.assertTrue(output.endswith("\n"))

    def test_stays_quiet_when_stream_is_not_terminal(self):
        stream = NonTerminalStream()
        progress = ProgressReporter(stream=stream)

        progress.start(total=2)
        progress.update(current=1)
        progress.finish()

        self.assertEqual(stream.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
