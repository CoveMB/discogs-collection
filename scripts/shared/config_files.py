"""Shared helpers for JSON-backed config files."""

from __future__ import annotations

import json
from collections.abc import Mapping, Set
from pathlib import Path

from shared.files import write_json_file


def load_json_file(path: Path, *, malformed_label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"malformed {malformed_label} JSON: {path}") from error


def load_or_create_json_file(
    path: Path,
    *,
    default_payload: Mapping[str, object],
    malformed_label: str,
) -> object:
    if not path.exists():
        write_json_file(path, dict(default_payload))
    return load_json_file(path, malformed_label=malformed_label)


def reject_unknown_keys(
    payload: Mapping[object, object],
    *,
    allowed_keys: Set[str],
    config_label: str,
) -> None:
    unknown_keys = sorted(str(key) for key in payload if key not in allowed_keys)
    if unknown_keys:
        raise ValueError(f"unknown {config_label} key: {', '.join(unknown_keys)}")
