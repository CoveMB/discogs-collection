import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.env import load_spotify_settings


class SpotifyEnvTests(unittest.TestCase):
    def test_loads_spotify_settings_from_env_file_without_shell_exports(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            env_path = directory / ".env"
            env_path.write_text(
                'SPOTIFY_CLIENT_ID="client-id"\n'
                'SPOTIFY_REDIRECT_URI="http://127.0.0.1:8765/callback"\n',
                encoding="utf-8",
            )

            settings = load_spotify_settings(env_path)

            self.assertEqual(settings.client_id, "client-id")
            self.assertEqual(settings.redirect_uri, "http://127.0.0.1:8765/callback")
            self.assertEqual(settings.token_cache_path, Path("config/cache/spotify-token.cache.json"))

    def test_env_file_values_take_precedence_over_shell_environment(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            env_path = directory / ".env"
            env_path.write_text(
                'SPOTIFY_CLIENT_ID="file-client-id"\n'
                'SPOTIFY_REDIRECT_URI="http://127.0.0.1:8765/callback"\n',
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "SPOTIFY_CLIENT_ID": "shell-client-id",
                    "SPOTIFY_REDIRECT_URI": "http://127.0.0.1:9999/callback",
                },
            ):
                settings = load_spotify_settings(env_path)

            self.assertEqual(settings.client_id, "file-client-id")
            self.assertEqual(settings.redirect_uri, "http://127.0.0.1:8765/callback")

    def test_missing_required_spotify_env_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            env_path = Path(temporary_directory) / ".env"
            env_path.write_text("SPOTIFY_CLIENT_ID=\n", encoding="utf-8")

            with self.assertRaises(ValueError) as context:
                load_spotify_settings(env_path)

            self.assertIn("missing required Spotify env value: SPOTIFY_CLIENT_ID", str(context.exception))
            self.assertIn("SPOTIFY_REDIRECT_URI", str(context.exception))


if __name__ == "__main__":
    unittest.main()
