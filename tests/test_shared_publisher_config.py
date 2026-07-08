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
    NON_PUBLISHING_PUBLISHERS,
    PublisherConfig,
    load_or_create_publisher_config,
    publisher_local_name_from_target,
    publisher_playlist_name,
    publishing_publishers,
    validate_publisher_naming_is_safe,
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

    def test_filters_non_publishing_publishers(self):
        self.assertEqual(NON_PUBLISHING_PUBLISHERS, frozenset({"none"}))
        self.assertEqual(publishing_publishers(("spotify", "none")), ("spotify",))

    def test_builds_and_parses_publisher_playlist_names(self):
        config = PublisherConfig(default_publisher="spotify", playlist_prefix="Discogs - ", playlist_suffix=" Archive")

        self.assertEqual(publisher_playlist_name("House", config), "Discogs - House Archive")
        self.assertEqual(publisher_local_name_from_target("Discogs - House Archive", config), "House")
        self.assertIsNone(publisher_local_name_from_target("House", config))
        self.assertIsNone(publisher_local_name_from_target("Discogs -  Archive", config))

    def test_rejects_unsafe_managed_playlist_naming(self):
        config = PublisherConfig(default_publisher="spotify", playlist_prefix="", playlist_suffix="")

        with self.assertRaisesRegex(ValueError, "playlist_prefix or playlist_suffix"):
            validate_publisher_naming_is_safe(config)


if __name__ == "__main__":
    unittest.main()
