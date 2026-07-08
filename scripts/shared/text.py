"""Small text normalization helpers shared by CLI scripts."""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def clean_cell(value: object) -> str:
    return str(value or "").strip()


def display_report_value(value: object) -> str:
    text = " ".join(str(value or "").split())
    return text if text else "(blank)"


def is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def join_non_empty(values: Iterable[str], separator: str = "; ") -> str:
    return separator.join(value for value in values if value)


def split_unique_comma_separated(value: object) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            term.strip()
            for term in str(value or "").split(",")
            if term.strip()
        )
    )


def sorted_stripped_unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


def unique_stripped_strings(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(value).strip()
            for value in values
            if str(value or "").strip()
        )
    )
