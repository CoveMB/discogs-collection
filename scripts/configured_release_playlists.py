"""Filesystem ownership preflight for configured release playlists."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import discogs_playlist_exporter as exporter
import discogs_playlist_splitter as splitter
from discogs_tracklists import ReleaseTracklistLookup
from shared.discogs_columns import RELEASE_ID_COLUMN
from shared.files import read_csv_file, write_csv_file
from shared.playlist_config import (
    ConfiguredReleasePlaylist,
    PlaylistConfig,
    is_strict_configured_release_playlist_folder_name,
)
from shared.playlist_selection import (
    ensure_path_inside_output_directory,
    playlist_master_path,
    safe_playlist_filename,
)
from shared.release_playlist_metadata import (
    CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
    RELEASE_PLAYLIST_METADATA_FILENAME,
    ReleasePlaylistMetadata,
    read_release_playlist_metadata,
    write_release_playlist_metadata,
)
from shared.reports import format_report_section, format_report_title, write_text_report
from shared.tunemymusic import TUNEMYMUSIC_COLUMNS, normalize_tunemymusic_rows
from shared.workflow_config import WorkflowConfig


@dataclass(frozen=True)
class ConfiguredReleasePlaylistTarget:
    definition: ConfiguredReleasePlaylist
    folder_path: Path
    master_path: Path
    metadata_path: Path


@dataclass(frozen=True)
class ConfiguredReleasePlaylistPreflight:
    targets: tuple[ConfiguredReleasePlaylistTarget, ...]
    stale_folder_observations: tuple[ConfiguredReleasePlaylistStaleFolderObservation, ...]
    ignored_folder_paths: tuple[Path, ...]

    @property
    def stale_folder_paths(self) -> tuple[Path, ...]:
        return tuple(observation.folder_path for observation in self.stale_folder_observations)


@dataclass(frozen=True)
class ConfiguredReleasePlaylistPathObservation:
    relative_path: Path
    device: int
    inode: int
    mode: int
    size: int
    modified_time_ns: int
    changed_time_ns: int


@dataclass(frozen=True)
class ConfiguredReleasePlaylistStaleFolderObservation:
    folder_path: Path
    metadata: ReleasePlaylistMetadata
    generated_paths: tuple[ConfiguredReleasePlaylistPathObservation, ...]


@dataclass(frozen=True)
class PreparedConfiguredReleasePlaylist:
    target: ConfiguredReleasePlaylistTarget
    staged_folder_path: Path
    staged_master_path: Path
    output_rows: tuple[dict[str, str], ...]
    split_summary: splitter.PlaylistSplitSummary
    release_change: exporter.PlaylistReleaseChange


@dataclass(frozen=True)
class PreparedConfiguredReleasePlaylistBatch:
    playlists: tuple[PreparedConfiguredReleasePlaylist, ...]


@dataclass(frozen=True)
class ConfiguredReleasePlaylistOutput:
    playlist_name: str
    release_ids: tuple[str, ...]
    folder_path: Path
    master_path: Path
    metadata_path: Path
    track_row_count: int
    split_summary: splitter.PlaylistSplitSummary
    release_change: exporter.PlaylistReleaseChange


@dataclass(frozen=True)
class ConfiguredReleasePlaylistsSummary:
    config_path: Path
    output_directory: Path
    report_path: Path
    split_report_path: Path
    playlists: tuple[ConfiguredReleasePlaylistOutput, ...]
    deleted_folder_paths: tuple[Path, ...]
    ignored_folder_paths: tuple[Path, ...]

    @property
    def master_paths(self) -> tuple[Path, ...]:
        return tuple(playlist.master_path for playlist in self.playlists)

    @property
    def playlist_names_by_master_path(self) -> dict[Path, str]:
        return {playlist.master_path: playlist.playlist_name for playlist in self.playlists}


class ConfiguredReleasePlaylistCommitError(OSError):
    def __init__(self, completed_paths: tuple[Path, ...], failed_path: Path, cause: Exception):
        super().__init__(f"configured release playlist commit failed at {failed_path}: {cause}")
        self.completed_paths = completed_paths
        self.failed_path = failed_path


class _ConfiguredSplitSyncError(OSError):
    def __init__(self, completed_paths: tuple[Path, ...], failed_path: Path, cause: Exception):
        super().__init__(str(cause))
        self.completed_paths = completed_paths
        self.failed_path = failed_path
        self.cause = cause


def preflight_configured_release_playlists(
    config: PlaylistConfig,
    output_directory: Path,
) -> ConfiguredReleasePlaylistPreflight:
    targets: list[ConfiguredReleasePlaylistTarget] = []
    target_paths_by_safe_name: dict[str, Path] = {}
    active_folder_paths: set[Path] = set()

    for definition in config.release_playlists:
        safe_folder_name = safe_playlist_filename(definition.name)
        folder_path = output_directory / safe_folder_name
        safe_name_key = folder_path.name.casefold()
        existing_path = target_paths_by_safe_name.get(safe_name_key)
        if existing_path is not None:
            raise ValueError(
                f"{existing_path} and {folder_path}: configured release playlists resolve to the same "
                "configured release playlist folder"
            )
        target_paths_by_safe_name[safe_name_key] = folder_path

        if folder_path.is_symlink():
            raise ValueError(f"{folder_path}: configured release playlist folder symlinks are not supported")
        ensure_path_inside_output_directory(output_directory, folder_path)
        if (
            not is_strict_configured_release_playlist_folder_name(safe_folder_name)
            or folder_path.resolve().parent != output_directory.resolve()
        ):
            raise ValueError(
                f"{folder_path}: configured release playlist target must be a strict direct child "
                f"of {output_directory}"
            )

        master_path = playlist_master_path(folder_path)
        metadata_path = folder_path / RELEASE_PLAYLIST_METADATA_FILENAME
        if master_path.is_symlink():
            raise ValueError(f"{master_path}: playlist master CSV symlinks are not supported")
        ensure_path_inside_output_directory(output_directory, master_path)
        if metadata_path.is_symlink():
            raise ValueError(f"{metadata_path}: release playlist metadata symlinks are not supported")
        ensure_path_inside_output_directory(output_directory, metadata_path)

        if folder_path.exists():
            if not folder_path.is_dir():
                raise ValueError(f"{folder_path}: configured release playlist target is not a directory")
            if not metadata_path.exists() and not metadata_path.is_symlink():
                raise ValueError(f"{folder_path}: has no configured release-playlist metadata")
            validate_active_generated_paths(
                output_directory=output_directory,
                folder_path=folder_path,
                master_path=master_path,
                metadata_path=metadata_path,
            )
            metadata = read_release_playlist_metadata(
                metadata_path,
                CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
            )
            _validate_metadata_name(metadata, definition.name, folder_path)

        target = ConfiguredReleasePlaylistTarget(
            definition=definition,
            folder_path=folder_path,
            master_path=master_path,
            metadata_path=metadata_path,
        )
        targets.append(target)
        active_folder_paths.add(folder_path)

    stale_folder_observations: list[ConfiguredReleasePlaylistStaleFolderObservation] = []
    ignored_folder_paths: list[Path] = []
    if output_directory.exists():
        if output_directory.is_symlink():
            raise ValueError(f"{output_directory}: configured release playlist output symlinks are not supported")
        if not output_directory.is_dir():
            raise ValueError(f"{output_directory}: configured release playlist output is not a directory")
        for folder_path in sorted(output_directory.iterdir()):
            if folder_path in active_folder_paths:
                continue
            if folder_path.is_symlink():
                if folder_path.is_dir():
                    raise ValueError(f"{folder_path}: configured release playlist folder symlinks are not supported")
                continue
            if not folder_path.is_dir():
                continue

            metadata_path = folder_path / RELEASE_PLAYLIST_METADATA_FILENAME
            if not metadata_path.exists() and not metadata_path.is_symlink():
                ignored_folder_paths.append(folder_path)
                continue
            metadata = read_release_playlist_metadata(
                metadata_path,
                CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
            )
            expected_folder_name = safe_playlist_filename(metadata.playlist_name)
            if expected_folder_name != folder_path.name:
                raise ValueError(
                    f"{folder_path}: metadata playlist name {metadata.playlist_name!r} does not match its folder"
                )
            validate_stale_folder_contents(folder_path)
            stale_folder_observations.append(
                observe_stale_folder(folder_path, metadata)
            )

    return ConfiguredReleasePlaylistPreflight(
        targets=tuple(targets),
        stale_folder_observations=tuple(stale_folder_observations),
        ignored_folder_paths=tuple(ignored_folder_paths),
    )


def build_strict_playlist_rows(
    definition: ConfiguredReleasePlaylist,
    lookup_tracklist: Callable[[Mapping[str, str]], ReleaseTracklistLookup],
) -> tuple[dict[str, str], ...]:
    records: list[exporter.ReleaseExportRecord] = []
    for release_id in definition.release_ids:
        row = {
            RELEASE_ID_COLUMN: release_id,
            "Artist": "",
            "Title": "",
            "Released": "",
        }
        lookup = lookup_tracklist(row)
        if not lookup.tracks or any(not track.title.strip() for track in lookup.tracks):
            note = next((note for note in lookup.notes if note.strip()), "no Discogs tracklist found")
            raise ValueError(f"Release ID {release_id}: no usable Discogs tracks: {note}")
        records.append(exporter.ReleaseExportRecord(row=row, lookup=lookup))

    output_rows, fallback_count = exporter.build_playlist_output_rows(records)
    if fallback_count:
        raise ValueError(f"{definition.name}: configured release playlists cannot contain fallback rows")
    return tuple(output_rows)


def prepare_configured_release_playlists(
    *,
    preflight: ConfiguredReleasePlaylistPreflight,
    staging_directory: Path,
    workflow_config: WorkflowConfig,
    lookup_tracklist: Callable[[Mapping[str, str]], ReleaseTracklistLookup],
) -> PreparedConfiguredReleasePlaylistBatch:
    prepared_playlists: list[PreparedConfiguredReleasePlaylist] = []
    staged_folder_paths = validate_staging_layout(preflight, staging_directory)
    for target, staged_folder_path in zip(preflight.targets, staged_folder_paths, strict=True):
        output_rows = build_strict_playlist_rows(target.definition, lookup_tracklist)
        staged_master_path = playlist_master_path(staged_folder_path)
        write_csv_file(staged_master_path, TUNEMYMUSIC_COLUMNS, output_rows)
        write_release_playlist_metadata(
            staged_folder_path / RELEASE_PLAYLIST_METADATA_FILENAME,
            ReleasePlaylistMetadata(
                schema_version=1,
                record_type=CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
                playlist_name=target.definition.name,
            ),
        )

        if output_rows:
            copy_existing_direct_child_splits(target, staged_folder_path)
            if (
                workflow_config.create_new_split_files_for_new_releases
                or split_seed_matches_master_prefix(staged_master_path)
            ):
                split_summary = splitter.write_stable_splits(
                    staged_master_path,
                    max_rows=workflow_config.max_rows_per_split,
                    keep_release_tracks_together=workflow_config.keep_release_tracks_together,
                    create_new_split_files_for_new_releases=(
                        workflow_config.create_new_split_files_for_new_releases
                    ),
                )
            else:
                split_summary = splitter.write_regenerated_splits(
                    staged_master_path,
                    max_rows=workflow_config.max_rows_per_split,
                    keep_release_tracks_together=workflow_config.keep_release_tracks_together,
                )
        else:
            split_summary = splitter.write_regenerated_splits(
                staged_master_path,
                max_rows=workflow_config.max_rows_per_split,
                keep_release_tracks_together=workflow_config.keep_release_tracks_together,
            )

        review_notes: list[str] = []
        previous_rows, previous_notes = exporter.read_existing_playlist_rows_for_report(
            target.master_path,
            review_notes,
        )
        prepared_playlists.append(
            PreparedConfiguredReleasePlaylist(
                target=target,
                staged_folder_path=staged_folder_path,
                staged_master_path=staged_master_path,
                output_rows=output_rows,
                split_summary=split_summary,
                release_change=exporter.build_playlist_release_change(
                    playlist_name=target.definition.name,
                    path=target.master_path,
                    previous_rows=previous_rows,
                    current_rows=output_rows,
                    notes=previous_notes,
                ),
            )
        )

    return PreparedConfiguredReleasePlaylistBatch(playlists=tuple(prepared_playlists))


def split_seed_matches_master_prefix(master_path: Path) -> bool:
    master_rows, _ = read_csv_file(master_path)
    normalized_master_rows = normalize_tunemymusic_rows(master_rows)
    try:
        existing_splits, warnings = splitter.read_existing_split_files(
            master_path.parent / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
        )
    except ValueError:
        return False
    if warnings:
        return False
    return all(
        split.end_row_number <= len(normalized_master_rows)
        and split.rows
        == normalized_master_rows[split.start_row_number - 1 : split.end_row_number]
        for split in existing_splits
    )


def sync_generated_split_files(
    staged_splits_directory: Path,
    final_splits_directory: Path,
) -> tuple[Path, ...]:
    written_paths, _ = _sync_generated_split_files(staged_splits_directory, final_splits_directory)
    return written_paths


def _sync_generated_split_files(
    staged_splits_directory: Path,
    final_splits_directory: Path,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    completed_paths: list[Path] = []
    try:
        staged_paths = (
            tuple(sorted(staged_splits_directory.glob("*.csv")))
            if staged_splits_directory.exists()
            else ()
        )
        final_splits_directory_exists = final_splits_directory.exists()
    except Exception as error:
        raise _ConfiguredSplitSyncError((), final_splits_directory, error) from error
    staged_names = {path.name for path in staged_paths}

    if not staged_paths and not final_splits_directory_exists:
        return (), ()

    try:
        if final_splits_directory.is_symlink():
            raise ValueError(f"{final_splits_directory}: splits directory symlinks are not supported")
        if final_splits_directory.exists() and not final_splits_directory.is_dir():
            raise ValueError(f"{final_splits_directory}: splits path is not a directory")
        final_splits_directory.mkdir(parents=True, exist_ok=True)
    except Exception as error:
        raise _ConfiguredSplitSyncError((), final_splits_directory, error) from error

    written_paths: list[Path] = []
    for staged_path in staged_paths:
        final_path = final_splits_directory / staged_path.name
        try:
            rows, fieldnames = read_csv_file(staged_path)
            write_csv_file(final_path, fieldnames, rows)
        except Exception as error:
            raise _ConfiguredSplitSyncError(tuple(completed_paths), final_path, error) from error
        completed_paths.append(final_path)
        written_paths.append(final_path)

    deleted_paths: list[Path] = []
    try:
        existing_paths = tuple(sorted(final_splits_directory.glob("*.csv")))
    except Exception as error:
        raise _ConfiguredSplitSyncError(tuple(completed_paths), final_splits_directory, error) from error
    for existing_path in existing_paths:
        if existing_path.name in staged_names:
            continue
        try:
            if existing_path.is_symlink():
                raise ValueError(f"{existing_path}: split CSV symlinks are not supported")
            if not existing_path.is_file():
                raise ValueError(f"{existing_path}: split CSV path is not a file")
            existing_path.unlink()
        except Exception as error:
            raise _ConfiguredSplitSyncError(tuple(completed_paths), existing_path, error) from error
        completed_paths.append(existing_path)
        deleted_paths.append(existing_path)

    return tuple(written_paths), tuple(deleted_paths)


def commit_configured_release_playlists(
    *,
    config_path: Path,
    output_directory: Path,
    report_path: Path,
    split_report_path: Path,
    preflight: ConfiguredReleasePlaylistPreflight,
    prepared: PreparedConfiguredReleasePlaylistBatch,
) -> ConfiguredReleasePlaylistsSummary:
    completed_paths: list[Path] = []
    outputs: list[ConfiguredReleasePlaylistOutput] = []

    for prepared_playlist in prepared.playlists:
        target = prepared_playlist.target
        try:
            master_rows, master_fieldnames = read_csv_file(prepared_playlist.staged_master_path)
            write_csv_file(target.master_path, master_fieldnames, master_rows)
        except Exception as error:
            raise ConfiguredReleasePlaylistCommitError(tuple(completed_paths), target.master_path, error) from error
        completed_paths.append(target.master_path)

        staged_metadata_path = prepared_playlist.staged_folder_path / RELEASE_PLAYLIST_METADATA_FILENAME
        try:
            metadata = read_release_playlist_metadata(
                staged_metadata_path,
                CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
            )
            write_release_playlist_metadata(target.metadata_path, metadata)
        except Exception as error:
            raise ConfiguredReleasePlaylistCommitError(tuple(completed_paths), target.metadata_path, error) from error
        completed_paths.append(target.metadata_path)

        staged_splits_directory = prepared_playlist.staged_folder_path / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
        final_splits_directory = target.folder_path / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
        try:
            written_split_paths, deleted_split_paths = _sync_generated_split_files(
                staged_splits_directory,
                final_splits_directory,
            )
        except _ConfiguredSplitSyncError as error:
            raise ConfiguredReleasePlaylistCommitError(
                tuple(completed_paths) + error.completed_paths,
                error.failed_path,
                error.cause,
            ) from error.cause
        completed_paths.extend(written_split_paths)
        completed_paths.extend(deleted_split_paths)

        outputs.append(
            ConfiguredReleasePlaylistOutput(
                playlist_name=target.definition.name,
                release_ids=target.definition.release_ids,
                folder_path=target.folder_path,
                master_path=target.master_path,
                metadata_path=target.metadata_path,
                track_row_count=len(prepared_playlist.output_rows),
                split_summary=_remap_split_summary(
                    prepared_playlist.split_summary,
                    prepared_playlist.staged_folder_path,
                    target.folder_path,
                ),
                release_change=prepared_playlist.release_change,
            )
        )

    deleted_folder_paths: list[Path] = []
    for observation in preflight.stale_folder_observations:
        stale_folder_path = observation.folder_path
        try:
            revalidate_stale_folder(observation)
            shutil.rmtree(stale_folder_path)
        except Exception as error:
            raise ConfiguredReleasePlaylistCommitError(
                tuple(completed_paths),
                stale_folder_path,
                error,
            ) from error
        completed_paths.append(stale_folder_path)
        deleted_folder_paths.append(stale_folder_path)

    return ConfiguredReleasePlaylistsSummary(
        config_path=config_path,
        output_directory=output_directory,
        report_path=report_path,
        split_report_path=split_report_path,
        playlists=tuple(outputs),
        deleted_folder_paths=tuple(deleted_folder_paths),
        ignored_folder_paths=preflight.ignored_folder_paths,
    )


def _remap_split_summary(
    summary: splitter.PlaylistSplitSummary,
    staged_folder_path: Path,
    final_folder_path: Path,
) -> splitter.PlaylistSplitSummary:
    def remap_path(path: Path) -> Path:
        try:
            relative_path = path.relative_to(staged_folder_path)
        except ValueError:
            return path
        return final_folder_path / relative_path

    staged_folder_text = str(staged_folder_path)
    final_folder_text = str(final_folder_path)
    return replace(
        summary,
        playlist_folder_path=final_folder_path,
        master_path=remap_path(summary.master_path),
        written_split_paths=tuple(remap_path(path) for path in summary.written_split_paths),
        regenerated_split_paths=tuple(remap_path(path) for path in summary.regenerated_split_paths),
        preserved_split_paths=tuple(remap_path(path) for path in summary.preserved_split_paths),
        updated_split_paths=tuple(remap_path(path) for path in summary.updated_split_paths),
        warnings=tuple(warning.replace(staged_folder_text, final_folder_text) for warning in summary.warnings),
    )


def create_configured_release_playlists(
    *,
    config: PlaylistConfig,
    config_path: Path,
    workflow_config: WorkflowConfig,
    output_directory: Path,
    report_path: Path,
    split_report_path: Path,
    lookup_tracklist: Callable[[Mapping[str, str]], ReleaseTracklistLookup],
) -> ConfiguredReleasePlaylistsSummary:
    try:
        preflight = preflight_configured_release_playlists(config, output_directory)
        output_directory.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".configured-release-playlists-",
            dir=output_directory.parent,
        ) as temporary_directory:
            prepared = prepare_configured_release_playlists(
                preflight=preflight,
                staging_directory=Path(temporary_directory),
                workflow_config=workflow_config,
                lookup_tracklist=lookup_tracklist,
            )
            summary = commit_configured_release_playlists(
                config_path=config_path,
                output_directory=output_directory,
                report_path=report_path,
                split_report_path=split_report_path,
                preflight=preflight,
                prepared=prepared,
            )
    except Exception as error:
        try:
            write_configured_release_playlist_failure_report(
                report_path,
                config_path=config_path,
                output_directory=output_directory,
                error=error,
            )
        except Exception:
            pass
        raise

    write_configured_release_playlist_report(summary)
    write_configured_split_report(summary)
    return summary


def write_configured_release_playlist_report(summary: ConfiguredReleasePlaylistsSummary) -> None:
    lines = format_report_title("Configured release playlist generation report")
    lines.extend(
        format_report_section(
            "Summary",
            [
                f"- Config: {summary.config_path}",
                f"- Output directory: {summary.output_directory}",
                f"- Current playlists: {len(summary.playlists)}",
                f"- Track rows: {sum(playlist.track_row_count for playlist in summary.playlists)}",
                f"- Empty playlists: {sum(not playlist.release_ids for playlist in summary.playlists)}",
                f"- Deleted folders: {len(summary.deleted_folder_paths)}",
                f"- Ignored folders: {len(summary.ignored_folder_paths)}",
                "- Collection master: not read or written",
            ],
        )
    )

    playlist_lines: list[str] = []
    for playlist in summary.playlists:
        playlist_lines.extend(
            [
                f"- {playlist.playlist_name}:",
                f"  Folder: {playlist.folder_path}",
                f"  Master CSV: {playlist.master_path}",
                f"  Metadata: {playlist.metadata_path}",
                f"  Track rows: {playlist.track_row_count}",
                f"  Split CSVs: {_count_direct_child_split_csvs(playlist.folder_path)}",
                "  Release IDs:",
            ]
        )
        if playlist.release_ids:
            playlist_lines.extend(f"    - {release_id}" for release_id in playlist.release_ids)
        else:
            playlist_lines.append("    - None (authoritative empty playlist)")
    lines.extend(format_report_section("Current playlists", playlist_lines or ["- None"]))
    lines.extend(
        format_report_section(
            "Release changes",
            exporter.format_playlist_release_change_lines(
                tuple(playlist.release_change for playlist in summary.playlists)
            ),
        )
    )
    lines.extend(
        format_report_section(
            "Deleted configured folders",
            [f"- {path}" for path in summary.deleted_folder_paths] or ["- None"],
        )
    )
    lines.extend(
        format_report_section(
            "Ignored unknown folders",
            [f"- {path}" for path in summary.ignored_folder_paths] or ["- None"],
        )
    )
    write_text_report(summary.report_path, lines)


def _count_direct_child_split_csvs(folder_path: Path) -> int:
    splits_directory = folder_path / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
    if not splits_directory.exists() or not splits_directory.is_dir():
        return 0
    return sum(path.is_file() for path in splits_directory.glob("*.csv"))


def write_configured_split_report(summary: ConfiguredReleasePlaylistsSummary) -> None:
    splitter.write_report(
        summary.split_report_path,
        summary.output_directory,
        "configured release playlists",
        tuple(playlist.split_summary for playlist in summary.playlists),
    )


def write_configured_release_playlist_failure_report(
    path: Path,
    *,
    config_path: Path,
    output_directory: Path,
    error: Exception,
) -> None:
    completed_paths = error.completed_paths if isinstance(error, ConfiguredReleasePlaylistCommitError) else ()
    failed_path = error.failed_path if isinstance(error, ConfiguredReleasePlaylistCommitError) else None
    lines = format_report_title("Configured release playlist generation failure report")
    lines.extend(
        format_report_section(
            "Failure",
            [
                f"- Config: {config_path}",
                f"- Output directory: {output_directory}",
                f"- Error: {type(error).__name__}: {error}",
                f"- Failed path: {failed_path if failed_path is not None else 'None'}",
                "- Collection master: not read or written",
            ],
        )
    )
    lines.extend(
        format_report_section(
            "Completed paths",
            [f"- {completed_path}" for completed_path in completed_paths] or ["- None"],
        )
    )
    write_text_report(path, lines)


def validate_staging_layout(
    preflight: ConfiguredReleasePlaylistPreflight,
    staging_directory: Path,
) -> tuple[Path, ...]:
    if staging_directory.is_symlink():
        raise ValueError(f"{staging_directory}: staging directory symlinks are not supported")
    if staging_directory.exists() and not staging_directory.is_dir():
        raise ValueError(f"{staging_directory}: staging path is not a directory")

    resolved_staging_directory = staging_directory.resolve()
    final_output_directories = {target.folder_path.parent.resolve() for target in preflight.targets}
    for final_output_directory in final_output_directories:
        if paths_overlap(resolved_staging_directory, final_output_directory):
            raise ValueError(
                f"{staging_directory}: staging directory overlaps configured release playlist output "
                f"{final_output_directory}"
            )

    staged_folder_paths: list[Path] = []
    for target in preflight.targets:
        staged_folder_path = staging_directory / target.folder_path.name
        staged_master_path = playlist_master_path(staged_folder_path)
        staged_metadata_path = staged_folder_path / RELEASE_PLAYLIST_METADATA_FILENAME
        staged_splits_directory = staged_folder_path / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME

        if staged_folder_path.is_symlink():
            raise ValueError(f"{staged_folder_path}: staged playlist folder symlinks are not supported")
        ensure_path_inside_output_directory(staging_directory, staged_folder_path)
        if staged_folder_path.exists() and not staged_folder_path.is_dir():
            raise ValueError(f"{staged_folder_path}: staged playlist target is not a directory")

        if staged_master_path.is_symlink():
            raise ValueError(f"{staged_master_path}: staged playlist master CSV symlinks are not supported")
        ensure_path_inside_output_directory(staging_directory, staged_master_path)
        if staged_master_path.exists() and not staged_master_path.is_file():
            raise ValueError(f"{staged_master_path}: staged playlist master CSV path is not a file")

        if staged_metadata_path.is_symlink():
            raise ValueError(f"{staged_metadata_path}: staged release playlist metadata symlinks are not supported")
        ensure_path_inside_output_directory(staging_directory, staged_metadata_path)
        if staged_metadata_path.exists() and not staged_metadata_path.is_file():
            raise ValueError(f"{staged_metadata_path}: staged release playlist metadata path is not a file")

        if staged_splits_directory.is_symlink():
            raise ValueError(f"{staged_splits_directory}: staged splits directory symlinks are not supported")
        ensure_path_inside_output_directory(staging_directory, staged_splits_directory)
        if staged_splits_directory.exists() and not staged_splits_directory.is_dir():
            raise ValueError(f"{staged_splits_directory}: staged splits path is not a directory")
        if staged_splits_directory.exists():
            for staged_split_path in staged_splits_directory.iterdir():
                if staged_split_path.suffix.lower() != ".csv":
                    continue
                if staged_split_path.is_symlink():
                    raise ValueError(f"{staged_split_path}: staged split CSV symlinks are not supported")
                if not staged_split_path.is_file():
                    raise ValueError(f"{staged_split_path}: staged split CSV path is not a file")

        staged_folder_paths.append(staged_folder_path)

    return tuple(staged_folder_paths)


def paths_overlap(first_path: Path, second_path: Path) -> bool:
    return first_path == second_path or first_path in second_path.parents or second_path in first_path.parents


def copy_existing_direct_child_splits(
    target: ConfiguredReleasePlaylistTarget,
    staged_folder_path: Path,
) -> None:
    staged_splits_directory = staged_folder_path / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
    remove_staged_direct_child_split_csvs(staged_splits_directory)
    source_splits_directory = target.folder_path / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
    if not source_splits_directory.exists():
        return
    if source_splits_directory.is_symlink():
        raise ValueError(f"{source_splits_directory}: splits directory symlinks are not supported")
    if not source_splits_directory.is_dir():
        raise ValueError(f"{source_splits_directory}: splits path is not a directory")

    for source_path in sorted(source_splits_directory.iterdir()):
        if source_path.suffix.lower() != ".csv" or not source_path.is_file():
            continue
        if source_path.is_symlink():
            raise ValueError(f"{source_path}: split CSV symlinks are not supported")
        staged_splits_directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, staged_splits_directory / source_path.name)


def remove_staged_direct_child_split_csvs(staged_splits_directory: Path) -> None:
    if not staged_splits_directory.exists():
        return
    if staged_splits_directory.is_symlink():
        raise ValueError(f"{staged_splits_directory}: staged splits directory symlinks are not supported")
    if not staged_splits_directory.is_dir():
        raise ValueError(f"{staged_splits_directory}: staged splits path is not a directory")
    for staged_path in staged_splits_directory.iterdir():
        if staged_path.suffix.lower() != ".csv":
            continue
        if staged_path.is_symlink():
            raise ValueError(f"{staged_path}: staged split CSV symlinks are not supported")
        if staged_path.is_file():
            staged_path.unlink()


def _validate_metadata_name(
    metadata: ReleasePlaylistMetadata,
    expected_playlist_name: str,
    folder_path: Path,
) -> None:
    if metadata.playlist_name != expected_playlist_name:
        raise ValueError(
            f"{folder_path}: metadata playlist name {metadata.playlist_name!r} does not match "
            f"configured name {expected_playlist_name!r}"
        )


def validate_active_generated_paths(
    *,
    output_directory: Path,
    folder_path: Path,
    master_path: Path,
    metadata_path: Path,
) -> None:
    if master_path.exists() and not master_path.is_file():
        raise ValueError(f"{master_path}: playlist master CSV path is not a file")
    if metadata_path.exists() and not metadata_path.is_file():
        raise ValueError(f"{metadata_path}: release playlist metadata path is not a file")

    splits_directory = folder_path / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
    if splits_directory.is_symlink():
        raise ValueError(f"{splits_directory}: splits directory symlinks are not supported")
    ensure_path_inside_output_directory(output_directory, splits_directory)
    if splits_directory.exists() and not splits_directory.is_dir():
        raise ValueError(f"{splits_directory}: splits path is not a directory")
    if not splits_directory.exists():
        return

    for split_path in splits_directory.iterdir():
        if split_path.suffix.lower() != ".csv":
            continue
        if split_path.is_symlink():
            raise ValueError(f"{split_path}: split CSV symlinks are not supported")
        ensure_path_inside_output_directory(output_directory, split_path)
        if not split_path.is_file():
            raise ValueError(f"{split_path}: split CSV path is not a file")


def validate_stale_folder_contents(folder_path: Path) -> None:
    master_path = playlist_master_path(folder_path)
    allowed_root_names = {
        RELEASE_PLAYLIST_METADATA_FILENAME,
        master_path.name,
        "splits",
    }
    unexpected_root_entries = [path for path in folder_path.iterdir() if path.name not in allowed_root_names]
    if unexpected_root_entries:
        raise ValueError(f"{folder_path}: unexpected content blocks configured playlist cleanup")

    if master_path.is_symlink():
        raise ValueError(f"{master_path}: playlist master CSV symlinks are not supported")
    if master_path.exists() and not master_path.is_file():
        raise ValueError(f"{master_path}: playlist master CSV path is not a file")

    splits_directory = folder_path / "splits"
    if splits_directory.is_symlink():
        raise ValueError(f"{splits_directory}: splits directory symlinks are not supported")
    if splits_directory.exists() and not splits_directory.is_dir():
        raise ValueError(f"{splits_directory}: splits path is not a directory")
    if splits_directory.exists():
        unexpected_split_entries = [
            path
            for path in splits_directory.iterdir()
            if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".csv"
        ]
        if unexpected_split_entries:
            raise ValueError(f"{splits_directory}: unexpected content blocks configured playlist cleanup")


def observe_stale_folder(
    folder_path: Path,
    metadata: ReleasePlaylistMetadata,
) -> ConfiguredReleasePlaylistStaleFolderObservation:
    master_path = playlist_master_path(folder_path)
    metadata_path = folder_path / RELEASE_PLAYLIST_METADATA_FILENAME
    splits_directory = folder_path / splitter.PLAYLIST_SPLITS_DIRECTORY_NAME
    generated_paths = [folder_path, metadata_path]
    if master_path.exists():
        generated_paths.append(master_path)
    if splits_directory.exists():
        generated_paths.append(splits_directory)
        generated_paths.extend(sorted(splits_directory.iterdir()))

    path_observations: list[ConfiguredReleasePlaylistPathObservation] = []
    for path in generated_paths:
        path_stat = path.lstat()
        path_observations.append(
            ConfiguredReleasePlaylistPathObservation(
                relative_path=path.relative_to(folder_path),
                device=path_stat.st_dev,
                inode=path_stat.st_ino,
                mode=path_stat.st_mode,
                size=path_stat.st_size,
                modified_time_ns=path_stat.st_mtime_ns,
                changed_time_ns=path_stat.st_ctime_ns,
            )
        )
    return ConfiguredReleasePlaylistStaleFolderObservation(
        folder_path=folder_path,
        metadata=metadata,
        generated_paths=tuple(path_observations),
    )


def revalidate_stale_folder(
    observation: ConfiguredReleasePlaylistStaleFolderObservation,
) -> None:
    folder_path = observation.folder_path
    if folder_path.is_symlink():
        raise ValueError(f"{folder_path}: configured release playlist folder symlinks are not supported")
    if not folder_path.exists() or not folder_path.is_dir():
        raise ValueError(f"{folder_path}: configured release playlist cleanup target is no longer a directory")

    metadata_path = folder_path / RELEASE_PLAYLIST_METADATA_FILENAME
    metadata = read_release_playlist_metadata(
        metadata_path,
        CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
    )
    if metadata != observation.metadata:
        raise ValueError(f"{folder_path}: configured release playlist ownership changed after preflight")
    expected_folder_name = safe_playlist_filename(metadata.playlist_name)
    if expected_folder_name != folder_path.name:
        raise ValueError(
            f"{folder_path}: metadata playlist name {metadata.playlist_name!r} does not match its folder"
        )

    validate_stale_folder_contents(folder_path)
    current_observation = observe_stale_folder(folder_path, metadata)
    if current_observation.generated_paths != observation.generated_paths:
        raise ValueError(f"{folder_path}: configured release playlist contents changed after preflight")
