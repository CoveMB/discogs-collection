"""Helpers for optional sanitized debug log files."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path


DebugLog = Callable[[str], None]


def build_debug_logger(path: Path | None) -> DebugLog | None:
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    def debug_log(message: str) -> None:
        timestamp = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{timestamp} {message}\n")

    return debug_log
