"""Shared command-line entrypoint helpers."""

from __future__ import annotations

import csv
import sys
from collections.abc import Callable, Sequence
from typing import TypeVar


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


def print_cli_error(error: BaseException) -> None:
    print(f"Error: {error}", file=sys.stderr)


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
