import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from shared.tunemymusic import (  # noqa: E402
    TUNEMYMUSIC_COLUMNS,
    missing_tunemymusic_columns,
    normalize_tunemymusic_rows,
)


class TuneMyMusicTests(unittest.TestCase):
    def test_reports_missing_schema_columns(self):
        fieldnames = ("Release Id", "Album Name", "Track Number")

        self.assertEqual(
            missing_tunemymusic_columns(fieldnames),
            ("Track Name", "Artist Name", "Spotify Search Query"),
        )

    def test_normalizes_rows_to_tunemymusic_schema(self):
        rows = (
            {
                "Release Id": 111,
                "Album Name": "Alpha Album",
                "Track Number": None,
                "Track Name": "Alpha One",
                "Artist Name": "Alpha Artist",
                "Spotify Search Query": "Alpha Artist Alpha One Alpha Album",
                "Ignored": "value",
            },
        )

        self.assertEqual(
            normalize_tunemymusic_rows(rows),
            (
                {
                    "Release Id": "111",
                    "Album Name": "Alpha Album",
                    "Track Number": "",
                    "Track Name": "Alpha One",
                    "Artist Name": "Alpha Artist",
                    "Spotify Search Query": "Alpha Artist Alpha One Alpha Album",
                },
            ),
        )
        self.assertEqual(tuple(normalize_tunemymusic_rows(rows)[0]), TUNEMYMUSIC_COLUMNS)


if __name__ == "__main__":
    unittest.main()
