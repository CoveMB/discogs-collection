"""Shared Discogs collection CSV column names."""

from collections.abc import Sequence

STYLE_COLUMN = "Style"
GENRE_COLUMN = "Genre"
RELEASE_ID_COLUMN = "release_id"


def move_release_id_to_front(fieldnames: Sequence[str]) -> list[str]:
    ordered_fieldnames = list(fieldnames)
    if RELEASE_ID_COLUMN not in ordered_fieldnames:
        return ordered_fieldnames
    return [
        RELEASE_ID_COLUMN,
        *(fieldname for fieldname in ordered_fieldnames if fieldname != RELEASE_ID_COLUMN),
    ]
