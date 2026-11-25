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
