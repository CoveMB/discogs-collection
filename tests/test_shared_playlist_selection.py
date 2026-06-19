import csv
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from discogs_playlist_exporter import TUNEMYMUSIC_COLUMNS  # noqa: E402
from shared.playlist_selection import resolve_playlist_master_paths, safe_playlist_filename  # noqa: E402


def playlist_row(release_id: str) -> dict[str, str]:
    return {
        "Release Id": release_id,
        "Album Name": f"Album {release_id}",
        "Track Number": "1",
        "Track Name": f"Track {release_id}",
        "Artist Name": f"Artist {release_id}",
        "Spotify Search Query": f"Artist {release_id} Track {release_id}",
    }


def write_master(path: Path, release_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=TUNEMYMUSIC_COLUMNS)
        writer.writeheader()
        writer.writerow(playlist_row(release_id))


class PlaylistSelectionTests(unittest.TestCase):
    def test_omitted_selectors_resolve_every_playlist_master_in_folder_order(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "playlists"
            write_master(output_directory / "House" / "House.csv", "111")
            write_master(output_directory / "Techno" / "Techno.csv", "222")
            (output_directory / "No Master").mkdir(parents=True)

            paths = resolve_playlist_master_paths(output_directory)

        self.assertEqual([path.name for path in paths], ["House.csv", "Techno.csv"])

    def test_resolves_display_name_folder_name_folder_path_and_master_path_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "playlists"
            breakbeat_folder = output_directory / safe_playlist_filename('Discogs: "Breakbeat"')
            house_folder = output_directory / "House"
            techno_folder = output_directory / "Techno"
            write_master(breakbeat_folder / f"{breakbeat_folder.name}.csv", "111")
            write_master(house_folder / "House.csv", "222")
            write_master(techno_folder / "Techno.csv", "333")

            paths = resolve_playlist_master_paths(
                output_directory,
                [
                    'Discogs: "Breakbeat"',
                    "House",
                    str(techno_folder),
                    str(techno_folder / "Techno.csv"),
                ],
            )

        self.assertEqual(
            [path.name for path in paths],
            [f"{breakbeat_folder.name}.csv", "House.csv", "Techno.csv"],
        )

    def test_all_selector_is_rejected_when_not_allowed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "playlists"
            write_master(output_directory / "House" / "House.csv", "111")

            with self.assertRaisesRegex(ValueError, "all.*not allowed"):
                resolve_playlist_master_paths(output_directory, ["all"], allow_all_selector=False)

    def test_all_selector_can_be_allowed_for_splitter_regenerate(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "playlists"
            write_master(output_directory / "House" / "House.csv", "111")

            paths = resolve_playlist_master_paths(output_directory, ["all"], allow_all_selector=True)

        self.assertEqual([path.name for path in paths], ["House.csv"])

    def test_missing_selector_reports_no_playlist_match(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory) / "playlists"
            output_directory.mkdir()

            with self.assertRaisesRegex(FileNotFoundError, "no playlist match found.*Missing"):
                resolve_playlist_master_paths(output_directory, ["Missing"])

    def test_rejects_playlist_path_that_escapes_output_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            outside_folder = directory / "outside"
            write_master(outside_folder / "outside.csv", "111")
            output_directory.mkdir()

            with self.assertRaisesRegex(ValueError, "outside playlist output directory"):
                resolve_playlist_master_paths(output_directory, [str(outside_folder)])

    def test_rejects_symlinked_playlist_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            outside_folder = directory / "outside"
            output_directory.mkdir()
            write_master(outside_folder / "Linked.csv", "111")
            try:
                (output_directory / "Linked").symlink_to(outside_folder, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlinks are not available: {error}")

            with self.assertRaisesRegex(ValueError, "symlink"):
                resolve_playlist_master_paths(output_directory)

    def test_rejects_exact_symlinked_playlist_selector(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_directory = directory / "playlists"
            outside_folder = directory / "outside"
            output_directory.mkdir()
            write_master(outside_folder / "Linked.csv", "111")
            try:
                (output_directory / "Linked").symlink_to(outside_folder, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlinks are not available: {error}")

            with self.assertRaisesRegex(ValueError, "symlink"):
                resolve_playlist_master_paths(output_directory, ["Linked"])


if __name__ == "__main__":
    unittest.main()
