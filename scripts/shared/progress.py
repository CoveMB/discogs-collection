"""Terminal progress reporting helpers."""

from __future__ import annotations

import sys
from typing import TextIO


class ProgressReporter:
    def __init__(
        self,
        stream: TextIO | None = None,
        width: int = 24,
        label: str = "Processing rows",
        enabled: bool | None = None,
    ) -> None:
        self.stream = stream or sys.stderr
        self.width = max(1, width)
        self.label = label
        self.enabled = self.stream_is_terminal() if enabled is None else enabled
        self.current = 0
        self.total = 0
        self.started = False
        self.last_line_length = 0

        print("\n")

    def stream_is_terminal(self) -> bool:
        is_terminal = getattr(self.stream, "isatty", None)
        return bool(is_terminal and is_terminal())

    def start(self, total: int) -> None:
        self.total = max(0, total)
        self.current = 0
        self.started = True
        self.render()

    def update(self, current: int) -> None:
        if not self.started:
            return
        if self.total:
            self.current = min(max(0, current), self.total)
        else:
            self.current = 0
        self.render()

    def finish(self) -> None:
        if not self.started:
            return
        self.started = False
        if not self.enabled:
            return
        self.stream.write("\n")
        self.stream.flush()

    def render(self) -> None:
        if not self.enabled:
            return
        ratio = 1.0 if self.total == 0 else self.current / self.total
        filled_width = int(ratio * self.width)
        bar = "#" * filled_width + "-" * (self.width - filled_width)
        percentage = int(ratio * 100)
        line = f"\r{self.label} [{bar}] {self.current}/{self.total} {percentage}%"
        padding = " " * max(0, self.last_line_length - len(line))
        self.stream.write(line + padding)
        self.stream.flush()
        self.last_line_length = len(line)
