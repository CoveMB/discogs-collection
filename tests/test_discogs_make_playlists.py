import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_make_playlists as maker  # noqa: E402
from publishers.spotify import publish_playlist as spotify_publisher  # noqa: E402


class DiscogsMakePlaylistsTests(unittest.TestCase):
    def test_missing_publisher_config_defaults_to_non_publishing_workflow(self):
        calls: list[tuple[str, list[str]]] = []

        def record_step(name: str):
            def step(argv):
                calls.append((name, list(argv)))
                return 0

            return step

        with tempfile.TemporaryDirectory() as temporary_directory:
            publisher_config_path = Path(temporary_directory) / "publisher.json"
            with (
                patch.object(maker.enricher, "main", side_effect=record_step("enricher")),
                patch.object(maker.mapper, "main", side_effect=record_step("mapper")),
                patch.object(maker.exporter, "main", side_effect=record_step("exporter")),
                patch.object(maker.splitter, "main", side_effect=record_step("splitter")),
                patch.object(spotify_publisher, "main") as publisher_main,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = maker.main(["--publisher-config", str(publisher_config_path)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [
                ("enricher", []),
                ("mapper", []),
                ("exporter", []),
                ("splitter", []),
            ],
        )
        self.assertIn("Playlist publishing skipped because the resolved publisher is none.", stdout.getvalue())
        publisher_main.assert_not_called()

    def test_run_pipeline_includes_configured_publisher_step(self):
        calls: list[tuple[str, list[str]]] = []

        def record_step(name: str):
            def step(argv):
                calls.append((name, list(argv)))
                return 0

            return step

        args = maker.parse_args(
            [
                "--publisher",
                "spotify",
                "--playlist-output-dir",
                "collection/custom-playlists",
                "--publishing-dry-run",
            ]
        )

        with (
            patch.object(maker.enricher, "main", side_effect=record_step("enricher")),
            patch.object(maker.mapper, "main", side_effect=record_step("mapper")),
            patch.object(maker.exporter, "main", side_effect=record_step("exporter")),
            patch.object(maker.splitter, "main", side_effect=record_step("splitter")),
            patch.object(spotify_publisher, "main", side_effect=record_step("publisher")),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            args.publisher = maker.resolve_publisher(args)
            exit_code = maker.run_pipeline(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [
                ("enricher", []),
                ("mapper", []),
                ("exporter", ["--output-dir", "collection/custom-playlists"]),
                ("splitter", ["--output-dir", "collection/custom-playlists"]),
                (
                    "publisher",
                    [
                        "--playlist-output-dir",
                        "collection/custom-playlists",
                        "--publisher-config",
                        "config/publisher.json",
                        "--publishing-dry-run",
                    ],
                ),
            ],
        )

    def test_writes_debug_log_for_pipeline_steps_when_requested(self):
        calls: list[tuple[str, list[str]]] = []

        def record_step(name: str):
            def step(argv):
                calls.append((name, list(argv)))
                return 0

            return step

        with tempfile.TemporaryDirectory() as temporary_directory:
            debug_log_path = Path(temporary_directory) / "debug" / "make-playlists.log"
            with (
                patch.object(maker.enricher, "main", side_effect=record_step("enricher")),
                patch.object(maker.mapper, "main", side_effect=record_step("mapper")),
                patch.object(maker.exporter, "main", side_effect=record_step("exporter")),
                patch.object(maker.splitter, "main", side_effect=record_step("splitter")),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                exit_code = maker.main(
                    [
                        "--export",
                        "export/subset.csv",
                        "--playlist-output-dir",
                        "collection/playlists",
                        "--publisher",
                        "none",
                        "--debug-log",
                        str(debug_log_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual([name for name, _arguments in calls], ["enricher", "mapper", "exporter", "splitter"])
            debug_text = debug_log_path.read_text(encoding="utf-8")
            self.assertIn("start discogs_make_playlists", debug_text)
            self.assertIn("resolved_publisher value=none", debug_text)
            self.assertIn("path export=export/subset.csv", debug_text)
            self.assertIn("pipeline_steps count=5", debug_text)
            self.assertIn("step_start index=1 total=5 label=Discogs style enricher", debug_text)
            self.assertIn("step_end index=4 total=5 label=Discogs playlist splitter exit_code=0", debug_text)
            self.assertIn("step_start index=5 total=5 label=Playlist publisher", debug_text)
            self.assertIn("step_end index=5 total=5 label=Playlist publisher exit_code=0", debug_text)
            self.assertIn("completed exit_code=0", debug_text)

    def test_forwards_shared_paths_and_options_to_the_matching_steps(self):
        calls: list[tuple[str, list[str]]] = []

        def record_step(name: str):
            def step(argv):
                calls.append((name, list(argv)))
                return 0

            return step

        with (
            patch.object(maker.enricher, "main", side_effect=record_step("enricher")),
            patch.object(maker.mapper, "main", side_effect=record_step("mapper")),
            patch.object(maker.exporter, "main", side_effect=record_step("exporter")),
            patch.object(maker.splitter, "main", side_effect=record_step("splitter")),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            exit_code = maker.main(
                [
                    "--export",
                    "export/latest.csv",
                    "--master",
                    "collection/custom-master.csv",
                    "--config",
                    "config/custom-playlists.json",
                    "--workflow-config",
                    "config/workflow.json",
                    "--playlist-output-dir",
                    "collection/custom-playlists",
                    "--enrichment-cache",
                    "collection/cache/enrichment.json",
                    "--tracklist-cache",
                    "collection/cache/tracks.json",
                    "--enrichment-report",
                    "reports/enrichment.txt",
                    "--mapping-report",
                    "reports/mapping.txt",
                    "--playlist-report",
                    "reports/playlists.txt",
                    "--split-report",
                    "reports/splits.txt",
                    "--regenerate-splits",
                    "Evening Listening",
                    "--refresh-existing",
                    "--no-seen-terms",
                    "--no-progress",
                    "--timeout-seconds",
                    "10",
                    "--request-interval-seconds",
                    "0.25",
                    "--max-workers",
                    "2",
                    "--max-rows",
                    "250",
                    "--publisher-config",
                    "config/publisher.json",
                    "--publisher",
                    "none",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [
                (
                    "enricher",
                    [
                        "--export",
                        "export/latest.csv",
                        "--master",
                        "collection/custom-master.csv",
                        "--cache",
                        "collection/cache/enrichment.json",
                        "--report",
                        "reports/enrichment.txt",
                        "--refresh-existing",
                        "--no-seen-terms",
                        "--no-progress",
                        "--timeout-seconds",
                        "10",
                        "--request-interval-seconds",
                        "0.25",
                        "--max-workers",
                        "2",
                    ],
                ),
                (
                    "mapper",
                    [
                        "--input",
                        "collection/custom-master.csv",
                        "--output",
                        "collection/custom-master.csv",
                        "--config",
                        "config/custom-playlists.json",
                        "--report",
                        "reports/mapping.txt",
                    ],
                ),
                (
                    "exporter",
                    [
                        "--input",
                        "collection/custom-master.csv",
                        "--output-dir",
                        "collection/custom-playlists",
                        "--cache",
                        "collection/cache/tracks.json",
                        "--report",
                        "reports/playlists.txt",
                        "--no-progress",
                        "--timeout-seconds",
                        "10",
                        "--request-interval-seconds",
                        "0.25",
                    ],
                ),
                (
                    "splitter",
                    [
                        "--output-dir",
                        "collection/custom-playlists",
                        "--report",
                        "reports/splits.txt",
                        "--workflow-config",
                        "config/workflow.json",
                        "--regenerate",
                        "Evening Listening",
                        "--max-rows",
                        "250",
                    ],
                ),
            ],
        )

    def test_resolves_default_publisher_from_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            publisher_config_path = Path(temporary_directory) / "publisher.json"
            publisher_config_path.write_text(
                json.dumps(
                    {
                        "default_publisher": "spotify",
                        "playlist_prefix": "",
                        "playlist_suffix": "",
                    }
                ),
                encoding="utf-8",
            )
            args = maker.parse_args(["--publisher-config", str(publisher_config_path)])

            publisher = maker.resolve_publisher(args)

        self.assertEqual(publisher, "spotify")

    def test_runs_configured_default_publisher_when_publisher_flag_is_omitted(self):
        def successful_step(_argv):
            return 0

        with tempfile.TemporaryDirectory() as temporary_directory:
            publisher_config_path = Path(temporary_directory) / "publisher.json"
            publisher_config_path.write_text(
                json.dumps(
                    {
                        "default_publisher": "spotify",
                        "playlist_prefix": "",
                        "playlist_suffix": "",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(maker.enricher, "main", side_effect=successful_step),
                patch.object(maker.mapper, "main", side_effect=successful_step),
                patch.object(maker.exporter, "main", side_effect=successful_step),
                patch.object(maker.splitter, "main", side_effect=successful_step),
                patch.object(spotify_publisher, "main", return_value=0) as publisher_main,
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                exit_code = maker.main(
                    [
                        "--publisher-config",
                        str(publisher_config_path),
                        "--playlist-output-dir",
                        "collection/custom-playlists",
                        "--no-progress",
                    ]
                )

        self.assertEqual(exit_code, 0)
        publisher_main.assert_called_once_with(
            [
                "--playlist-output-dir",
                "collection/custom-playlists",
                "--publisher-config",
                str(publisher_config_path),
                "--no-progress",
            ]
        )

    def test_passes_publishing_dry_run_to_configured_publisher(self):
        def successful_step(_argv):
            return 0

        with (
            patch.object(maker.enricher, "main", side_effect=successful_step),
            patch.object(maker.mapper, "main", side_effect=successful_step),
            patch.object(maker.exporter, "main", side_effect=successful_step),
            patch.object(maker.splitter, "main", side_effect=successful_step),
            patch.object(spotify_publisher, "main", return_value=0) as publisher_main,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            exit_code = maker.main(["--publisher", "spotify", "--publishing-dry-run"])

        self.assertEqual(exit_code, 0)
        publisher_main.assert_called_once_with(
            [
                "--publisher-config",
                "config/publisher.json",
                "--publishing-dry-run",
            ]
        )

    def test_logs_when_resolved_publisher_is_none(self):
        def successful_step(_argv):
            return 0

        with tempfile.TemporaryDirectory() as temporary_directory:
            publisher_config_path = Path(temporary_directory) / "publisher.json"
            publisher_config_path.write_text(
                json.dumps(
                    {
                        "default_publisher": "none",
                        "playlist_prefix": "",
                        "playlist_suffix": "",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(maker.enricher, "main", side_effect=successful_step),
                patch.object(maker.mapper, "main", side_effect=successful_step),
                patch.object(maker.exporter, "main", side_effect=successful_step),
                patch.object(maker.splitter, "main", side_effect=successful_step),
                patch.object(spotify_publisher, "main") as publisher_main,
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = maker.main(["--publisher-config", str(publisher_config_path)])

        output = stdout.getvalue()
        splitter_position = output.find("Running Discogs playlist splitter...")
        available_publishers = ", ".join(maker.available_playlist_publishers())
        publisher_notice_position = output.find(
            "Playlist publishing skipped because the resolved publisher is none. "
            f"Run with --publisher {available_publishers} to publish the playlist. "
        )
        self.assertEqual(exit_code, 0)
        self.assertGreaterEqual(splitter_position, 0)
        self.assertGreater(publisher_notice_position, splitter_position)
        publisher_main.assert_not_called()

    def test_stops_before_publisher_when_splitter_fails(self):
        with (
            patch.object(maker.enricher, "main", return_value=0) as enricher_main,
            patch.object(maker.mapper, "main", return_value=0) as mapper_main,
            patch.object(maker.exporter, "main", return_value=0) as exporter_main,
            patch.object(maker.splitter, "main", return_value=5) as splitter_main,
            patch.object(spotify_publisher, "main") as publisher_main,
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            exit_code = maker.main(["--publisher", "spotify"])

        self.assertEqual(exit_code, 5)
        enricher_main.assert_called_once_with([])
        mapper_main.assert_called_once_with([])
        exporter_main.assert_called_once_with([])
        splitter_main.assert_called_once_with([])
        publisher_main.assert_not_called()
        self.assertIn("Stopping before Spotify playlist publisher", stderr.getvalue())

    def test_available_playlist_publishers_are_derived_from_supported_publishers(self):
        self.assertEqual(
            maker.available_playlist_publishers(("spotify", "apple", "none")),
            ("spotify", "apple"),
        )

    def test_publisher_cli_override_accepts_none(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            publisher_config_path = Path(temporary_directory) / "publisher.json"
            publisher_config_path.write_text(
                json.dumps(
                    {
                        "default_publisher": "spotify",
                        "playlist_prefix": "Prefix ",
                        "playlist_suffix": " Suffix",
                    }
                ),
                encoding="utf-8",
            )
            args = maker.parse_args(
                [
                    "--publisher-config",
                    str(publisher_config_path),
                    "--publisher",
                    "none",
                ]
            )

            publisher = maker.resolve_publisher(args)

        self.assertEqual(publisher, "none")

    def test_invalid_publisher_config_stops_before_workflow_steps(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            publisher_config_path = Path(temporary_directory) / "publisher.json"
            publisher_config_path.write_text('{"default_publisher": "apple"}', encoding="utf-8")
            stderr = io.StringIO()

            with (
                patch.object(maker.enricher, "main") as enricher_main,
                patch.object(maker.mapper, "main") as mapper_main,
                patch.object(maker.exporter, "main") as exporter_main,
                patch.object(maker.splitter, "main") as splitter_main,
                patch("sys.stderr", stderr),
            ):
                exit_code = maker.main(["--publisher-config", str(publisher_config_path)])

        self.assertEqual(exit_code, 1)
        self.assertIn("default_publisher", stderr.getvalue())
        enricher_main.assert_not_called()
        mapper_main.assert_not_called()
        exporter_main.assert_not_called()
        splitter_main.assert_not_called()

    def test_stops_before_mapper_when_enricher_fails(self):
        with (
            patch.object(maker.enricher, "main", return_value=2) as enricher_main,
            patch.object(maker.mapper, "main") as mapper_main,
            patch.object(maker.exporter, "main") as exporter_main,
            patch.object(maker.splitter, "main") as splitter_main,
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            exit_code = maker.main([])

        self.assertEqual(exit_code, 2)
        enricher_main.assert_called_once_with([])
        mapper_main.assert_not_called()
        exporter_main.assert_not_called()
        splitter_main.assert_not_called()
        self.assertIn("Stopping before Discogs playlist mapper", stderr.getvalue())

    def test_stops_before_exporter_when_mapper_fails(self):
        with (
            patch.object(maker.enricher, "main", return_value=0) as enricher_main,
            patch.object(maker.mapper, "main", return_value=3) as mapper_main,
            patch.object(maker.exporter, "main") as exporter_main,
            patch.object(maker.splitter, "main") as splitter_main,
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            exit_code = maker.main([])

        self.assertEqual(exit_code, 3)
        enricher_main.assert_called_once_with([])
        mapper_main.assert_called_once_with([])
        exporter_main.assert_not_called()
        splitter_main.assert_not_called()
        self.assertIn("Stopping before Discogs playlist exporter", stderr.getvalue())

    def test_stops_before_splitter_when_exporter_fails(self):
        with (
            patch.object(maker.enricher, "main", return_value=0) as enricher_main,
            patch.object(maker.mapper, "main", return_value=0) as mapper_main,
            patch.object(maker.exporter, "main", return_value=4) as exporter_main,
            patch.object(maker.splitter, "main") as splitter_main,
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            exit_code = maker.main([])

        self.assertEqual(exit_code, 4)
        enricher_main.assert_called_once_with([])
        mapper_main.assert_called_once_with([])
        exporter_main.assert_called_once_with([])
        splitter_main.assert_not_called()
        self.assertIn("Stopping before Discogs playlist splitter", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
