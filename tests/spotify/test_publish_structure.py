import sys
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

        self.assertIs(publish_playlist.write_publish_report, publish_reports.write_publish_report)
        self.assertIs(publish_playlist.format_publish_decision, publish_reports.format_publish_decision)


if __name__ == "__main__":
    unittest.main()
