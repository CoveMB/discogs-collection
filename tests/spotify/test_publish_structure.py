import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify import publish_playlist  # noqa: E402


class SpotifyPublishStructureTests(unittest.TestCase):
    def test_publish_playlist_reexports_split_publish_models(self):
        from publishers.spotify import publish_types

        self.assertIs(publish_playlist.SpotifyPublishSummary, publish_types.SpotifyPublishSummary)
        self.assertIs(publish_playlist.PlaylistPublishDecision, publish_types.PlaylistPublishDecision)
        self.assertEqual(publish_playlist.APPEND_SYNC_MODE, publish_types.APPEND_SYNC_MODE)
        self.assertEqual(publish_playlist.WOULD_ADD, publish_types.WOULD_ADD)

    def test_publish_report_writer_lives_outside_cli_facade(self):
        from publishers.spotify import publish_reports

        self.assertIs(publish_playlist.write_dry_run_report, publish_reports.write_dry_run_report)
        self.assertIs(publish_playlist.format_publish_decision, publish_reports.format_publish_decision)
        summary = publish_playlist.SpotifyDryRunSummary(
            playlist_count=0,
            track_count=0,
            matched_count=0,
            ambiguous_count=0,
            unmatched_count=0,
            error_count=0,
            report_path=Path("report.txt"),
            decisions=(),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "publish.txt"

            publish_reports.write_dry_run_report(report_path, summary)

            self.assertIn("Spotify playlist dry-run report", report_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
