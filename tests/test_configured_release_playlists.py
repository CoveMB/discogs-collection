import sys
import tempfile
import unittest
import csv
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import configured_release_playlists as configured  # noqa: E402
from configured_release_playlists import (  # noqa: E402
    build_strict_playlist_rows,
    preflight_configured_release_playlists,
    prepare_configured_release_playlists,
)
from discogs_tracklists import DiscogsTrack, ReleaseTracklistLookup  # noqa: E402
from shared.playlist_config import ConfiguredReleasePlaylist, normalize_playlist_config  # noqa: E402
from shared.release_playlist_metadata import (  # noqa: E402
    AD_HOC_RELEASE_PLAYLIST_RECORD_TYPE,
    CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
    RELEASE_PLAYLIST_METADATA_FILENAME,
    ReleasePlaylistMetadata,
    write_release_playlist_metadata,
)
from shared.tunemymusic import TUNEMYMUSIC_COLUMNS  # noqa: E402
from shared.workflow_config import WorkflowConfig  # noqa: E402
from shared.files import write_csv_file  # noqa: E402


def playlist_config(release_playlists: dict[str, list[str]]):
    return normalize_playlist_config(
        {
            "excluded_terms": [],
            "playlists": {},
            "release_playlists": release_playlists,
        }
    )


def write_configured_metadata(folder_path: Path, playlist_name: str) -> Path:
    metadata_path = folder_path / RELEASE_PLAYLIST_METADATA_FILENAME
    write_release_playlist_metadata(
        metadata_path,
        ReleasePlaylistMetadata(
            schema_version=1,
            record_type=CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
            playlist_name=playlist_name,
        ),
    )
    return metadata_path


def configured_preflight_fixture(
    output_directory: Path,
    release_playlists: dict[str, list[str]],
):
    return preflight_configured_release_playlists(playlist_config(release_playlists), output_directory)


def lookup_fixture(release_id: str, tracks: tuple[DiscogsTrack, ...]) -> ReleaseTracklistLookup:
    return ReleaseTracklistLookup(
        release_id=release_id,
        artist_name=f"Artist {release_id}",
        album_name=f"Album {release_id}",
        record_year="2026",
        tracks=tracks,
        notes=(),
    )


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as input_file:
        return list(csv.DictReader(input_file))


def write_split_csv(path: Path, rows: list[dict[str, str]]) -> None:
    write_csv_file(path, TUNEMYMUSIC_COLUMNS, rows)


def write_existing_configured_playlist(output_directory: Path, playlist_name: str) -> tuple[Path, Path, str, str]:
    folder_path = output_directory / playlist_name
    write_configured_metadata(folder_path, playlist_name)
    master_path = folder_path / f"{playlist_name}.csv"
    master_text = (
        "Release Id,Album Name,Track Number,Track Name,Artist Name,Spotify Search Query\n"
        "111,Old,1,Old Track,Artist,Old Track\n"
    )
    master_path.write_text(master_text, encoding="utf-8")
    split_path = folder_path / "splits" / "1-1.csv"
    write_split_csv(
        split_path,
        [
            {
                "Release Id": "111",
                "Album Name": "Old",
                "Track Number": "1",
                "Track Name": "Old Track",
                "Artist Name": "Artist",
                "Spotify Search Query": "Old Track",
            }
        ],
    )
    return master_path, split_path, master_text, split_path.read_text(encoding="utf-8")


def create_configured_batch(
    *,
    config,
    directory: Path,
    lookup_tracklist,
):
    create = getattr(configured, "create_configured_release_playlists", None)
    if create is None:
        raise AssertionError("create_configured_release_playlists is not implemented")
    return create(
        config=config,
        config_path=directory / "playlist-map.json",
        workflow_config=WorkflowConfig(500, True, True),
        output_directory=directory / "release-playlists",
        report_path=directory / "configured-report.txt",
        split_report_path=directory / "configured-split-report.txt",
        lookup_tracklist=lookup_tracklist,
    )


