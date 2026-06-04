"""Small report helpers shared by CLI scripts."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from pathlib import Path


DEFAULT_REPORT_DIRECTORY = Path("reports")


def readable_timestamp() -> str:
    return dt.datetime.now().replace(microsecond=0).strftime("%Y-%m-%d_%H-%M-%S")


def timestamped_report_path(
    output_path: Path,
    report_suffix: str,
    directory: Path = DEFAULT_REPORT_DIRECTORY,
) -> Path:
    return directory / f"{output_path.stem}_{readable_timestamp()}_{report_suffix}.txt"


def format_report_title(title: str) -> list[str]:
    return [title, "=" * len(title)]


def format_report_section(title: str, lines: Iterable[str]) -> list[str]:
    return ["", title, "-" * len(title), *lines]


def write_text_report(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
