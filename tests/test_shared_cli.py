import csv
import io
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from shared.cli import run_cli  # noqa: E402


@dataclass(frozen=True)
class Summary:
    error_count: int = 0


class SharedCliTests(unittest.TestCase):
    def test_run_cli_runs_parse_run_summary_and_uses_custom_exit_code(self):
        calls: list[tuple[str, object]] = []

        def parse_args(argv):
            calls.append(("parse", tuple(argv or ())))
            return {"parsed": True}

        def run(args):
            calls.append(("run", args))
            return Summary(error_count=2)

        def print_summary(summary):
            calls.append(("summary", summary))

        exit_code = run_cli(
            parse_args=parse_args,
            run=run,
            print_summary=print_summary,
            argv=["--flag"],
            success_exit_code=lambda summary: 2 if summary.error_count else 0,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            calls,
            [
                ("parse", ("--flag",)),
                ("run", {"parsed": True}),
                ("summary", Summary(error_count=2)),
            ],
        )

    def test_run_cli_prints_expected_errors_to_stderr(self):
        def parse_args(_argv):
            raise csv.Error("bad csv")

        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            exit_code = run_cli(
                parse_args=parse_args,
                run=lambda _args: Summary(),
                print_summary=lambda _summary: None,
                argv=[],
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "Error: bad csv\n")


if __name__ == "__main__":
    unittest.main()