class ConfiguredReleasePlaylistCommitTests(unittest.TestCase):
    def test_invalid_active_generated_paths_fail_before_lookup_or_generated_write(self):
        cases = (
            (
                "master directory",
                ["111"],
                "master_directory",
                "playlist master CSV path is not a file",
            ),
            (
                "symlinked splits directory",
                ["111"],
                "splits_symlink",
                "splits directory symlinks are not supported",
            ),
            (
                "non-directory splits path",
                ["111"],
                "splits_file",
                "splits path is not a directory",
            ),
            (
                "symlinked split CSV for empty definition",
                [],
                "split_csv_symlink",
                "split CSV symlinks are not supported",
            ),
            (
                "non-file split CSV",
                ["111"],
                "split_csv_directory",
                "split CSV path is not a file",
            ),
        )

        for label, release_ids, invalid_shape, expected_error in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                output_directory = directory / "release-playlists"
                folder_path = output_directory / "Friday Picks"
                metadata_path = write_configured_metadata(folder_path, "Friday Picks")
                metadata_text = metadata_path.read_text(encoding="utf-8")
                master_path = folder_path / "Friday Picks.csv"
                splits_directory = folder_path / "splits"
                outside_path = directory / "outside"

                if invalid_shape == "master_directory":
                    master_path.mkdir()
                elif invalid_shape == "splits_symlink":
                    outside_path.mkdir()
                    splits_directory.symlink_to(outside_path, target_is_directory=True)
                elif invalid_shape == "splits_file":
                    splits_directory.write_text("keep", encoding="utf-8")
                else:
                    splits_directory.mkdir()
                    split_path = splits_directory / "1-1.csv"
                    if invalid_shape == "split_csv_symlink":
                        outside_path.write_text("keep", encoding="utf-8")
                        split_path.symlink_to(outside_path)
                    else:
                        split_path.mkdir()

                lookup_count = 0

                def unexpected_lookup(_row: Mapping[str, str]) -> ReleaseTracklistLookup:
                    nonlocal lookup_count
                    lookup_count += 1
                    return lookup_fixture(
                        "111",
                        (DiscogsTrack(position="1", title="New", artist_name="Artist 111"),),
                    )

                with self.assertRaisesRegex(
                    (ValueError, configured.ConfiguredReleasePlaylistCommitError),
                    expected_error,
                ):
                    create_configured_batch(
                        config=playlist_config({"Friday Picks": release_ids}),
                        directory=directory,
                        lookup_tracklist=unexpected_lookup,
                    )

                self.assertEqual(lookup_count, 0)
                self.assertEqual(metadata_path.read_text(encoding="utf-8"), metadata_text)
                if invalid_shape == "master_directory":
                    self.assertTrue(master_path.is_dir())
                else:
                    self.assertFalse(master_path.exists())
                if invalid_shape.endswith("symlink"):
                    self.assertTrue(outside_path.exists())

    def test_update_replaces_generated_csvs_and_preserves_active_notes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            master_path, old_split_path, _, _ = write_existing_configured_playlist(
                output_directory,
                "Friday Picks",
            )
            notes_path = master_path.parent / "notes.txt"
            notes_path.write_text("keep these notes", encoding="utf-8")

            summary = create_configured_batch(
                config=playlist_config({"Friday Picks": ["111", "222"]}),
                directory=directory,
                lookup_tracklist=lambda row: lookup_fixture(
                    row["release_id"],
                    (DiscogsTrack(position="1", title=f"Track {row['release_id']}", artist_name="Artist"),),
                ),
            )

            self.assertEqual([row["Release Id"] for row in read_csv_rows(master_path)], ["111", "222"])
            self.assertEqual(notes_path.read_text(encoding="utf-8"), "keep these notes")
            self.assertEqual(
                sorted(path.name for path in (master_path.parent / "splits").glob("*.csv")),
                ["1-1.csv", "2-2.csv"],
            )
            self.assertEqual([row["Release Id"] for row in read_csv_rows(old_split_path)], ["111"])
            self.assertEqual(summary.master_paths, (master_path,))
            self.assertEqual(summary.playlist_names_by_master_path, {master_path: "Friday Picks"})
            self.assertIn("Split CSVs: 2", summary.report_path.read_text(encoding="utf-8"))

    def test_removed_definition_deletes_clean_metadata_owned_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            master_path, _, _, _ = write_existing_configured_playlist(output_directory, "Old Picks")
            stale_folder_path = master_path.parent

            summary = create_configured_batch(
                config=playlist_config({}),
                directory=directory,
                lookup_tracklist=lambda _row: self.fail("removed definitions must not be looked up"),
            )

            self.assertFalse(stale_folder_path.exists())
            self.assertEqual(summary.deleted_folder_paths, (stale_folder_path,))
            self.assertEqual(summary.master_paths, ())
            self.assertEqual(summary.playlist_names_by_master_path, {})

    def test_cleanup_preserves_stale_folder_when_generated_content_changes_after_preflight(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            master_path, _, _, _ = write_existing_configured_playlist(output_directory, "Old Picks")
            stale_folder_path = master_path.parent
            added_split_path = stale_folder_path / "splits" / "2-2.csv"
            real_commit = configured.commit_configured_release_playlists

            def mutate_then_commit(**kwargs):
                write_split_csv(
                    added_split_path,
                    [
                        {
                            "Release Id": "222",
                            "Album Name": "New",
                            "Track Number": "1",
                            "Track Name": "New Track",
                            "Artist Name": "Artist",
                            "Spotify Search Query": "New Track",
                        }
                    ],
                )
                return real_commit(**kwargs)

            with patch.object(
                configured,
                "commit_configured_release_playlists",
                side_effect=mutate_then_commit,
            ):
                with self.assertRaises(configured.ConfiguredReleasePlaylistCommitError) as raised:
                    create_configured_batch(
                        config=playlist_config({}),
                        directory=directory,
                        lookup_tracklist=lambda _row: self.fail("removed definitions must not be looked up"),
                    )

            self.assertEqual(raised.exception.failed_path, stale_folder_path)
            self.assertTrue(stale_folder_path.is_dir())
            self.assertTrue(master_path.is_file())
            self.assertTrue(added_split_path.is_file())

    def test_cleanup_preserves_stale_folder_when_metadata_ownership_changes_after_preflight(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            master_path, _, _, _ = write_existing_configured_playlist(output_directory, "Old Picks")
            stale_folder_path = master_path.parent
            metadata_path = stale_folder_path / RELEASE_PLAYLIST_METADATA_FILENAME
            real_commit = configured.commit_configured_release_playlists

            def mutate_then_commit(**kwargs):
                write_release_playlist_metadata(
                    metadata_path,
                    ReleasePlaylistMetadata(
                        schema_version=1,
                        record_type=AD_HOC_RELEASE_PLAYLIST_RECORD_TYPE,
                        playlist_name="Old Picks",
                    ),
                )
                return real_commit(**kwargs)

            with patch.object(
                configured,
                "commit_configured_release_playlists",
                side_effect=mutate_then_commit,
            ):
                with self.assertRaises(configured.ConfiguredReleasePlaylistCommitError) as raised:
                    create_configured_batch(
                        config=playlist_config({}),
                        directory=directory,
                        lookup_tracklist=lambda _row: self.fail("removed definitions must not be looked up"),
                    )

            self.assertEqual(raised.exception.failed_path, stale_folder_path)
            self.assertTrue(stale_folder_path.is_dir())
            self.assertTrue(master_path.is_file())
            self.assertIn(
                AD_HOC_RELEASE_PLAYLIST_RECORD_TYPE,
                metadata_path.read_text(encoding="utf-8"),
            )

    def test_cleanup_preserves_replacement_at_stale_folder_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            master_path, _, _, _ = write_existing_configured_playlist(output_directory, "Old Picks")
            stale_folder_path = master_path.parent
            original_folder_path = directory / "original-old-picks"
            replacement_master_path = stale_folder_path / "Old Picks.csv"
            real_commit = configured.commit_configured_release_playlists

            def replace_then_commit(**kwargs):
                stale_folder_path.rename(original_folder_path)
                write_configured_metadata(stale_folder_path, "Old Picks")
                replacement_master_path.write_text("replacement\n", encoding="utf-8")
                return real_commit(**kwargs)

            with patch.object(
                configured,
                "commit_configured_release_playlists",
                side_effect=replace_then_commit,
            ):
                with self.assertRaises(configured.ConfiguredReleasePlaylistCommitError) as raised:
                    create_configured_batch(
                        config=playlist_config({}),
                        directory=directory,
                        lookup_tracklist=lambda _row: self.fail("removed definitions must not be looked up"),
                    )

            self.assertEqual(raised.exception.failed_path, stale_folder_path)
            self.assertEqual(replacement_master_path.read_text(encoding="utf-8"), "replacement\n")
            self.assertTrue(original_folder_path.is_dir())

    def test_unsafe_removed_folder_fails_preflight_before_commit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            stale_folder_path = output_directory / "Old Picks"
            write_configured_metadata(stale_folder_path, "Old Picks")
            unexpected_path = stale_folder_path / "personal-notes.txt"
            unexpected_path.write_text("keep", encoding="utf-8")

            with patch.object(
                configured,
                "commit_configured_release_playlists",
                side_effect=AssertionError("commit must not run"),
                create=True,
            ):
                with self.assertRaisesRegex(ValueError, "unexpected content"):
                    create_configured_batch(
                        config=playlist_config({}),
                        directory=directory,
                        lookup_tracklist=lambda _row: self.fail("preflight must run before lookup"),
                    )

            self.assertEqual(unexpected_path.read_text(encoding="utf-8"), "keep")

    def test_empty_current_definition_removes_old_direct_child_split_csvs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            master_path, old_split_path, _, _ = write_existing_configured_playlist(
                output_directory,
                "Empty",
            )
            preserved_path = master_path.parent / "splits" / "notes.txt"
            preserved_path.write_text("keep", encoding="utf-8")

            summary = create_configured_batch(
                config=playlist_config({"Empty": []}),
                directory=directory,
                lookup_tracklist=lambda _row: self.fail("empty definitions must not be looked up"),
            )

            self.assertEqual(read_csv_rows(master_path), [])
            self.assertFalse(old_split_path.exists())
            self.assertEqual(preserved_path.read_text(encoding="utf-8"), "keep")
            self.assertEqual(summary.playlists[0].track_row_count, 0)
            self.assertEqual(summary.playlists[0].split_summary.written_split_paths, ())

    def test_unknown_folder_remains_and_is_listed_in_generation_report(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            unknown_folder_path = directory / "release-playlists" / "Personal"
            unknown_file_path = unknown_folder_path / "notes.txt"
            unknown_file_path.parent.mkdir(parents=True)
            unknown_file_path.write_text("keep", encoding="utf-8")

            summary = create_configured_batch(
                config=playlist_config({}),
                directory=directory,
                lookup_tracklist=lambda _row: self.fail("empty config must not be looked up"),
            )

            self.assertEqual(unknown_file_path.read_text(encoding="utf-8"), "keep")
            self.assertEqual(summary.ignored_folder_paths, (unknown_folder_path,))
            report_text = summary.report_path.read_text(encoding="utf-8")
            self.assertIn(str(unknown_folder_path), report_text)
            self.assertIn("Collection master: not read or written", report_text)

    def test_preparation_error_preserves_every_master_and_writes_failure_report(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            first_master, _, first_master_text, _ = write_existing_configured_playlist(
                output_directory,
                "Friday Picks",
            )
            second_master, _, second_master_text, _ = write_existing_configured_playlist(
                output_directory,
                "Saturday Picks",
            )

            def lookup(row: Mapping[str, str]) -> ReleaseTracklistLookup:
                if row["release_id"] == "333":
                    raise RuntimeError("Discogs unavailable")
                return lookup_fixture(
                    row["release_id"],
                    (DiscogsTrack(position="1", title="Prepared", artist_name="Artist"),),
                )

            with self.assertRaisesRegex(RuntimeError, "Discogs unavailable"):
                create_configured_batch(
                    config=playlist_config({"Friday Picks": ["222"], "Saturday Picks": ["333"]}),
                    directory=directory,
                    lookup_tracklist=lookup,
                )

            self.assertEqual(first_master.read_text(encoding="utf-8"), first_master_text)
            self.assertEqual(second_master.read_text(encoding="utf-8"), second_master_text)
            failure_report = (directory / "configured-report.txt").read_text(encoding="utf-8")
            self.assertIn("Discogs unavailable", failure_report)
            self.assertIn("Completed paths\n", failure_report)
            self.assertIn("- None", failure_report)

    def test_commit_error_returns_no_summary_and_reports_completed_and_failed_final_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            final_folder_path = directory / "release-playlists" / "Friday Picks"
            master_path = final_folder_path / "Friday Picks.csv"
            metadata_path = final_folder_path / RELEASE_PLAYLIST_METADATA_FILENAME
            real_commit = getattr(configured, "commit_configured_release_playlists", None)
            self.assertIsNotNone(real_commit, "commit_configured_release_playlists is not implemented")
            assert real_commit is not None

            def fail_metadata_commit(**kwargs):
                with patch.object(
                    configured,
                    "write_release_playlist_metadata",
                    side_effect=OSError("disk full"),
                ):
                    return real_commit(**kwargs)

            publishable_summary = None
            with patch.object(configured, "commit_configured_release_playlists", side_effect=fail_metadata_commit):
                with self.assertRaisesRegex(OSError, "configured release playlist commit failed") as raised:
                    publishable_summary = create_configured_batch(
                        config=playlist_config({"Friday Picks": ["222"]}),
                        directory=directory,
                        lookup_tracklist=lambda _row: lookup_fixture(
                            "222",
                            (DiscogsTrack(position="1", title="New", artist_name="Artist 222"),),
                        ),
                    )

            self.assertIsNone(publishable_summary)
            error = raised.exception
            self.assertEqual(getattr(error, "completed_paths", None), (master_path,))
            self.assertEqual(getattr(error, "failed_path", None), metadata_path)
            failure_report = (directory / "configured-report.txt").read_text(encoding="utf-8")
            self.assertIn(f"- {master_path}", failure_report)
            self.assertIn(f"Failed path: {metadata_path}", failure_report)
            self.assertNotIn(".configured-release-playlists-", failure_report)

    def test_split_discovery_error_reports_prior_final_writes_and_failed_final_split_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            final_folder_path = directory / "release-playlists" / "Friday Picks"
            master_path = final_folder_path / "Friday Picks.csv"
            metadata_path = final_folder_path / RELEASE_PLAYLIST_METADATA_FILENAME
            final_splits_directory = final_folder_path / "splits"
            real_glob = Path.glob

            def fail_staged_split_enumeration(path: Path, pattern: str):
                if (
                    pattern == "*.csv"
                    and path.name == "splits"
                    and path.parent.parent.name.startswith(".configured-release-playlists-")
                ):
                    raise OSError("split discovery failed")
                return real_glob(path, pattern)

            publishable_summary = None
            with patch.object(Path, "glob", new=fail_staged_split_enumeration):
                with self.assertRaises(OSError) as raised:
                    publishable_summary = create_configured_batch(
                        config=playlist_config({"Friday Picks": ["222"]}),
                        directory=directory,
                        lookup_tracklist=lambda _row: lookup_fixture(
                            "222",
                            (DiscogsTrack(position="1", title="New", artist_name="Artist 222"),),
                        ),
                    )

            self.assertIsNone(publishable_summary)
            error = raised.exception
            self.assertIsInstance(error, configured.ConfiguredReleasePlaylistCommitError)
            assert isinstance(error, configured.ConfiguredReleasePlaylistCommitError)
            self.assertTrue(final_folder_path.is_dir())
            self.assertTrue(master_path.is_file())
            self.assertTrue(metadata_path.is_file())
            self.assertEqual(error.completed_paths, (master_path, metadata_path))
            self.assertEqual(error.failed_path, final_splits_directory)
            failure_report = (directory / "configured-report.txt").read_text(encoding="utf-8")
            self.assertIn(f"- {master_path}", failure_report)
            self.assertIn(f"- {metadata_path}", failure_report)
            self.assertIn(f"Failed path: {final_splits_directory}", failure_report)
            self.assertNotIn(".configured-release-playlists-", failure_report)


class ConfiguredReleasePlaylistPreparationTests(unittest.TestCase):
    def test_strict_rows_reject_lookup_track_with_blank_title(self):
        definition = ConfiguredReleasePlaylist(name="Friday Picks", release_ids=("222",))
        lookup = lookup_fixture(
            "222",
            (DiscogsTrack(position="1", title="   ", artist_name="Artist 222"),),
        )

        with self.assertRaisesRegex(ValueError, "no usable Discogs tracks"):
            build_strict_playlist_rows(definition, lambda _row: lookup)

    def test_prepare_rejects_symlinked_staging_root_without_changing_final_playlist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            master_path, split_path, master_text, split_text = write_existing_configured_playlist(
                output_directory,
                "Friday Picks",
            )
            staging_directory = directory / "staging"
            staging_directory.symlink_to(output_directory, target_is_directory=True)
            preflight = configured_preflight_fixture(output_directory, {"Friday Picks": ["222"]})

            with self.assertRaisesRegex(ValueError, "staging directory symlinks are not supported"):
                prepare_configured_release_playlists(
                    preflight=preflight,
                    staging_directory=staging_directory,
                    workflow_config=WorkflowConfig(500, True, True),
                    lookup_tracklist=lambda _row: lookup_fixture(
                        "222",
                        (DiscogsTrack(position="1", title="New", artist_name="Artist 222"),),
                    ),
                )

            self.assertEqual(master_path.read_text(encoding="utf-8"), master_text)
            self.assertEqual(split_path.read_text(encoding="utf-8"), split_text)

    def test_prepare_rejects_symlinked_staged_target_before_writing_any_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            first_master, first_split, first_master_text, first_split_text = write_existing_configured_playlist(
                output_directory,
                "Friday Picks",
            )
            second_master, second_split, second_master_text, second_split_text = write_existing_configured_playlist(
                output_directory,
                "Saturday Picks",
            )
            staging_directory = directory / "staging"
            staging_directory.mkdir()
            (staging_directory / "Saturday Picks").symlink_to(
                output_directory / "Saturday Picks",
                target_is_directory=True,
            )
            preflight = configured_preflight_fixture(
                output_directory,
                {"Friday Picks": ["222"], "Saturday Picks": ["333"]},
            )

            with self.assertRaisesRegex(ValueError, "staged playlist folder symlinks are not supported"):
                prepare_configured_release_playlists(
                    preflight=preflight,
                    staging_directory=staging_directory,
                    workflow_config=WorkflowConfig(500, True, True),
                    lookup_tracklist=lambda row: lookup_fixture(
                        row["release_id"],
                        (DiscogsTrack(position="1", title="New", artist_name="Artist"),),
                    ),
                )

            self.assertFalse((staging_directory / "Friday Picks" / "Friday Picks.csv").exists())
            self.assertEqual(first_master.read_text(encoding="utf-8"), first_master_text)
            self.assertEqual(first_split.read_text(encoding="utf-8"), first_split_text)
            self.assertEqual(second_master.read_text(encoding="utf-8"), second_master_text)
            self.assertEqual(second_split.read_text(encoding="utf-8"), second_split_text)

    def test_prepare_rejects_staging_directory_that_overlaps_final_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            master_path, split_path, master_text, split_text = write_existing_configured_playlist(
                output_directory,
                "Friday Picks",
            )
            preflight = configured_preflight_fixture(output_directory, {"Friday Picks": ["222"]})

            with self.assertRaisesRegex(ValueError, "staging directory overlaps configured release playlist output"):
                prepare_configured_release_playlists(
                    preflight=preflight,
                    staging_directory=output_directory,
                    workflow_config=WorkflowConfig(500, True, True),
                    lookup_tracklist=lambda _row: lookup_fixture(
                        "222",
                        (DiscogsTrack(position="1", title="New", artist_name="Artist 222"),),
                    ),
                )

            self.assertEqual(master_path.read_text(encoding="utf-8"), master_text)
            self.assertEqual(split_path.read_text(encoding="utf-8"), split_text)

    def test_prepare_rejects_staged_split_csv_symlink_before_writing_target_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            staging_directory = directory / "staging"
            staged_folder = staging_directory / "Friday Picks"
            staged_split = staged_folder / "splits" / "1-1.csv"
            staged_split.parent.mkdir(parents=True)
            external_csv = directory / "external.csv"
            external_text = "keep external split\n"
            external_csv.write_text(external_text, encoding="utf-8")
            staged_split.symlink_to(external_csv)
            preflight = configured_preflight_fixture(directory / "release-playlists", {"Friday Picks": ["222"]})

            with self.assertRaisesRegex(ValueError, "staged split CSV symlinks are not supported"):
                prepare_configured_release_playlists(
                    preflight=preflight,
                    staging_directory=staging_directory,
                    workflow_config=WorkflowConfig(500, True, True),
                    lookup_tracklist=lambda _row: lookup_fixture(
                        "222",
                        (DiscogsTrack(position="1", title="New", artist_name="Artist 222"),),
                    ),
                )

            self.assertFalse((staged_folder / "Friday Picks.csv").exists())
            self.assertFalse((staged_folder / RELEASE_PLAYLIST_METADATA_FILENAME).exists())
            self.assertEqual(external_csv.read_text(encoding="utf-8"), external_text)

    def test_prepare_rejects_staged_metadata_directory_before_writing_master(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            staging_directory = directory / "staging"
            staged_folder = staging_directory / "Friday Picks"
            staged_metadata_path = staged_folder / RELEASE_PLAYLIST_METADATA_FILENAME
            staged_metadata_path.mkdir(parents=True)
            preflight = configured_preflight_fixture(directory / "release-playlists", {"Friday Picks": ["222"]})

            with self.assertRaisesRegex(ValueError, "staged release playlist metadata path is not a file"):
                prepare_configured_release_playlists(
                    preflight=preflight,
                    staging_directory=staging_directory,
                    workflow_config=WorkflowConfig(500, True, True),
                    lookup_tracklist=lambda _row: lookup_fixture(
                        "222",
                        (DiscogsTrack(position="1", title="New", artist_name="Artist 222"),),
                    ),
                )

            self.assertFalse((staged_folder / "Friday Picks.csv").exists())
            self.assertTrue(staged_metadata_path.is_dir())

    def test_prepare_preserves_configured_release_and_discogs_track_order_without_collection_master(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            preflight = configured_preflight_fixture(output_directory, {"Friday Picks": ["222", "111"]})
            lookups = {
                "222": lookup_fixture(
                    "222",
                    (
                        DiscogsTrack(position="B2", title="Second on Disc", artist_name="Artist 222"),
                        DiscogsTrack(position="A1", title="First on Disc", artist_name="Artist 222"),
                    ),
                ),
                "111": lookup_fixture(
                    "111",
                    (DiscogsTrack(position="1", title="Only Track", artist_name="Artist 111"),),
                ),
            }
            stale_staged_split = directory / "staging" / "Friday Picks" / "splits" / "9-9.csv"
            write_split_csv(
                stale_staged_split,
                [
                    {
                        "Release Id": "stale",
                        "Album Name": "Stale",
                        "Track Number": "1",
                        "Track Name": "Stale",
                        "Artist Name": "Stale",
                        "Spotify Search Query": "Stale",
                    }
                ],
            )

            prepared = prepare_configured_release_playlists(
                preflight=preflight,
                staging_directory=directory / "staging",
                workflow_config=WorkflowConfig(500, True, True),
                lookup_tracklist=lambda row: lookups[row["release_id"]],
            )

            self.assertEqual(len(prepared.playlists), 1)
            rows = read_csv_rows(prepared.playlists[0].staged_master_path)
            self.assertEqual([row["Release Id"] for row in rows], ["222", "222", "111"])
            self.assertEqual([row["Track Name"] for row in rows], ["Second on Disc", "First on Disc", "Only Track"])
            self.assertFalse(output_directory.exists())

    def test_prepare_lookup_exception_keeps_existing_final_master_unchanged(self):
        existing_text = (
            "Release Id,Album Name,Track Number,Track Name,Artist Name,Spotify Search Query\n"
            "111,Old,1,Old Track,Artist,Old Track\n"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            existing_master = output_directory / "Friday Picks" / "Friday Picks.csv"
            write_configured_metadata(existing_master.parent, "Friday Picks")
            existing_master.write_text(existing_text, encoding="utf-8")
            preflight = configured_preflight_fixture(output_directory, {"Friday Picks": ["222"]})

            with self.assertRaisesRegex(RuntimeError, "Discogs unavailable"):
                prepare_configured_release_playlists(
                    preflight=preflight,
                    staging_directory=directory / "staging",
                    workflow_config=WorkflowConfig(500, True, True),
                    lookup_tracklist=lambda _row: (_ for _ in ()).throw(RuntimeError("Discogs unavailable")),
                )

            self.assertEqual(existing_master.read_text(encoding="utf-8"), existing_text)

    def test_prepare_rejects_empty_tracklist_without_changing_existing_master(self):
        existing_text = "Release Id,Album Name,Track Number,Track Name,Artist Name,Spotify Search Query\n111,Old,1,Old Track,Artist,Old Track\n"
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            existing_master = output_directory / "Friday Picks" / "Friday Picks.csv"
            write_configured_metadata(existing_master.parent, "Friday Picks")
            existing_master.write_text(existing_text, encoding="utf-8")
            preflight = configured_preflight_fixture(output_directory, {"Friday Picks": ["222"]})
            empty_lookup = ReleaseTracklistLookup(
                release_id="222",
                artist_name="Artist",
                album_name="Album",
                record_year="2026",
                tracks=(),
                notes=("no Discogs tracklist found",),
            )

            with self.assertRaisesRegex(ValueError, "no usable Discogs tracks"):
                prepare_configured_release_playlists(
                    preflight=preflight,
                    staging_directory=directory / "staging",
                    workflow_config=WorkflowConfig(500, True, True),
                    lookup_tracklist=lambda _row: empty_lookup,
                )

            self.assertEqual(existing_master.read_text(encoding="utf-8"), existing_text)

    def test_prepare_empty_definition_writes_header_only_staged_master_without_lookup_or_splits(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            preflight = configured_preflight_fixture(output_directory, {"Empty": []})
            lookup_count = 0

            def unexpected_lookup(_row: Mapping[str, str]) -> ReleaseTracklistLookup:
                nonlocal lookup_count
                lookup_count += 1
                raise AssertionError("empty definitions must not call Discogs")

            prepared = prepare_configured_release_playlists(
                preflight=preflight,
                staging_directory=directory / "staging",
                workflow_config=WorkflowConfig(1, False, False),
                lookup_tracklist=unexpected_lookup,
            )

            playlist = prepared.playlists[0]
            self.assertEqual(lookup_count, 0)
            self.assertEqual(playlist.output_rows, ())
            self.assertEqual(playlist.staged_master_path.read_text(encoding="utf-8"), ",".join(TUNEMYMUSIC_COLUMNS) + "\n")
            self.assertFalse((playlist.staged_folder_path / "splits").exists())
            self.assertFalse(output_directory.exists())

    def test_prepare_uses_workflow_split_size_and_preserves_release_track_groups(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            preflight = configured_preflight_fixture(
                directory / "release-playlists",
                {"Friday Picks": ["111", "222"]},
            )
            lookups = {
                "111": lookup_fixture(
                    "111",
                    (
                        DiscogsTrack(position="1", title="One", artist_name="Artist 111"),
                        DiscogsTrack(position="2", title="Two", artist_name="Artist 111"),
                    ),
                ),
                "222": lookup_fixture(
                    "222",
                    (
                        DiscogsTrack(position="1", title="Three", artist_name="Artist 222"),
                        DiscogsTrack(position="2", title="Four", artist_name="Artist 222"),
                    ),
                ),
            }

            prepared = prepare_configured_release_playlists(
                preflight=preflight,
                staging_directory=directory / "staging",
                workflow_config=WorkflowConfig(3, True, True),
                lookup_tracklist=lambda row: lookups[row["release_id"]],
            )

            split_directory = prepared.playlists[0].staged_folder_path / "splits"
            self.assertEqual(sorted(path.name for path in split_directory.glob("*.csv")), ["1-2.csv", "3-4.csv"])
            self.assertEqual([row["Release Id"] for row in read_csv_rows(split_directory / "1-2.csv")], ["111", "111"])
            self.assertEqual([row["Release Id"] for row in read_csv_rows(split_directory / "3-4.csv")], ["222", "222"])

    def test_prepare_copies_only_existing_direct_child_splits_before_stable_planning(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            final_folder = output_directory / "Friday Picks"
            write_configured_metadata(final_folder, "Friday Picks")
            seeded_row = {
                "Release Id": "111",
                "Album Name": "Album 111",
                "Track Number": "1",
                "Track Name": "One",
                "Artist Name": "Artist 111",
                "Spotify Search Query": "Artist 111 One Album 111",
            }
            direct_split = final_folder / "splits" / "1-1.csv"
            write_split_csv(direct_split, [seeded_row])
            nested_split = final_folder / "splits" / "archive" / "2-2.csv"
            write_split_csv(nested_split, [seeded_row])
            stale_staged_split = directory / "staging" / "Friday Picks" / "splits" / "9-9.csv"
            write_split_csv(stale_staged_split, [seeded_row])
            preflight = configured_preflight_fixture(output_directory, {"Friday Picks": ["111", "222"]})
            lookups = {
                "111": lookup_fixture("111", (DiscogsTrack(position="1", title="One", artist_name="Artist 111"),)),
                "222": lookup_fixture("222", (DiscogsTrack(position="1", title="Two", artist_name="Artist 222"),)),
            }

            prepared = prepare_configured_release_playlists(
                preflight=preflight,
                staging_directory=directory / "staging",
                workflow_config=WorkflowConfig(500, True, True),
                lookup_tracklist=lambda row: lookups[row["release_id"]],
            )

            staged_splits = prepared.playlists[0].staged_folder_path / "splits"
            self.assertEqual((staged_splits / "1-1.csv").read_text(encoding="utf-8"), direct_split.read_text(encoding="utf-8"))
            self.assertTrue((staged_splits / "2-2.csv").exists())
            self.assertFalse((staged_splits / "9-9.csv").exists())
            self.assertFalse((staged_splits / "archive" / "2-2.csv").exists())
            self.assertFalse((final_folder / "Friday Picks.csv").exists())

    def test_prepare_append_to_latest_regenerates_malformed_owned_range_seed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            _, existing_split, _, _ = write_existing_configured_playlist(
                output_directory,
                "Friday Picks",
            )
            malformed_split = existing_split.with_name("1-2.csv")
            existing_split.replace(malformed_split)
            preflight = configured_preflight_fixture(output_directory, {"Friday Picks": ["111"]})

            try:
                prepared = prepare_configured_release_playlists(
                    preflight=preflight,
                    staging_directory=directory / "staging",
                    workflow_config=WorkflowConfig(500, True, False),
                    lookup_tracklist=lambda _row: lookup_fixture(
                        "111",
                        (DiscogsTrack(position="1", title="Authoritative", artist_name="Artist 111"),),
                    ),
                )
            except ValueError as error:
                self.fail(f"append-to-latest should regenerate a malformed owned split seed: {error}")

            staged_splits = prepared.playlists[0].staged_folder_path / "splits"
            self.assertEqual(sorted(path.name for path in staged_splits.glob("*.csv")), ["1-1.csv"])
            self.assertEqual(
                [row["Track Name"] for row in read_csv_rows(staged_splits / "1-1.csv")],
                ["Authoritative"],
            )
            self.assertFalse((staged_splits / "1-2.csv").exists())

    def test_prepare_append_to_latest_propagates_existing_split_discovery_oserror(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            write_existing_configured_playlist(output_directory, "Friday Picks")
            preflight = configured_preflight_fixture(output_directory, {"Friday Picks": ["111"]})

            with patch.object(
                configured.splitter,
                "read_existing_split_files",
                side_effect=OSError("split discovery unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "split discovery unavailable"):
                    prepare_configured_release_playlists(
                        preflight=preflight,
                        staging_directory=directory / "staging",
                        workflow_config=WorkflowConfig(500, True, False),
                        lookup_tracklist=lambda _row: lookup_fixture(
                            "111",
                            (DiscogsTrack(position="1", title="Authoritative", artist_name="Artist 111"),),
                        ),
                    )

    def test_prepare_frozen_splits_reject_malformed_owned_range_seed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            _, existing_split, _, _ = write_existing_configured_playlist(
                output_directory,
                "Friday Picks",
            )
            malformed_split = existing_split.with_name("1-2.csv")
            existing_split.replace(malformed_split)
            preflight = configured_preflight_fixture(output_directory, {"Friday Picks": ["111"]})

            with self.assertRaisesRegex(ValueError, "row count 1 does not match advertised range 1-2"):
                prepare_configured_release_playlists(
                    preflight=preflight,
                    staging_directory=directory / "staging",
                    workflow_config=WorkflowConfig(500, True, True),
                    lookup_tracklist=lambda _row: lookup_fixture(
                        "111",
                        (DiscogsTrack(position="1", title="Authoritative", artist_name="Artist 111"),),
                    ),
                )


class ConfiguredReleasePlaylistPreflightTests(unittest.TestCase):
    def test_preflight_accepts_new_active_target_without_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"

            result = preflight_configured_release_playlists(
                playlist_config({"Friday/Picks": ["111"]}),
                output_directory,
            )

            self.assertEqual(len(result.targets), 1)
            target = result.targets[0]
            self.assertEqual(target.definition.name, "Friday/Picks")
            self.assertEqual(target.folder_path, output_directory / "Friday_Picks")
            self.assertEqual(target.master_path, output_directory / "Friday_Picks" / "Friday_Picks.csv")
            self.assertEqual(
                target.metadata_path,
                output_directory / "Friday_Picks" / RELEASE_PLAYLIST_METADATA_FILENAME,
            )
            self.assertEqual(result.stale_folder_paths, ())
            self.assertEqual(result.ignored_folder_paths, ())
            self.assertFalse(output_directory.exists())

    def test_preflight_accepts_existing_active_target_with_matching_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            folder_path = output_directory / "Friday_Picks"
            write_configured_metadata(folder_path, "Friday/Picks")

            result = preflight_configured_release_playlists(
                playlist_config({"Friday/Picks": ["111"]}),
                output_directory,
            )

            self.assertEqual(tuple(target.folder_path for target in result.targets), (folder_path,))
            self.assertEqual(result.stale_folder_paths, ())
            self.assertEqual(result.ignored_folder_paths, ())

    def test_preflight_rejects_unknown_directory_at_active_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            folder_path = output_directory / "Friday Picks"
            folder_path.mkdir(parents=True)

            with self.assertRaisesRegex(ValueError, "has no configured release-playlist metadata"):
                preflight_configured_release_playlists(
                    playlist_config({"Friday Picks": ["111"]}),
                    output_directory,
                )

            self.assertTrue(folder_path.exists())

    def test_preflight_rejects_non_directory_active_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            output_directory.mkdir()
            target_path = output_directory / "Friday Picks"
            target_path.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "is not a directory"):
                preflight_configured_release_playlists(
                    playlist_config({"Friday Picks": ["111"]}),
                    output_directory,
                )

            self.assertEqual(target_path.read_text(encoding="utf-8"), "keep")

    def test_preflight_rejects_active_target_folder_symlink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            output_directory.mkdir()
            outside_directory = directory / "outside"
            outside_directory.mkdir()
            (output_directory / "Friday Picks").symlink_to(outside_directory, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "folder symlinks are not supported"):
                preflight_configured_release_playlists(
                    playlist_config({"Friday Picks": ["111"]}),
                    output_directory,
                )

    def test_preflight_rejects_active_metadata_symlink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            folder_path = output_directory / "Friday Picks"
            folder_path.mkdir(parents=True)
            outside_metadata = write_configured_metadata(directory / "outside", "Friday Picks")
            (folder_path / RELEASE_PLAYLIST_METADATA_FILENAME).symlink_to(outside_metadata)

            with self.assertRaisesRegex(ValueError, "metadata symlinks are not supported"):
                preflight_configured_release_playlists(
                    playlist_config({"Friday Picks": ["111"]}),
                    output_directory,
                )

    def test_preflight_rejects_active_master_csv_symlink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            folder_path = output_directory / "Friday Picks"
            write_configured_metadata(folder_path, "Friday Picks")
            shared_csv = output_directory / "shared.csv"
            shared_csv.write_text("keep", encoding="utf-8")
            (folder_path / "Friday Picks.csv").symlink_to(shared_csv)

            with self.assertRaisesRegex(ValueError, "master CSV symlinks are not supported"):
                preflight_configured_release_playlists(
                    playlist_config({"Friday Picks": ["111"]}),
                    output_directory,
                )

            self.assertEqual(shared_csv.read_text(encoding="utf-8"), "keep")

    def test_preflight_rejects_malformed_active_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            metadata_path = output_directory / "Friday Picks" / RELEASE_PLAYLIST_METADATA_FILENAME
            metadata_path.parent.mkdir(parents=True)
            metadata_path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "malformed release playlist metadata"):
                preflight_configured_release_playlists(
                    playlist_config({"Friday Picks": ["111"]}),
                    output_directory,
                )

    def test_preflight_rejects_active_metadata_with_wrong_record_type(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            folder_path = output_directory / "Friday Picks"
            write_release_playlist_metadata(
                folder_path / RELEASE_PLAYLIST_METADATA_FILENAME,
                ReleasePlaylistMetadata(
                    schema_version=1,
                    record_type=AD_HOC_RELEASE_PLAYLIST_RECORD_TYPE,
                    playlist_name="Friday Picks",
                ),
            )

            with self.assertRaisesRegex(ValueError, "unsupported release playlist metadata record type"):
                preflight_configured_release_playlists(
                    playlist_config({"Friday Picks": ["111"]}),
                    output_directory,
                )

    def test_preflight_rejects_active_metadata_name_that_differs_from_definition(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            folder_path = output_directory / "Friday_Picks"
            write_configured_metadata(folder_path, "Friday:Picks")

            with self.assertRaisesRegex(ValueError, "metadata playlist name"):
                preflight_configured_release_playlists(
                    playlist_config({"Friday/Picks": ["111"]}),
                    output_directory,
                )

    def test_preflight_rejects_sanitized_target_collisions_even_for_preconstructed_config(self):
        config = playlist_config({})
        config = replace(
            config,
            release_playlists=(
                ConfiguredReleasePlaylist(name="Friday/Picks", release_ids=("111",)),
                ConfiguredReleasePlaylist(name="Friday:Picks", release_ids=("222",)),
            ),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "same configured release playlist folder"):
                preflight_configured_release_playlists(config, Path(temporary_directory))

    def test_preflight_rejects_target_path_escape_even_for_preconstructed_config(self):
        config = playlist_config({})
        config = replace(
            config,
            release_playlists=(ConfiguredReleasePlaylist(name="..", release_ids=("111",)),),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "outside playlist output directory"):
                preflight_configured_release_playlists(config, Path(temporary_directory) / "release-playlists")

    def test_preflight_rejects_root_collapsing_target_even_for_preconstructed_config(self):
        config = playlist_config({})
        config = replace(
            config,
            release_playlists=(ConfiguredReleasePlaylist(name=".", release_ids=("111",)),),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"

            with self.assertRaisesRegex(ValueError, "strict direct child"):
                preflight_configured_release_playlists(config, output_directory)

            self.assertFalse(output_directory.exists())

    def test_preflight_classifies_owned_removed_folder_as_stale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            stale_folder = output_directory / "Old_Picks"
            write_configured_metadata(stale_folder, "Old/Picks")
            (stale_folder / "Old_Picks.csv").write_text("Track name\n", encoding="utf-8")
            splits_directory = stale_folder / "splits"
            splits_directory.mkdir()
            (splits_directory / "Old_Picks_001.csv").write_text("Track name\n", encoding="utf-8")

            result = preflight_configured_release_playlists(playlist_config({}), output_directory)

            self.assertEqual(result.stale_folder_paths, (stale_folder,))
            self.assertEqual(result.ignored_folder_paths, ())
            self.assertTrue(stale_folder.exists())

    def test_preflight_classifies_unknown_folder_without_metadata_as_ignored(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            unknown_folder = output_directory / "Personal"
            nested_owned_folder = unknown_folder / "nested"
            write_configured_metadata(nested_owned_folder, "nested")

            result = preflight_configured_release_playlists(playlist_config({}), output_directory)

            self.assertEqual(result.stale_folder_paths, ())
            self.assertEqual(result.ignored_folder_paths, (unknown_folder,))
            self.assertTrue(nested_owned_folder.exists())

    def test_preflight_rejects_stale_metadata_name_that_does_not_resolve_to_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            stale_folder = output_directory / "Other"
            write_configured_metadata(stale_folder, "Old Picks")

            with self.assertRaisesRegex(ValueError, "metadata playlist name"):
                preflight_configured_release_playlists(playlist_config({}), output_directory)

            self.assertTrue(stale_folder.exists())

    def test_preflight_rejects_unknown_content_in_removed_owned_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            stale_folder = output_directory / "Old Picks"
            write_configured_metadata(stale_folder, "Old Picks")
            (stale_folder / "personal-notes.txt").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unexpected content"):
                preflight_configured_release_playlists(playlist_config({}), output_directory)

            self.assertTrue(stale_folder.exists())

    def test_preflight_rejects_master_csv_symlink_in_removed_owned_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            stale_folder = output_directory / "Old Picks"
            write_configured_metadata(stale_folder, "Old Picks")
            outside_csv = directory / "outside.csv"
            outside_csv.write_text("keep", encoding="utf-8")
            (stale_folder / "Old Picks.csv").symlink_to(outside_csv)

            with self.assertRaisesRegex(ValueError, "master CSV symlinks are not supported"):
                preflight_configured_release_playlists(playlist_config({}), output_directory)

            self.assertTrue(stale_folder.exists())
            self.assertEqual(outside_csv.read_text(encoding="utf-8"), "keep")

    def test_preflight_rejects_master_csv_directory_in_removed_owned_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            stale_folder = output_directory / "Old Picks"
            write_configured_metadata(stale_folder, "Old Picks")
            master_path = stale_folder / "Old Picks.csv"
            master_path.mkdir()

            with self.assertRaisesRegex(ValueError, "master CSV path is not a file"):
                preflight_configured_release_playlists(playlist_config({}), output_directory)

            self.assertTrue(master_path.is_dir())

    def test_preflight_rejects_symlinked_splits_directory_in_removed_owned_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            stale_folder = output_directory / "Old Picks"
            write_configured_metadata(stale_folder, "Old Picks")
            outside_directory = directory / "outside"
            outside_directory.mkdir()
            (stale_folder / "splits").symlink_to(outside_directory, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "splits directory symlinks are not supported"):
                preflight_configured_release_playlists(playlist_config({}), output_directory)

            self.assertTrue(outside_directory.exists())

    def test_preflight_rejects_non_directory_splits_path_in_removed_owned_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            stale_folder = output_directory / "Old Picks"
            write_configured_metadata(stale_folder, "Old Picks")
            (stale_folder / "splits").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "splits path is not a directory"):
                preflight_configured_release_playlists(playlist_config({}), output_directory)

    def test_preflight_rejects_non_csv_split_entry_in_removed_owned_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "release-playlists"
            stale_folder = output_directory / "Old Picks"
            write_configured_metadata(stale_folder, "Old Picks")
            splits_directory = stale_folder / "splits"
            splits_directory.mkdir()
            (splits_directory / "notes.txt").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unexpected content"):
                preflight_configured_release_playlists(playlist_config({}), output_directory)

    def test_preflight_rejects_symlinked_split_csv_in_removed_owned_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            stale_folder = output_directory / "Old Picks"
            write_configured_metadata(stale_folder, "Old Picks")
            splits_directory = stale_folder / "splits"
            splits_directory.mkdir()
            outside_csv = directory / "outside.csv"
            outside_csv.write_text("keep", encoding="utf-8")
            (splits_directory / "Old Picks_001.csv").symlink_to(outside_csv)

            with self.assertRaisesRegex(ValueError, "unexpected content"):
                preflight_configured_release_playlists(playlist_config({}), output_directory)

            self.assertEqual(outside_csv.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
