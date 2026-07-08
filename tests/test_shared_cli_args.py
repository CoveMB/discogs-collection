import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_make_playlists as maker  # noqa: E402
from publishers.spotify import publish_playlist  # noqa: E402
from shared import cli_args  # noqa: E402


class SharedCliArgsTests(unittest.TestCase):
    def test_append_cli_option_skips_none_and_stringifies_values(self):
        arguments: list[str] = []

        cli_args.append_cli_option(arguments, "--missing", None)
        cli_args.append_cli_option(arguments, "--count", 3)
        cli_args.append_cli_option(arguments, "--path", Path("collection/playlists"))

        self.assertEqual(arguments, ["--count", "3", "--path", "collection/playlists"])

    def test_workflow_modules_use_shared_cli_option_helper(self):
        self.assertIs(maker.append_option, cli_args.append_cli_option)
        self.assertIs(publish_playlist.append_cli_option, cli_args.append_cli_option)


if __name__ == "__main__":
    unittest.main()
