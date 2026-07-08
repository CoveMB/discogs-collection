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

from shared.cli import (  # noqa: E402
    ConsoleSection,
    files_section,
    print_cli_summary,
    print_console_sections,
    print_step_header,
    processed_section,
    run_cli,
)


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

    def test_print_console_sections_uses_shared_section_format(self):
        stdout = io.StringIO()

        with patch("sys.stdout", stdout):
            print_console_sections(
                [
                    ConsoleSection("Files", ("Report: reports/run.txt",)),
                    ConsoleSection("Processed", ("Rows: 3",)),
                ]
            )

        self.assertEqual(
            stdout.getvalue().splitlines(),
            [
                "",
                "Files",
                "-----",
                "Report: reports/run.txt",
                "",
                "Processed",
                "---------",
                "Rows: 3",
            ],
        )

    def test_print_cli_summary_prints_standard_files_and_processed_sections(self):
        stdout = io.StringIO()

        with patch("sys.stdout", stdout):
            print_cli_summary(
                files=("Report: reports/run.txt",),
                processed=("Rows: 3",),
                extra_sections=(ConsoleSection("Review", ("None",)),),
            )

        self.assertEqual(
            stdout.getvalue().splitlines(),
            [
                "",
                "Files",
                "-----",
                "Report: reports/run.txt",
                "",
                "Processed",
                "---------",
                "Rows: 3",
                "",
                "Review",
                "------",
                "None",
            ],
        )

    def test_named_summary_section_helpers_materialize_lines_once(self):
        files = files_section(line for line in ("Output: out.csv", "Report: report.txt"))
        processed = processed_section(["Rows: 3"])

        self.assertEqual(files, ConsoleSection("Files", ("Output: out.csv", "Report: report.txt")))
        self.assertEqual(processed, ConsoleSection("Processed", ("Rows: 3",)))

    def test_print_step_header_uses_numbered_section_shape(self):
        stdout = io.StringIO()

        with patch("sys.stdout", stdout):
            print_step_header("Discogs playlist splitter", step_index=4, total_steps=5)

        self.assertEqual(
            stdout.getvalue().splitlines(),
            [
                "",
                "Step 4/5",
                "--------",
                "Running: Discogs playlist splitter",
            ],
        )


if __name__ == "__main__":
    unittest.main()
