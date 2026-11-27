"""Read local manual override JSON without silently discarding duplicate members."""

from __future__ import annotations

import json
from pathlib import Path


def read_override_json(path: Path, *, label: str) -> object:
    def unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
        members: dict[str, object] = {}
        for name, value in pairs:
            if name in members:
                raise ValueError(f"{label} has duplicate JSON member {name!r}: {path}")
            members[name] = value
        return members

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_members)
    except json.JSONDecodeError as error:
        raise ValueError(f"malformed {label} JSON: {path}") from error
    except RecursionError as error:
        raise ValueError(f"malformed {label} JSON (nesting too deep): {path}") from error


def canonical_positive_decimal(value: str) -> bool:
    return value.isascii() and value.isdecimal() and not value.startswith("0")


def read_publisher_track_map(path: Path, publisher: str) -> tuple[dict[tuple[str, str], str], tuple[str, ...]]:
    payload = read_override_json(path, label="manual override map")
    if not isinstance(payload, dict) or set(payload) != {"publishers"}:
        raise ValueError("manual override map must contain only the 'publishers' object")
    publishers = payload["publishers"]
    if not isinstance(publishers, dict) or not isinstance(selected := publishers.get(publisher), dict):
        raise ValueError(f"manual override map must contain the {publisher!r} publisher object")

    selections: dict[tuple[str, str], str] = {}
    for release_id, release in selected.items():
        if not canonical_positive_decimal(release_id):
            raise ValueError(f"{publisher} release ID {release_id!r} must be positive ASCII decimal without leading zeros")
        if not isinstance(release, dict):
            raise ValueError(f"{publisher} release {release_id} must contain a track number object")
        for track_number, target in release.items():
            if not canonical_positive_decimal(track_number):
                raise ValueError(f"{publisher} release {release_id} track number {track_number!r} must be "
                                 "positive ASCII decimal without leading zeros")
            if not isinstance(target, str) or not target.strip():
                raise ValueError(f"{publisher} release {release_id}, track {track_number} must select a nonempty string")
            selections[release_id, track_number] = target
    return selections, tuple(name for name in publishers if name != publisher)
