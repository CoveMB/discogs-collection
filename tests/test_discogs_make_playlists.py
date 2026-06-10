import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_make_playlists as maker  # noqa: E402


class DiscogsMakePlaylistsTests(unittest.TestCase):
    def test_runs_enricher_mapper_exporter_and_splitter_in_order_with_defaults(self):
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
            exit_code = maker.main([])

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
                        "--regenerate",
                        "Evening Listening",
                    ],
                ),
            ],
        )

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
