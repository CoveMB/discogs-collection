"""Small helpers for building CLI argument lists."""

from __future__ import annotations


def append_cli_option(arguments: list[str], option: str, value: object | None) -> None:
    if value is None:
        return
    arguments.extend([option, str(value)])
