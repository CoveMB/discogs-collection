import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import dedupe_playlists  # noqa: E402


class DedupePlaylistsCliTests(unittest.TestCase):
    def assert_parse_args_exits(self, argv: list[str], expected_error: str) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
            self.assertRaises(SystemExit),
        ):
            dedupe_playlists.parse_args(argv)

        self.assertIn(expected_error, stderr.getvalue())

    def test_parse_args_defaults_to_spotify_dry_run(self):
        args = dedupe_playlists.parse_args([])

        self.assertEqual(args.provider, "spotify")
        self.assertFalse(args.apply)
        self.assertTrue(args.progress)

    def test_parse_args_accepts_apply_and_no_progress(self):
        args = dedupe_playlists.parse_args(["--provider", "spotify", "--apply", "--no-progress"])

        self.assertEqual(args.provider, "spotify")
        self.assertTrue(args.apply)
        self.assertFalse(args.progress)

    def test_parse_args_accepts_multiple_playlist_selectors(self):
        args = dedupe_playlists.parse_args(["--playlists", "House", "Techno"])

        self.assertEqual(args.playlists, ["House", "Techno"])

    def test_parse_args_rejects_removed_single_playlist_selector(self):
        self.assert_parse_args_exits(["--playlist", "House"], "unrecognized arguments: --playlist House")

    def test_parse_args_rejects_blank_playlist_selector(self):
        self.assert_parse_args_exits(
            ["--playlists", "House", "   "],
            "--playlists cannot contain blank selectors",
        )

    def test_parse_args_rejects_all_playlist_selector(self):
        self.assert_parse_args_exits(
            ["--playlists", "all"],
            "--playlists all is not allowed; omit --playlists to process every eligible playlist",
        )

    def test_main_prints_summary_from_spotify_provider(self):
        summary = type(
            "Summary",
            (),
            {
                "report_path": Path("reports/dedupe.txt"),
                "provider_playlist_count": 2,
                "eligible_playlist_count": 1,
                "skipped_playlist_count": 1,
                "track_count": 3,
                "duplicate_count": 1,
                "removed_count": 0,
            },
        )()

        with (
            patch.object(dedupe_playlists, "run_spotify_dedupe_from_args", return_value=summary) as run_provider,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            exit_code = dedupe_playlists.main(["--provider", "spotify"])

        self.assertEqual(exit_code, 0)
        run_provider.assert_called_once()
        output = stdout.getvalue()
        self.assertIn("Playlist dedupe report: reports/dedupe.txt", output)
        self.assertIn("Eligible playlists: 1", output)
        self.assertIn("Duplicates planned: 1", output)
        self.assertIn("Duplicates removed: 0", output)

    def test_main_reports_os_errors_with_shared_cli_boundary(self):
        with (
            patch.object(dedupe_playlists, "run_spotify_dedupe_from_args", side_effect=OSError("disk full")),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            exit_code = dedupe_playlists.main(["--provider", "spotify"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "Error: disk full\n")

    def test_run_spotify_dedupe_passes_playlist_selectors(self):
        summary = type(
            "Summary",
            (),
            {
                "provider_playlist_count": 1,
                "eligible_playlist_count": 1,
                "skipped_playlist_count": 0,
                "track_count": 0,
                "duplicate_count": 0,
                "removed_count": 0,
            },
        )()
        args = dedupe_playlists.parse_args(["--playlists", "House", "Techno", "--access-token", "access-token"])

        with (
            patch.object(dedupe_playlists, "load_spotify_settings", return_value=object()),
            patch.object(dedupe_playlists, "load_or_create_publisher_config", return_value=object()),
            patch.object(dedupe_playlists, "SpotifyClient", return_value=object()),
            patch.object(dedupe_playlists, "dedupe_spotify_managed_playlists", return_value=summary) as run_dedupe,
        ):
            dedupe_playlists.run_spotify_dedupe_from_args(args)

        self.assertEqual(run_dedupe.call_args.kwargs["playlist_selectors"], ["House", "Techno"])


if __name__ == "__main__":
    unittest.main()
