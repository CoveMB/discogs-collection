"""Small report helpers shared by CLI scripts."""

from __future__ import annotations

import datetime as dt
import tempfile
from collections.abc import Iterable
from pathlib import Path


DEFAULT_REPORT_DIRECTORY = Path("reports")


def readable_timestamp() -> str:
    return dt.datetime.now().replace(microsecond=0).strftime("%Y-%m-%d_%H-%M-%S")


def script_report_path(
    script_path: Path | str,
    directory: Path = DEFAULT_REPORT_DIRECTORY,
) -> Path:
    script_name = Path(script_path).stem
    return directory / f"{readable_timestamp()}_{script_name}.txt"


def format_report_title(title: str) -> list[str]:
    return [title, "=" * len(title)]


def format_report_section(title: str, lines: Iterable[str]) -> list[str]:
    return ["", title, "-" * len(title), *lines]


def print_report_section(title: str, lines: Iterable[str]) -> None:
    for line in format_report_section(title, lines):
        print(line)


def write_text_report(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write("\n".join(lines))
        temporary_path.replace(path)
    except BaseException:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass  # Keep the original write/replace failure when cleanup also fails.
        raise
