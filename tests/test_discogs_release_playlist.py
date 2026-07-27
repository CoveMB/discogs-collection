import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_playlist_exporter as exporter  # noqa: E402
import discogs_release_playlist as release_playlist  # noqa: E402
import discogs_playlist_splitter as splitter  # noqa: E402
import configured_release_playlists as configured  # noqa: E402
from shared.playlist_config import (  # noqa: E402
    ConfiguredReleasePlaylist,
    PlaylistConfig,
)
from shared.publisher_config import PublisherConfig  # noqa: E402
from shared.playlist_selection import resolve_all_playlist_master_paths  # noqa: E402
from shared.workflow_config import WorkflowConfig  # noqa: E402


def read_csv_file(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


def lookup_for(release_id: str) -> exporter.ReleaseTracklistLookup:
    return exporter.ReleaseTracklistLookup(
        release_id=release_id,
        artist_name=f"Artist {release_id}",
        album_name=f"Album {release_id}",
        record_year="1997",
        tracks=(
            exporter.DiscogsTrack(
                position="A1",
                title=f"Track {release_id}",
                artist_name=f"Artist {release_id}",
            ),
        ),
        notes=(),
    )


def publish_summary_stub():
    return release_playlist.spotify_publisher.SpotifyPublishSummary(
        playlist_count=1,
        report_path=Path("reports/spotify_playlist_publish_report.txt"),
        track_count=1,
        run_status="complete",
        cache_hit_count=0,
        search_count=1,
        searched_row_count=1,
        matched_count=1,
        already_present_count=0,
        would_add_count=1,
        added_count=0,
        would_include_count=0,
        included_count=0,
        duplicate_in_source_count=0,
        ambiguous_count=0,
        unmatched_count=0,
        error_count=0,
        apply=False,
        publisher_sync_mode="replace",
        decisions=(),
        final_items=(),
        playlist_contexts=(),
    )


def playlist_config_stub(
    release_playlists: tuple[ConfiguredReleasePlaylist, ...],
    playlist_labels: tuple[str, ...] = (),
) -> PlaylistConfig:
    return PlaylistConfig(
        excluded_terms=(),
        excluded_term_keys=frozenset(),
        playlist_labels=playlist_labels,
        raw_aliases_by_label={label: () for label in playlist_labels},
        alias_keys_by_label={label: () for label in playlist_labels},
        release_playlists=release_playlists,
    )


def configured_summary_stub(
    *,
    output_directory: Path,
    report_path: Path,
    split_report_path: Path,
    playlists: tuple[SimpleNamespace, ...] = (),
    deleted_folder_paths: tuple[Path, ...] = (),
) -> SimpleNamespace:
    master_paths = tuple(playlist.master_path for playlist in playlists)
    return SimpleNamespace(
        config_path=Path("config/playlist-map.json"),
        output_directory=output_directory,
        report_path=report_path,
        split_report_path=split_report_path,
        playlists=playlists,
        deleted_folder_paths=deleted_folder_paths,
        ignored_folder_paths=(),
        master_paths=master_paths,
        playlist_names_by_master_path={
            playlist.master_path: playlist.playlist_name for playlist in playlists
        },
    )


def run_configured(args):
    runner = getattr(release_playlist, "run_configured_release_playlist", None)
    if runner is None:
        raise AssertionError("run_configured_release_playlist is not implemented")
    return runner(args)


class DiscogsReleasePlaylistTests(unittest.TestCase):
    def test_help_describes_mode_specific_configured_report_defaults(self):
        stdout = io.StringIO()

        with (
            patch("sys.stdout", stdout),
            patch.dict("os.environ", {"COLUMNS": "500"}),
            self.assertRaises(SystemExit) as exit_context,
        ):
            release_playlist.parse_args(["--help"])

        self.assertEqual(exit_context.exception.code, 0)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn(
            "Create isolated ad-hoc or configured publisher playlists from Discogs release IDs.",
            help_text,
        )
        self.assertIn(
            "Configured release playlist split report path. Defaults to reports/<timestamp>_configured_release_playlist_splitter.txt.",
            help_text,
        )
        self.assertIn(
            "Spotify publisher report path. Ad-hoc mode defaults to reports/<timestamp>_publish_playlist.txt; configured mode defaults to reports/<timestamp>_configured_release_playlist_publisher.txt.",
            help_text,
        )

    def assert_configured_output_alias_rejected_before_config_or_lookup(
        self,
        *,
        output_directory: Path,
        on_the_fly_directory: Path,
        report_path: Path,
    ) -> None:
        args = release_playlist.parse_args(
            [
                "--from-config",
                "--publisher",
                "none",
                "--output-dir",
                str(output_directory),
                "--report",
                str(report_path),
            ]
        )
        with (
            patch.object(
                release_playlist,
                "DEFAULT_ON_THE_FLY_PLAYLIST_DIRECTORY",
                on_the_fly_directory,
            ),
            patch(
                "shared.playlist_config.load_playlist_config",
                side_effect=ValueError("playlist config loaded before ownership rejection"),
            ) as load_playlist_config,
            patch(
                "discogs_release_playlist.tracklists.make_cached_tracklist_lookup"
            ) as make_lookup,
        ):
            with self.assertRaisesRegex(ValueError, "on-the-fly"):
                run_configured(args)

        load_playlist_config.assert_not_called()
        make_lookup.assert_not_called()
        self.assertIn("on-the-fly", report_path.read_text(encoding="utf-8"))

    def test_from_config_uses_configured_defaults_and_forces_replace_mode(self):
        try:
            args = release_playlist.parse_args(["--from-config", "--publisher", "none"])
        except SystemExit:
            self.fail("--from-config should select configured mode without ad-hoc inputs")

        self.assertTrue(getattr(args, "from_config", False))
        self.assertEqual(args.output_dir, Path("collection/playlists/release-playlists"))
        self.assertEqual(args.config, Path("config/playlist-map.json"))
        self.assertEqual(args.workflow_config, Path("config/workflow.json"))
        self.assertEqual(args.publisher_sync_mode, "replace")

    def test_from_config_rejects_ad_hoc_inputs(self):
        invalid_argv = (
            ["--from-config", "--name", "Friday Picks"],
            ["--from-config", "111"],
            ["--from-config", "--release-ids-file", "ids.txt"],
            ["--from-config", "--publisher-sync-mode", "append"],
        )
        for argv in invalid_argv:
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    release_playlist.parse_args(argv)

    def test_ad_hoc_mode_preserves_required_inputs_and_defaults(self):
        invalid_argv = (
            ["111"],
            ["--name", "Friday Picks"],
        )
        for argv in invalid_argv:
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    release_playlist.parse_args(argv)

        args = release_playlist.parse_args(["--name", "Friday Picks", "111"])

        self.assertFalse(getattr(args, "from_config", False))
        self.assertEqual(args.output_dir, Path("collection/playlists/on-the-fly"))
        self.assertEqual(args.publisher_sync_mode, "append")

    def test_from_config_accepts_configured_paths_and_uses_distinct_default_reports(self):
        try:
            args = release_playlist.parse_args(
                [
                    "--from-config",
                    "--publisher",
                    "none",
                    "--config",
                    "custom-playlists.json",
                    "--workflow-config",
                    "custom-workflow.json",
                    "--split-report",
                    "custom-splits.txt",
                    "--max-rows",
                    "25",
                ]
            )
        except SystemExit:
            self.fail("configured paths and split options should parse in --from-config mode")

        self.assertEqual(args.config, Path("custom-playlists.json"))
        self.assertEqual(args.workflow_config, Path("custom-workflow.json"))
        self.assertEqual(args.split_report, Path("custom-splits.txt"))
        self.assertEqual(args.max_rows, 25)

        with patch("shared.reports.readable_timestamp", return_value="2026-07-27_12-00-00"):
            default_args = release_playlist.parse_args(["--from-config", "--publisher", "none"])
        self.assertEqual(
            default_args.split_report,
            Path("reports/2026-07-27_12-00-00_configured_release_playlist_splitter.txt"),
        )
        self.assertEqual(
            default_args.publisher_report,
            Path("reports/2026-07-27_12-00-00_configured_release_playlist_publisher.txt"),
        )

    def test_configured_mode_rejects_output_inside_ad_hoc_root_before_config_or_lookup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            on_the_fly_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "configured.txt"
            with (
                patch.object(
                    release_playlist,
                    "DEFAULT_ON_THE_FLY_PLAYLIST_DIRECTORY",
                    on_the_fly_directory,
                ),
                patch("shared.playlist_config.load_playlist_config") as load_playlist_config,
                patch("discogs_release_playlist.tracklists.make_cached_tracklist_lookup") as make_lookup,
            ):
                for output_directory in (
                    on_the_fly_directory,
                    on_the_fly_directory / "nested",
                ):
                    with self.subTest(output_directory=output_directory):
                        args = release_playlist.parse_args(
                            [
                                "--from-config",
                                "--publisher",
                                "none",
                                "--output-dir",
                                str(output_directory),
                                "--report",
                                str(report_path),
                            ]
                        )
                        with self.assertRaisesRegex(ValueError, "on-the-fly"):
                            run_configured(args)

            load_playlist_config.assert_not_called()
            make_lookup.assert_not_called()
            self.assertIn("on-the-fly", report_path.read_text(encoding="utf-8"))

    def test_configured_mode_rejects_alternate_case_ad_hoc_root_before_config_or_lookup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            on_the_fly_directory = directory / "collection" / "playlists" / "on-the-fly"

            self.assert_configured_output_alias_rejected_before_config_or_lookup(
                output_directory=directory / "collection" / "playlists" / "ON-THE-FLY",
                on_the_fly_directory=on_the_fly_directory,
                report_path=directory / "reports" / "alternate-case-root.txt",
            )

    def test_configured_mode_rejects_child_of_alternate_case_ad_hoc_root_before_config_or_lookup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            on_the_fly_directory = directory / "collection" / "playlists" / "on-the-fly"

            self.assert_configured_output_alias_rejected_before_config_or_lookup(
                output_directory=(
                    directory / "collection" / "playlists" / "ON-THE-FLY" / "Nested"
                ),
                on_the_fly_directory=on_the_fly_directory,
                report_path=directory / "reports" / "alternate-case-child.txt",
            )

    def test_configured_mode_rejects_normal_playlist_root_alias_before_config_or_lookup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            normal_output_directory = directory / "collection" / "playlists"
            mapped_master_path = normal_output_directory / "House" / "House.csv"
            mapped_master_path.parent.mkdir(parents=True)
            mapped_master_path.write_text("Track Name\n", encoding="utf-8")

            for label, output_directory in (
                ("exact", normal_output_directory),
                ("alternate-case", directory / "collection" / "PLAYLISTS"),
            ):
                with self.subTest(label=label):
                    report_path = directory / "reports" / f"{label}.txt"
                    args = release_playlist.parse_args(
                        [
                            "--from-config",
                            "--publisher",
                            "none",
                            "--output-dir",
                            str(output_directory),
                            "--report",
                            str(report_path),
                        ]
                    )
                    with (
                        patch.object(
                            release_playlist,
                            "DEFAULT_PLAYLIST_OUTPUT_DIRECTORY",
                            normal_output_directory,
                            create=True,
                        ),
                        patch(
                            "shared.playlist_config.load_playlist_config",
                            side_effect=ValueError("playlist config loaded before ownership rejection"),
                        ) as load_playlist_config,
                        patch(
                            "discogs_release_playlist.tracklists.make_cached_tracklist_lookup"
                        ) as make_lookup,
                    ):
                        with self.assertRaisesRegex(ValueError, "normal playlist output root"):
                            run_configured(args)

                    load_playlist_config.assert_not_called()
                    make_lookup.assert_not_called()
                    self.assertIn(
                        "normal playlist output root",
                        report_path.read_text(encoding="utf-8"),
                    )

            self.assertEqual(
                resolve_all_playlist_master_paths(normal_output_directory),
                (mapped_master_path,),
            )
            self.assertFalse(
                (normal_output_directory / "Configured Picks" / "Configured Picks.csv").exists()
            )

    def test_configured_output_validation_allows_isolated_nested_and_custom_roots(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            normal_output_directory = directory / "collection" / "playlists"
            allowed_output_directories = (
                normal_output_directory / "release-playlists",
                directory / "custom" / "configured-release-playlists",
            )

            with patch.object(
                release_playlist,
                "DEFAULT_PLAYLIST_OUTPUT_DIRECTORY",
                normal_output_directory,
                create=True,
            ):
                for output_directory in allowed_output_directories:
                    with self.subTest(output_directory=output_directory):
                        release_playlist.validate_configured_output_directory(output_directory)

    def test_configured_config_failure_writes_report_before_lookup_or_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            config_path = directory / "config" / "playlist-map.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("{broken", encoding="utf-8")
            output_directory = directory / "collection" / "playlists" / "release-playlists"
            report_path = directory / "reports" / "configured.txt"
            args = release_playlist.parse_args(
                [
                    "--from-config",
                    "--publisher",
                    "none",
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                ]
            )

            with patch(
                "discogs_release_playlist.tracklists.make_cached_tracklist_lookup"
            ) as make_lookup:
                with self.assertRaises(ValueError):
                    run_configured(args)

            make_lookup.assert_not_called()
            self.assertFalse(output_directory.exists())
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Configured release playlist generation failure report", report_text)
            self.assertIn("playlist map", report_text)

    def test_configured_workflow_and_publisher_config_failures_write_reports(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            config_path = directory / "playlist-map.json"
            config_path.write_text(
                json.dumps(
                    {
                        "excluded_terms": [],
                        "playlists": {},
                        "release_playlists": {"Friday Picks": ["111"]},
                    }
                ),
                encoding="utf-8",
            )
            valid_workflow_path = directory / "valid-workflow.json"
            valid_workflow_path.write_text('{"max_rows_per_split": 500}', encoding="utf-8")
            broken_workflow_path = directory / "broken-workflow.json"
            broken_workflow_path.write_text("{broken", encoding="utf-8")
            broken_publisher_path = directory / "broken-publisher.json"
            broken_publisher_path.write_text("{broken", encoding="utf-8")

            cases = (
                ("workflow config", broken_workflow_path, directory / "unused-publisher.json"),
                ("publisher config", valid_workflow_path, broken_publisher_path),
            )
            with patch(
                "discogs_release_playlist.tracklists.make_cached_tracklist_lookup"
            ) as make_lookup:
                for label, workflow_path, publisher_path in cases:
                    with self.subTest(label=label):
                        output_directory = directory / label.replace(" ", "-")
                        report_path = directory / f"{label.replace(' ', '-')}.txt"
                        args = release_playlist.parse_args(
                            [
                                "--from-config",
                                "--publisher",
                                "none",
                                "--config",
                                str(config_path),
                                "--workflow-config",
                                str(workflow_path),
                                "--publisher-config",
                                str(publisher_path),
                                "--output-dir",
                                str(output_directory),
                                "--report",
                                str(report_path),
                            ]
                        )
                        with self.assertRaises(ValueError):
                            run_configured(args)

                        self.assertFalse(output_directory.exists())
                        self.assertIn(label, report_path.read_text(encoding="utf-8"))

            make_lookup.assert_not_called()

    def test_configured_loads_all_configs_before_lookup_and_resolves_max_rows(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            report_path = directory / "configured.txt"
            split_report_path = directory / "splits.txt"
            args = release_playlist.parse_args(
                [
                    "--from-config",
                    "--publisher",
                    "none",
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--split-report",
                    str(split_report_path),
                    "--max-rows",
                    "25",
                ]
            )
            playlist_config = playlist_config_stub(
                (ConfiguredReleasePlaylist("Friday Picks", ("111",)),)
            )
            publisher_config = PublisherConfig("none", "Normal ", "", "Release ", "!")
            order: list[str] = []
            local_summary = configured_summary_stub(
                output_directory=output_directory,
                report_path=report_path,
                split_report_path=split_report_path,
            )

            def load_playlist(_path):
                order.append("playlist")
                return playlist_config

            def load_workflow(_path):
                order.append("workflow")
                return WorkflowConfig(500, True, True)

            def load_publisher(_path):
                order.append("publisher")
                return publisher_config

            def make_lookup(**_kwargs):
                order.append("lookup")
                return lambda row: lookup_for(row["release_id"])

            def create_local(**kwargs):
                order.append("local")
                self.assertEqual(kwargs["workflow_config"].max_rows_per_split, 25)
                return local_summary

            with (
                patch("shared.playlist_config.load_playlist_config", side_effect=load_playlist),
                patch("shared.workflow_config.load_or_create_workflow_config", side_effect=load_workflow),
                patch(
                    "discogs_release_playlist.load_or_create_publisher_config",
                    side_effect=load_publisher,
                ),
                patch(
                    "discogs_release_playlist.tracklists.make_cached_tracklist_lookup",
                    side_effect=make_lookup,
                ),
                patch(
                    "configured_release_playlists.create_configured_release_playlists",
                    side_effect=create_local,
                ),
            ):
                summary = run_configured(args)

            self.assertEqual(order, ["playlist", "workflow", "publisher", "lookup", "local"])
            self.assertEqual(summary.publisher, "none")

    def test_configured_all_empty_definitions_commit_locally_without_creating_lookup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            args = release_playlist.parse_args(
                [
                    "--from-config",
                    "--publisher",
                    "none",
                    "--output-dir",
                    str(directory / "release-playlists"),
                    "--report",
                    str(directory / "configured.txt"),
                    "--split-report",
                    str(directory / "splits.txt"),
                ]
            )
            config = playlist_config_stub(
                (
                    ConfiguredReleasePlaylist("Empty One", ()),
                    ConfiguredReleasePlaylist("Empty Two", ()),
                )
            )
            local_summary = configured_summary_stub(
                output_directory=args.output_dir,
                report_path=args.report,
                split_report_path=args.split_report,
            )

            with (
                patch("shared.playlist_config.load_playlist_config", return_value=config),
                patch(
                    "shared.workflow_config.load_or_create_workflow_config",
                    return_value=WorkflowConfig(500, True, True),
                ),
                patch(
                    "discogs_release_playlist.load_or_create_publisher_config",
                    return_value=PublisherConfig("none", "", ""),
                ),
                patch("discogs_release_playlist.tracklists.make_cached_tracklist_lookup") as make_lookup,
                patch(
                    "configured_release_playlists.create_configured_release_playlists",
                    return_value=local_summary,
                ) as create_local,
                patch("discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args") as publish,
            ):
                summary = run_configured(args)

            make_lookup.assert_not_called()
            create_local.assert_called_once()
            with self.assertRaisesRegex(AssertionError, "empty release set"):
                create_local.call_args.kwargs["lookup_tracklist"]({"release_id": "111"})
            publish.assert_not_called()
            self.assertIs(summary.local_summary, local_summary)
            self.assertEqual(summary.publisher, "none")

    def test_configured_zero_definitions_skip_spotify_without_removing_remote_playlists(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            args = release_playlist.parse_args(
                [
                    "--from-config",
                    "--publisher",
                    "spotify",
                    "--output-dir",
                    str(directory / "release-playlists"),
                    "--report",
                    str(directory / "configured.txt"),
                    "--split-report",
                    str(directory / "splits.txt"),
                ]
            )
            local_summary = configured_summary_stub(
                output_directory=args.output_dir,
                report_path=args.report,
                split_report_path=args.split_report,
            )

            with (
                patch(
                    "shared.playlist_config.load_playlist_config",
                    return_value=playlist_config_stub(()),
                ),
                patch(
                    "shared.workflow_config.load_or_create_workflow_config",
                    return_value=WorkflowConfig(500, True, True),
                ),
                patch(
                    "discogs_release_playlist.load_or_create_publisher_config",
                    return_value=PublisherConfig("spotify", "", "", "Release ", ""),
                ),
                patch(
                    "configured_release_playlists.create_configured_release_playlists",
                    return_value=local_summary,
                ) as create_local,
                patch("discogs_release_playlist.tracklists.make_cached_tracklist_lookup") as make_lookup,
                patch("discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args") as publish,
            ):
                summary = run_configured(args)

            create_local.assert_called_once()
            make_lookup.assert_not_called()
            publish.assert_not_called()
            self.assertEqual(summary.publisher, "spotify")
            self.assertIsNone(summary.publisher_summary)

    def test_configured_spotify_publishes_only_current_masters_with_release_affixes_and_replace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "release-playlists"
            publisher_report_path = directory / "configured-spotify.txt"
            args = release_playlist.parse_args(
                [
                    "--from-config",
                    "--publisher",
                    "spotify",
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(directory / "configured.txt"),
                    "--split-report",
                    str(directory / "splits.txt"),
                    "--publisher-report",
                    str(publisher_report_path),
                    "--publishing-dry-run",
                ]
            )
            master_path = output_directory / "Friday_Picks" / "Friday_Picks.csv"
            playlist = SimpleNamespace(
                master_path=master_path,
                playlist_name="Friday/Picks",
                track_row_count=1,
            )
            local_summary = configured_summary_stub(
                output_directory=output_directory,
                report_path=args.report,
                split_report_path=args.split_report,
                playlists=(playlist,),
            )
            config = playlist_config_stub(
                (ConfiguredReleasePlaylist("Friday/Picks", ("111",)),)
            )

            with (
                patch("shared.playlist_config.load_playlist_config", return_value=config),
                patch(
                    "shared.workflow_config.load_or_create_workflow_config",
                    return_value=WorkflowConfig(500, True, True),
                ),
                patch(
                    "discogs_release_playlist.load_or_create_publisher_config",
                    return_value=PublisherConfig(
                        "spotify",
                        "Normal ",
                        "?",
                        "Release ",
                        "!",
                    ),
                ),
                patch(
                    "discogs_release_playlist.tracklists.make_cached_tracklist_lookup",
                    return_value=lambda row: lookup_for(row["release_id"]),
                ),
                patch(
                    "configured_release_playlists.create_configured_release_playlists",
                    return_value=local_summary,
                ),
                patch(
                    "discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args",
                    return_value=publish_summary_stub(),
                ) as publish,
            ):
                summary = run_configured(args)

            publisher_args = publish.call_args.args[0]
            projected_config = publish.call_args.kwargs["publisher_config"]
            self.assertEqual(publish.call_args.kwargs["playlist_master_paths"], (master_path,))
            self.assertEqual(
                publish.call_args.kwargs["playlist_names_by_master_path"],
                {master_path: "Friday/Picks"},
            )
            self.assertEqual(projected_config.playlist_prefix, "Release ")
            self.assertEqual(projected_config.playlist_suffix, "!")
            self.assertEqual(publisher_args.publisher_sync_mode, "replace")
            self.assertEqual(publisher_args.playlist_output_dir, output_directory)
            self.assertEqual(publisher_args.report, publisher_report_path)
            self.assertFalse(publisher_args.apply)
            self.assertIsNotNone(summary.publisher_summary)

    def test_configured_target_collision_fails_before_lookup_or_spotify(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            config_path = directory / "playlist-map.json"
            config_path.write_text(
                json.dumps(
                    {
                        "excluded_terms": [],
                        "playlists": {"House": []},
                        "release_playlists": {"HOUSE": ["111"]},
                    }
                ),
                encoding="utf-8",
            )
            publisher_config_path = directory / "publisher.json"
            publisher_config_path.write_text(
                json.dumps(
                    {
                        "default_publisher": "spotify",
                        "playlist_prefix": "Discogs - ",
                        "playlist_suffix": "",
                        "release_playlists_prefix": "discogs - ",
                        "release_playlists_suffix": "",
                    }
                ),
                encoding="utf-8",
            )
            args = release_playlist.parse_args(
                [
                    "--from-config",
                    "--publisher",
                    "spotify",
                    "--config",
                    str(config_path),
                    "--publisher-config",
                    str(publisher_config_path),
                    "--workflow-config",
                    str(directory / "workflow.json"),
                    "--output-dir",
                    str(directory / "release-playlists"),
                    "--report",
                    str(directory / "configured.txt"),
                ]
            )

            with (
                patch("discogs_release_playlist.tracklists.make_cached_tracklist_lookup") as make_lookup,
                patch("discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args") as publish,
            ):
                with self.assertRaisesRegex(ValueError, "Spotify target"):
                    run_configured(args)

            make_lookup.assert_not_called()
            publish.assert_not_called()
            self.assertIn(
                "Spotify target",
                args.report.read_text(encoding="utf-8"),
            )

    def test_configured_release_targets_are_compared_case_insensitively(self):
        config = playlist_config_stub(
            (
                ConfiguredReleasePlaylist("House", ("111",)),
                ConfiguredReleasePlaylist("HOUSE", ("222",)),
            )
        )

        with self.assertRaisesRegex(ValueError, "same Spotify target"):
            release_playlist.validate_configured_publisher_targets(
                config,
                PublisherConfig("spotify", "", "", "Release ", ""),
            )

    def test_configured_summary_prints_local_and_publisher_outcomes(self):
        master_path = Path("collection/playlists/release-playlists/House/House.csv")
        local_summary = configured.ConfiguredReleasePlaylistsSummary(
            config_path=Path("config/custom-playlist-map.json"),
            output_directory=Path("collection/playlists/release-playlists"),
            report_path=Path("reports/configured.txt"),
            split_report_path=Path("reports/configured-splits.txt"),
            playlists=(
                configured.ConfiguredReleasePlaylistOutput(
                    playlist_name="House",
                    release_ids=("111",),
                    folder_path=master_path.parent,
                    master_path=master_path,
                    metadata_path=master_path.parent / ".discogs-release-playlist.json",
                    track_row_count=3,
                    split_summary=splitter.PlaylistSplitSummary(
                        playlist_folder_path=master_path.parent,
                        master_path=master_path,
                        written_split_paths=(),
                        regenerated_split_paths=(),
                        preserved_split_paths=(),
                        warnings=(),
                    ),
                    release_change=exporter.PlaylistReleaseChange(
                        playlist_name="House",
                        path=master_path,
                        added_releases=(),
                        removed_releases=(),
                    ),
                ),
            ),
            deleted_folder_paths=(Path("collection/playlists/release-playlists/Old"),),
            ignored_folder_paths=(),
        )
        summary = release_playlist.ConfiguredReleasePlaylistRunSummary(
            local_summary=local_summary,
            publisher="spotify",
            publisher_summary=publish_summary_stub(),
        )
        stdout = io.StringIO()

        with patch("sys.stdout", stdout):
            release_playlist.print_release_playlist_mode_summary(summary)

        output = stdout.getvalue()
        self.assertIn("Config: config/custom-playlist-map.json", output)
        self.assertIn("Output directory: collection/playlists/release-playlists", output)
        self.assertIn("Report: reports/configured.txt", output)
        self.assertIn("Split report: reports/configured-splits.txt", output)
        self.assertIn("Publisher report: reports/spotify_playlist_publish_report.txt", output)
        self.assertIn("Current playlists: 1", output)
        self.assertIn("Deleted folders: 1", output)
        self.assertIn("Output track rows: 3", output)
        self.assertIn("Publisher: spotify", output)

    def test_configured_publisher_failure_returns_nonzero_from_shared_cli(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            local_summary = configured_summary_stub(
                output_directory=directory / "release-playlists",
                report_path=directory / "configured.txt",
                split_report_path=directory / "splits.txt",
                playlists=(
                    SimpleNamespace(
                        master_path=directory / "release-playlists" / "House" / "House.csv",
                        playlist_name="House",
                        track_row_count=1,
                    ),
                ),
            )
            stderr = io.StringIO()

            with (
                patch(
                    "shared.playlist_config.load_playlist_config",
                    return_value=playlist_config_stub(
                        (ConfiguredReleasePlaylist("House", ("111",)),)
                    ),
                ),
                patch(
                    "shared.workflow_config.load_or_create_workflow_config",
                    return_value=WorkflowConfig(500, True, True),
                ),
                patch(
                    "discogs_release_playlist.load_or_create_publisher_config",
                    return_value=PublisherConfig("spotify", "", "", "", ""),
                ),
                patch(
                    "discogs_release_playlist.tracklists.make_cached_tracklist_lookup",
                    return_value=lambda row: lookup_for(row["release_id"]),
                ),
                patch(
                    "configured_release_playlists.create_configured_release_playlists",
                    return_value=local_summary,
                ),
                patch(
                    "discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args",
                    side_effect=ValueError("configured remote failure"),
                ) as publish,
                patch("sys.stderr", stderr),
            ):
                exit_code = release_playlist.main(
                    [
                        "--from-config",
                        "--publisher",
                        "spotify",
                        "--output-dir",
                        str(local_summary.output_directory),
                        "--report",
                        str(local_summary.report_path),
                        "--split-report",
                        str(local_summary.split_report_path),
                    ]
                )

            self.assertEqual(exit_code, 1)
            publish.assert_called_once()
            self.assertIn("configured remote failure", stderr.getvalue())

    def test_create_release_playlist_dedupes_ids_and_writes_isolated_master_without_collection_master(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"

            summary = release_playlist.create_release_playlist(
                playlist_name="Friday Picks",
                release_ids=("111", "222", "111"),
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookup_for(row["release_id"]),
            )

            master_path = output_directory / "Friday Picks" / "Friday Picks.csv"
            rows = read_csv_file(master_path)

            self.assertEqual(summary.release_ids, ("111", "222"))
            self.assertEqual(summary.duplicate_release_ids, ("111",))
            self.assertEqual(summary.master_path, master_path)
            self.assertEqual([row["Release Id"] for row in rows], ["111", "222"])
            self.assertFalse((directory / "collection" / "enriched-collection.csv").exists())
            self.assertIn("Duplicate release IDs skipped: 1", report_path.read_text(encoding="utf-8"))

    def test_create_release_playlist_reuses_playlist_name_by_overwriting_existing_master(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"

            release_playlist.create_release_playlist(
                playlist_name="Friday Picks",
                release_ids=("111",),
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookup_for(row["release_id"]),
            )
            release_playlist.create_release_playlist(
                playlist_name="Friday Picks",
                release_ids=("222",),
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookup_for(row["release_id"]),
            )

            rows = read_csv_file(output_directory / "Friday Picks" / "Friday Picks.csv")
            self.assertEqual([row["Release Id"] for row in rows], ["222"])

    def test_create_release_playlist_rejects_path_control_playlist_name_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"

            with self.assertRaisesRegex(ValueError, "playlist name resolves outside"):
                release_playlist.create_release_playlist(
                    playlist_name="..",
                    release_ids=("111",),
                    output_directory=output_directory,
                    report_path=report_path,
                    lookup_tracklist=lambda row: lookup_for(row["release_id"]),
                )

            self.assertFalse((directory / "collection" / "playlists" / "...csv").exists())
            self.assertFalse(report_path.exists())

    def test_create_release_playlist_rejects_distinct_names_that_share_sanitized_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"

            release_playlist.create_release_playlist(
                playlist_name="Friday/Picks",
                release_ids=("111",),
                output_directory=output_directory,
                report_path=report_path,
                lookup_tracklist=lambda row: lookup_for(row["release_id"]),
            )

            with self.assertRaisesRegex(ValueError, "already used by playlist name"):
                release_playlist.create_release_playlist(
                    playlist_name="Friday:Picks",
                    release_ids=("222",),
                    output_directory=output_directory,
                    report_path=report_path,
                    lookup_tracklist=lambda row: lookup_for(row["release_id"]),
                )

            rows = read_csv_file(output_directory / "Friday_Picks" / "Friday_Picks.csv")
            self.assertEqual([row["Release Id"] for row in rows], ["111"])

    def test_run_release_playlist_uses_default_publisher_config_but_publishes_without_prefix_suffix(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"
            publisher_report_path = directory / "reports" / "spotify-publish.txt"
            publisher_config_path = directory / "config" / "publisher.json"
            publisher_config_path.parent.mkdir(parents=True)
            publisher_config_path.write_text(
                json.dumps(
                    {
                        "default_publisher": "spotify",
                        "playlist_prefix": "Discogs - ",
                        "playlist_suffix": "!",
                    }
                ),
                encoding="utf-8",
            )
            args = release_playlist.parse_args(
                [
                    "--name",
                    "Friday/Picks",
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--publisher-report",
                    str(publisher_report_path),
                    "--publisher-config",
                    str(publisher_config_path),
                    "--publishing-dry-run",
                    "--no-progress",
                    "111",
                ]
            )

            with (
                patch(
                    "discogs_release_playlist.tracklists.make_cached_tracklist_lookup",
                    return_value=lambda row: lookup_for(row["release_id"]),
                ),
                patch(
                    "discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args",
                    return_value=publish_summary_stub(),
                ) as publish,
            ):
                summary = release_playlist.run_release_playlist(args)

            expected_master_path = output_directory / "Friday_Picks" / "Friday_Picks.csv"
            publisher_args = publish.call_args.args[0]
            publisher_config = publish.call_args.kwargs["publisher_config"]
            self.assertEqual(summary.publisher, "spotify")
            self.assertEqual(publish.call_args.kwargs["playlist_master_paths"], (expected_master_path,))
            self.assertEqual(publish.call_args.kwargs["playlist_names_by_master_path"], {expected_master_path: "Friday/Picks"})
            self.assertEqual(publisher_config.playlist_prefix, "")
            self.assertEqual(publisher_config.playlist_suffix, "")
            self.assertEqual(publisher_args.report, publisher_report_path)
            self.assertEqual(publisher_args.publisher_sync_mode, "append")
            self.assertFalse(publisher_args.apply)
            self.assertFalse(publisher_args.refresh_match_cache)

    def test_run_release_playlist_forwards_refresh_match_cache_to_spotify_publisher(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"
            publisher_report_path = directory / "reports" / "spotify-publish.txt"
            args = release_playlist.parse_args(
                [
                    "--name",
                    "Friday Picks",
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--publisher",
                    "spotify",
                    "--publisher-report",
                    str(publisher_report_path),
                    "--publishing-dry-run",
                    "--refresh-match-cache",
                    "--no-progress",
                    "111",
                ]
            )

            with (
                patch(
                    "discogs_release_playlist.tracklists.make_cached_tracklist_lookup",
                    return_value=lambda row: lookup_for(row["release_id"]),
                ),
                patch(
                    "discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args",
                    return_value=publish_summary_stub(),
                ) as publish,
            ):
                summary = release_playlist.run_release_playlist(args)

            self.assertEqual(summary.publisher, "spotify")
            self.assertTrue(publish.call_args.args[0].refresh_match_cache)

    def test_run_release_playlist_forwards_max_new_searches_per_run_to_spotify_publisher(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"
            publisher_report_path = directory / "reports" / "spotify-publish.txt"
            args = release_playlist.parse_args(
                [
                    "--name",
                    "Friday Picks",
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--publisher",
                    "spotify",
                    "--publisher-report",
                    str(publisher_report_path),
                    "--publishing-dry-run",
                    "--max-new-searches-per-run",
                    "25",
                    "--no-progress",
                    "111",
                ]
            )

            with (
                patch(
                    "discogs_release_playlist.tracklists.make_cached_tracklist_lookup",
                    return_value=lambda row: lookup_for(row["release_id"]),
                ),
                patch(
                    "discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args",
                    return_value=publish_summary_stub(),
                ) as publish,
            ):
                summary = release_playlist.run_release_playlist(args)

            self.assertEqual(summary.publisher, "spotify")
            self.assertEqual(publish.call_args.args[0].max_new_searches_per_run, 25)

    def test_publisher_flag_overrides_default_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"
            publisher_config_path = directory / "config" / "publisher.json"
            publisher_config_path.parent.mkdir(parents=True)
            publisher_config_path.write_text(
                json.dumps(
                    {
                        "default_publisher": "spotify",
                        "playlist_prefix": "Discogs - ",
                        "playlist_suffix": "",
                    }
                ),
                encoding="utf-8",
            )
            args = release_playlist.parse_args(
                [
                    "--name",
                    "Friday Picks",
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--publisher-config",
                    str(publisher_config_path),
                    "--publisher",
                    "none",
                    "--no-progress",
                    "111",
                ]
            )

            with (
                patch(
                    "discogs_release_playlist.tracklists.make_cached_tracklist_lookup",
                    return_value=lambda row: lookup_for(row["release_id"]),
                ),
                patch("discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args") as publish,
            ):
                summary = release_playlist.run_release_playlist(args)

            self.assertEqual(summary.publisher, "none")
            publish.assert_not_called()

    def test_invalid_default_publisher_config_stops_before_tracklist_lookup_and_output_writes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "collection" / "playlists" / "on-the-fly"
            report_path = directory / "reports" / "release-playlist.txt"
            publisher_config_path = directory / "config" / "publisher.json"
            publisher_config_path.parent.mkdir(parents=True)
            publisher_config_path.write_text('{"default_publisher": "apple"}', encoding="utf-8")
            args = release_playlist.parse_args(
                [
                    "--name",
                    "Friday Picks",
                    "--output-dir",
                    str(output_directory),
                    "--report",
                    str(report_path),
                    "--publisher-config",
                    str(publisher_config_path),
                    "--no-progress",
                    "111",
                ]
            )

            with patch("discogs_release_playlist.tracklists.make_cached_tracklist_lookup") as make_lookup:
                with self.assertRaisesRegex(ValueError, "default_publisher"):
                    release_playlist.run_release_playlist(args)

            make_lookup.assert_not_called()
            self.assertFalse(output_directory.exists())
            self.assertFalse(report_path.exists())


if __name__ == "__main__":
    unittest.main()
