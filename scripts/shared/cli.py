"""Shared command-line entrypoint helpers."""

from __future__ import annotations

import csv
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from shared.reports import print_report_section


ArgsT = TypeVar("ArgsT")
SummaryT = TypeVar("SummaryT")

EXPECTED_CLI_ERRORS = (
    FileNotFoundError,
    NotADirectoryError,
    FileExistsError,
    OSError,
    UnicodeDecodeError,
    csv.Error,
    ValueError,
)


@dataclass(frozen=True)
class ConsoleSection:
    title: str
    lines: tuple[str, ...]


def print_cli_error(error: BaseException) -> None:
    print(f"Error: {error}", file=sys.stderr)


def console_section(title: str, lines: Iterable[str]) -> ConsoleSection:
    return ConsoleSection(title=title, lines=tuple(lines))


def files_section(lines: Iterable[str]) -> ConsoleSection:
    return console_section("Files", lines)


def processed_section(lines: Iterable[str]) -> ConsoleSection:
    return console_section("Processed", lines)


def print_console_sections(sections: Iterable[ConsoleSection]) -> None:
    for section in sections:
        print_report_section(section.title, section.lines)


def print_cli_summary(
    *,
    files: Iterable[str] = (),
    processed: Iterable[str] = (),
    extra_sections: Iterable[ConsoleSection] = (),
) -> None:
    sections: list[ConsoleSection] = []
    file_lines = tuple(files)
    processed_lines = tuple(processed)
    if file_lines:
        sections.append(files_section(file_lines))
    if processed_lines:
        sections.append(processed_section(processed_lines))
    sections.extend(extra_sections)
    print_console_sections(sections)


def print_step_header(label: str, step_index: int = 1, total_steps: int = 1) -> None:
    print_console_sections(
        [
            console_section(
                f"Step {step_index}/{total_steps}",
                [f"Running: {label}"],
            )
        ]
    )


def run_cli(
    parse_args: Callable[[Sequence[str] | None], ArgsT],
    run: Callable[[ArgsT], SummaryT],
    print_summary: Callable[[SummaryT], None],
    argv: Sequence[str] | None = None,
    success_exit_code: int | Callable[[SummaryT], int] = 0,
    expected_errors: tuple[type[BaseException], ...] = EXPECTED_CLI_ERRORS,
) -> int:
    try:
        args = parse_args(argv)
        summary = run(args)
    except expected_errors as error:
        print_cli_error(error)
        return 1
    print_summary(summary)
    if callable(success_exit_code):
        return success_exit_code(summary)
    return success_exit_code
