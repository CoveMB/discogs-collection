"""Small report helpers shared by CLI scripts."""

from __future__ import annotations

import datetime as dt
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
    path.write_text("\n".join(lines), encoding="utf-8")
