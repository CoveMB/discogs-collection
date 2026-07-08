"""Shared TuneMyMusic playlist CSV schema helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


TUNEMYMUSIC_COLUMNS = (
    "Release Id",
    "Album Name",
    "Track Number",
    "Track Name",
    "Artist Name",
    "Spotify Search Query",
)


def missing_tunemymusic_columns(fieldnames: Sequence[str]) -> tuple[str, ...]:
    return tuple(column for column in TUNEMYMUSIC_COLUMNS if column not in fieldnames)


def normalize_tunemymusic_rows(rows: Sequence[Mapping[str, str]]) -> tuple[dict[str, str], ...]:
    return tuple(
        {column: str(row.get(column, "") or "") for column in TUNEMYMUSIC_COLUMNS}
        for row in rows
    )
