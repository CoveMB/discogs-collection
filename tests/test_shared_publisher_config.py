import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from shared.publisher_config import (  # noqa: E402
    DEFAULT_PUBLISHER_CONFIG_PATH,
    PublisherConfig,
    load_or_create_publisher_config,
)


class PublisherConfigTests(unittest.TestCase):
    def test_missing_config_is_created_with_safe_defaults(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config" / "publisher.json"

            config = load_or_create_publisher_config(path)

            self.assertEqual(
                config,
                PublisherConfig(
                    default_publisher="none",
                    playlist_prefix="Discogs - ",
                    playlist_suffix="",
                ),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["default_publisher"], "none")
            self.assertEqual(payload["playlist_prefix"], "Discogs - ")
            self.assertEqual(DEFAULT_PUBLISHER_CONFIG_PATH, Path("config/publisher.json"))

    def test_loads_custom_valid_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "publisher.json"
            path.write_text(
                json.dumps(
                    {
                        "default_publisher": "none",
                        "playlist_prefix": "Discogs - ",
                        "playlist_suffix": " Archive",
                    }
                ),
                encoding="utf-8",
            )

            config = load_or_create_publisher_config(path)

        self.assertEqual(config.default_publisher, "none")
        self.assertEqual(config.playlist_prefix, "Discogs - ")
        self.assertEqual(config.playlist_suffix, " Archive")

    def test_rejects_unknown_publisher(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "publisher.json"
            path.write_text('{"default_publisher": "apple"}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "default_publisher"):
                load_or_create_publisher_config(path)

    def test_rejects_unknown_keys_and_non_string_affixes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "publisher.json"
            path.write_text('{"default_publisher": "spotify", "playlist_prefix": 123}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "playlist_prefix"):
                load_or_create_publisher_config(path)

            path.write_text('{"default_publisher": "spotify", "extra": true}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unknown publisher config key"):
                load_or_create_publisher_config(path)


if __name__ == "__main__":
    unittest.main()
