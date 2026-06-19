"""Shared playlist master CSV selection helpers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path


def safe_playlist_filename(playlist_name: str) -> str:
    clean_name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", playlist_name).strip()
    clean_name = re.sub(r"\s+", " ", clean_name)
    return clean_name or "playlist"


def playlist_master_path(folder_path: Path) -> Path:
    return folder_path / f"{folder_path.name}.csv"


def playlist_selection_from_flag(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    selectors = tuple(str(value or "").strip() for value in values)
    blank_selectors = [selector for selector in selectors if not selector]
    if blank_selectors:
        raise ValueError("playlist selectors cannot be blank")
    return selectors


def resolve_playlist_master_paths(
    output_directory: Path,
    selectors: Sequence[str] | None = None,
    allow_all_selector: bool = False,
) -> tuple[Path, ...]:
    validate_output_directory(output_directory)
    selected_values = playlist_selection_from_flag(selectors)
    if not selected_values:
        return resolve_all_playlist_master_paths(output_directory)
    if any(selector.casefold() == "all" for selector in selected_values):
        if allow_all_selector and len(selected_values) == 1:
            return resolve_all_playlist_master_paths(output_directory)
        raise ValueError("playlist selector 'all' is not allowed; omit the playlist flag to process every playlist")

    master_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for selector in selected_values:
        master_path = resolve_one_playlist_master_path(output_directory, selector)
        resolved_master_path = master_path.resolve()
        if resolved_master_path in seen_paths:
            continue
        seen_paths.add(resolved_master_path)
        master_paths.append(master_path)
    return tuple(master_paths)


def validate_output_directory(output_directory: Path) -> None:
    if not output_directory.exists():
        raise FileNotFoundError(output_directory)
    if not output_directory.is_dir():
        raise NotADirectoryError(output_directory)


def resolve_all_playlist_master_paths(output_directory: Path) -> tuple[Path, ...]:
    master_paths: list[Path] = []
    for folder_path in sorted(output_directory.iterdir()):
        if folder_path.is_symlink():
            raise ValueError(f"{folder_path}: playlist folder symlinks are not supported")
        if not folder_path.is_dir():
            continue
        master_path = playlist_master_path(folder_path)
        if master_path.is_symlink():
            raise ValueError(f"{master_path}: playlist master CSV symlinks are not supported")
        if master_path.exists():
            master_paths.append(master_path)
    return tuple(master_paths)


def resolve_one_playlist_master_path(output_directory: Path, selector: str) -> Path:
    path_match = resolve_playlist_path_selector(output_directory, selector)
    if path_match:
        return path_match

    exact_folder_path = output_directory / selector
    exact_match = resolve_playlist_folder(output_directory, exact_folder_path, selector)
    if exact_match:
        return exact_match

    safe_folder_path = output_directory / safe_playlist_filename(selector)
    safe_match = resolve_playlist_folder(output_directory, safe_folder_path, selector)
    if safe_match:
        return safe_match

    raise FileNotFoundError(f"no playlist match found for selector: {selector}")


def resolve_playlist_path_selector(output_directory: Path, selector: str) -> Path | None:
    if not is_path_like_selector(selector):
        return None

    selector_path = Path(selector)
    candidate_paths = (selector_path,) if selector_path.is_absolute() else (Path.cwd() / selector_path, output_directory / selector_path)
    for candidate_path in candidate_paths:
        if not candidate_path.exists() and not candidate_path.is_symlink():
            continue
        if candidate_path.is_symlink():
            raise ValueError(f"{candidate_path}: playlist path symlinks are not supported")
        ensure_path_inside_output_directory(output_directory, candidate_path)
        if candidate_path.is_dir():
            return require_playlist_folder_master(output_directory, candidate_path, selector)
        if candidate_path.is_file():
            return require_playlist_master_path(output_directory, candidate_path, selector)
    return None


def is_path_like_selector(selector: str) -> bool:
    selector_path = Path(selector)
    return selector_path.is_absolute() or "/" in selector or "\\" in selector or selector_path.suffix.lower() == ".csv"


def resolve_playlist_folder(output_directory: Path, folder_path: Path, selector: str) -> Path | None:
    if not folder_path.exists() and not folder_path.is_symlink():
        return None
    return require_playlist_folder_master(output_directory, folder_path, selector)


def require_playlist_folder_master(output_directory: Path, folder_path: Path, selector: str) -> Path:
    if folder_path.is_symlink():
        raise ValueError(f"{folder_path}: playlist folder symlinks are not supported")
    ensure_path_inside_output_directory(output_directory, folder_path)
    if not folder_path.is_dir():
        raise NotADirectoryError(folder_path)
    master_path = playlist_master_path(folder_path)
    if master_path.is_symlink():
        raise ValueError(f"{master_path}: playlist master CSV symlinks are not supported")
    if not master_path.exists():
        raise FileNotFoundError(f"no playlist match found for selector: {selector}")
    return master_path


def require_playlist_master_path(output_directory: Path, master_path: Path, selector: str) -> Path:
    if master_path.is_symlink():
        raise ValueError(f"{master_path}: playlist master CSV symlinks are not supported")
    ensure_path_inside_output_directory(output_directory, master_path)
    expected_master_path = playlist_master_path(master_path.parent)
    if master_path.name != expected_master_path.name:
        raise FileNotFoundError(f"no playlist match found for selector: {selector}")
    return master_path


def ensure_path_inside_output_directory(output_directory: Path, path: Path) -> None:
    resolved_output_directory = output_directory.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_output_directory)
    except ValueError as error:
        raise ValueError(f"{path}: playlist path is outside playlist output directory {output_directory}") from error
