import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from shared.release_playlist_metadata import (  # noqa: E402
    AD_HOC_RELEASE_PLAYLIST_RECORD_TYPE,
    CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
    ReleasePlaylistMetadata,
    read_release_playlist_metadata,
    write_release_playlist_metadata,
)


class ReleasePlaylistMetadataTests(unittest.TestCase):
    def test_round_trips_expected_record_type(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / ".release-playlist.json"
            expected = ReleasePlaylistMetadata(
                schema_version=1,
                record_type=CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
                playlist_name="Friday Picks",
            )

            write_release_playlist_metadata(path, expected)

            self.assertEqual(
                read_release_playlist_metadata(path, CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE),
                expected,
            )

    def test_rejects_metadata_with_wrong_record_type(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / ".release-playlist.json"
            write_release_playlist_metadata(
                path,
                ReleasePlaylistMetadata(
                    schema_version=1,
                    record_type=AD_HOC_RELEASE_PLAYLIST_RECORD_TYPE,
                    playlist_name="Friday Picks",
                ),
            )

            with self.assertRaisesRegex(ValueError, "unsupported release playlist metadata record type"):
                read_release_playlist_metadata(path, CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE)

    def test_rejects_malformed_json(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / ".release-playlist.json"
            path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "malformed release playlist metadata"):
                read_release_playlist_metadata(path, CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE)

    def test_rejects_blank_playlist_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / ".release-playlist.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
                        "playlist_name": "   ",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "missing playlist_name"):
                read_release_playlist_metadata(path, CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE)

    def test_rejects_metadata_symlink(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target_path = directory / "target.json"
            target_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE,
                        "playlist_name": "Friday Picks",
                    }
                ),
                encoding="utf-8",
            )
            path = directory / ".release-playlist.json"
            path.symlink_to(target_path)

            with self.assertRaisesRegex(ValueError, "metadata symlinks are not supported"):
                read_release_playlist_metadata(path, CONFIGURED_RELEASE_PLAYLIST_RECORD_TYPE)


if __name__ == "__main__":
    unittest.main()
