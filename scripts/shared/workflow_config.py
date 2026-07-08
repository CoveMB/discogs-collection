"""Shared workflow config helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from shared.config_files import load_or_create_json_file, reject_unknown_keys


DEFAULT_WORKFLOW_CONFIG_PATH = Path("config/workflow.json")
DEFAULT_MAX_ROWS_PER_SPLIT = 500
DEFAULT_KEEP_RELEASE_TRACKS_TOGETHER = True
DEFAULT_CREATE_NEW_SPLIT_FILES_FOR_NEW_RELEASES = True
WORKFLOW_CONFIG_KEYS = frozenset(
    {
        "max_rows_per_split",
        "keep_release_tracks_together",
        "create_new_split_files_for_new_releases",
    }
)


@dataclass(frozen=True)
class WorkflowConfig:
    max_rows_per_split: int
    keep_release_tracks_together: bool
    create_new_split_files_for_new_releases: bool


def default_workflow_config_payload() -> dict[str, object]:
    return {
        "max_rows_per_split": DEFAULT_MAX_ROWS_PER_SPLIT,
        "keep_release_tracks_together": DEFAULT_KEEP_RELEASE_TRACKS_TOGETHER,
        "create_new_split_files_for_new_releases": DEFAULT_CREATE_NEW_SPLIT_FILES_FOR_NEW_RELEASES,
    }


def load_or_create_workflow_config(path: Path) -> WorkflowConfig:
    payload = load_or_create_json_file(
        path,
        default_payload=default_workflow_config_payload(),
        malformed_label="workflow config",
    )
    return normalize_workflow_config(payload)


def normalize_workflow_config(payload: object) -> WorkflowConfig:
    if not isinstance(payload, Mapping):
        raise ValueError("workflow config must be a JSON object")

    reject_unknown_keys(payload, allowed_keys=WORKFLOW_CONFIG_KEYS, config_label="workflow config")

    max_rows_per_split = payload.get("max_rows_per_split", DEFAULT_MAX_ROWS_PER_SPLIT)
    if isinstance(max_rows_per_split, bool) or not isinstance(max_rows_per_split, int):
        raise ValueError("max_rows_per_split must be an integer")
    if max_rows_per_split < 1:
        raise ValueError("max_rows_per_split must be at least 1")

    keep_release_tracks_together = payload.get(
        "keep_release_tracks_together",
        DEFAULT_KEEP_RELEASE_TRACKS_TOGETHER,
    )
    if not isinstance(keep_release_tracks_together, bool):
        raise ValueError("keep_release_tracks_together must be a boolean")

    create_new_split_files_for_new_releases = payload.get(
        "create_new_split_files_for_new_releases",
        DEFAULT_CREATE_NEW_SPLIT_FILES_FOR_NEW_RELEASES,
    )
    if not isinstance(create_new_split_files_for_new_releases, bool):
        raise ValueError("create_new_split_files_for_new_releases must be a boolean")

    return WorkflowConfig(
        max_rows_per_split=max_rows_per_split,
        keep_release_tracks_together=keep_release_tracks_together,
        create_new_split_files_for_new_releases=create_new_split_files_for_new_releases,
    )
