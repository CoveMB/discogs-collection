"""Shared playlist map config helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from shared.files import write_json_file


DEFAULT_CONFIG_PATH = Path("config/playlist-map.json")


@dataclass(frozen=True)
class PlaylistConfig:
    playlist_prefix: str
    excluded_terms: tuple[str, ...]
    excluded_term_keys: frozenset[str]
    playlist_labels: tuple[str, ...]
    raw_aliases_by_label: Mapping[str, tuple[str, ...]]
    alias_keys_by_label: Mapping[str, tuple[str, ...]]


def blank_playlist_config_payload() -> dict[str, object]:
    return {
        "playlist_prefix": "Discogs - ",
        "excluded_terms": [],
        "playlists": {},
    }


def normalize_playlist_config(payload: object) -> PlaylistConfig:
    if not isinstance(payload, Mapping):
        raise ValueError("playlist config must be a JSON object")

    playlist_prefix = payload.get("playlist_prefix", "")
    if not isinstance(playlist_prefix, str):
        raise ValueError("playlist_prefix must be a string")

    excluded_terms = payload.get("excluded_terms", [])
    if not isinstance(excluded_terms, list) or not all(isinstance(term, str) for term in excluded_terms):
        raise ValueError("excluded_terms must be a list of strings")

    playlists = payload.get("playlists")
    if not isinstance(playlists, Mapping):
        raise ValueError("playlist config must contain a playlists object")

    clean_excluded_terms = tuple(term.strip() for term in excluded_terms if term.strip())
    excluded_term_keys = frozenset(normalize_term(term) for term in clean_excluded_terms)
    raw_terms_by_key: dict[str, tuple[str, str]] = {}
    playlist_labels: list[str] = []
    raw_aliases_by_label: dict[str, tuple[str, ...]] = {}
    alias_keys_by_label: dict[str, tuple[str, ...]] = {}

    for canonical_label, aliases in playlists.items():
        if not isinstance(canonical_label, str) or not canonical_label.strip():
            raise ValueError("playlist labels must be non-empty strings")
        if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
            raise ValueError(f"playlist aliases for {canonical_label} must be a list of strings")

        clean_label = canonical_label.strip()
        playlist_labels.append(clean_label)
        label_aliases: list[str] = []
        label_alias_keys: list[str] = []
        for alias in aliases:
            clean_alias = alias.strip()
            if not clean_alias:
                raise ValueError(f"playlist aliases for {clean_label} must be non-empty strings")
            alias_key = normalize_term(clean_alias)
            if alias_key in excluded_term_keys:
                raise ValueError(f"raw term appears in excluded_terms and playlists: {clean_alias}")

            previous = raw_terms_by_key.get(alias_key)
            if previous and previous[0] != clean_label:
                raise ValueError(f"raw term appears under multiple playlist labels: {clean_alias}")
            raw_terms_by_key[alias_key] = (clean_label, clean_alias)
            if alias_key not in label_alias_keys:
                label_aliases.append(clean_alias)
                label_alias_keys.append(alias_key)

        raw_aliases_by_label[clean_label] = tuple(label_aliases)
        alias_keys_by_label[clean_label] = tuple(label_alias_keys)

    return PlaylistConfig(
        playlist_prefix=playlist_prefix,
        excluded_terms=clean_excluded_terms,
        excluded_term_keys=excluded_term_keys,
        playlist_labels=tuple(playlist_labels),
        raw_aliases_by_label=raw_aliases_by_label,
        alias_keys_by_label=alias_keys_by_label,
    )


def normalize_term(term: str) -> str:
    return term.strip().casefold()


def load_playlist_config(path: Path) -> PlaylistConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"malformed playlist map JSON: {path}") from error
    return normalize_playlist_config(payload)


def ensure_playlist_config_file(path: Path) -> bool:
    if path.exists():
        return False
    write_json_file(path, blank_playlist_config_payload())
    return True


def format_playlist_config_overview(path: Path, config: PlaylistConfig, created: bool = False) -> str:
    lines = [
        f"Playlist config: {path}",
    ]
    if created:
        lines.append("Status: Created blank playlist config.")
    lines.extend(
        [
            "",
            "How playlist association works:",
            "1. Split Style and Genre into comma-separated Discogs terms.",
            "2. Trim whitespace and match terms case-insensitively.",
            "3. Ignore any term listed in excluded_terms.",
            "4. Check Style aliases first.",
            "5. If Style creates one or more playlists, keep those and skip Genre.",
            "6. Use Genre aliases only when Style creates no playlist.",
            "7. Keep playlist order from the playlists object in the config.",
            "",
            "Current playlist config:",
            f"Playlist prefix: {config.playlist_prefix or '(none)'}",
            "",
            "Excluded raw Discogs terms:",
        ]
    )
    if config.excluded_terms:
        lines.extend(f"- {term}" for term in config.excluded_terms)
    else:
        lines.append("- None configured.")

    lines.extend(["", "Playlist labels and raw Discogs terms:"])
    if config.playlist_labels:
        for playlist_label in config.playlist_labels:
            output_playlist_name = f"{config.playlist_prefix}{playlist_label}"
            raw_aliases = config.raw_aliases_by_label[playlist_label]
            raw_aliases_text = ", ".join(raw_aliases) if raw_aliases else "None"
            lines.append(f"- {playlist_label} -> {output_playlist_name}")
            lines.append(f"  Raw Discogs terms: {raw_aliases_text}")
    else:
        lines.append("No playlists configured yet.")

    lines.append("")
    return "\n".join(lines)
