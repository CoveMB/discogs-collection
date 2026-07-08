import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_make_playlists as make_playlists  # noqa: E402
import discogs_release_playlist as release_playlist  # noqa: E402
from publishers.spotify import publish_playlist as spotify_publisher  # noqa: E402
from shared.tunemymusic import TUNEMYMUSIC_COLUMNS  # noqa: E402


DISCOGS_EXPORT_COLUMNS = (
    "Catalog#",
    "Artist",
    "Title",
    "Label",
    "Format",
    "Rating",
    "Released",
    "release_id",
    "CollectionFolder",
    "Date Added",
    "Collection Media Condition",
    "Collection Sleeve Condition",
    "Collection Notes",
)
FAST_DISCOGS_HEADERS = {
    "x-discogs-ratelimit": "1000000",
    "x-discogs-ratelimit-remaining": "999999",
}


class FakeDiscogsResponse:
    def __init__(self, payload: object):
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = dict(FAST_DISCOGS_HEADERS)

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception, _traceback):
        return False

    def read(self) -> bytes:
        return self.body


class FastDiscogsRateLimiter:
    def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003 - test replacement for production constructor.
        pass

    def wait_before_request(self) -> None:
        pass

    def update_from_headers(self, _headers: object) -> None:
        pass

    def sleep_for_retry_after(self, _retry_after_seconds: float) -> None:
        pass


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


def release_ids_from_csv(path: Path, column_name: str) -> list[str]:
    return [row[column_name] for row in read_csv_rows(path)]


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_csv_rows(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def discogs_export_row(release_id: str, **overrides: str) -> dict[str, str]:
    release_id_text = str(release_id)
    date_day = int(release_id_text) % 20 + 1 if release_id_text.isdigit() else 1
    row = {
        "Catalog#": f"CAT-{release_id_text}" if release_id_text else "",
        "Artist": f"Artist {release_id_text}" if release_id_text else "Missing Artist",
        "Title": f"Album {release_id_text}" if release_id_text else "Missing Album",
        "Label": "Test Label",
        "Format": "Vinyl",
        "Rating": "0",
        "Released": "2026",
        "release_id": release_id_text,
        "CollectionFolder": "Uncategorized",
        "Date Added": f"2026-06-{date_day:02d}",
        "Collection Media Condition": "",
        "Collection Sleeve Condition": "",
        "Collection Notes": "",
    }
    row.update(overrides)
    return row


def write_discogs_export_rows(path: Path, rows: list[dict[str, str]]) -> None:
    write_csv_rows(path, DISCOGS_EXPORT_COLUMNS, rows)


def write_discogs_export(path: Path, release_ids: tuple[str, ...]) -> None:
    write_discogs_export_rows(path, [discogs_export_row(release_id) for release_id in release_ids])


def write_tunemymusic_master(path: Path, release_ids: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TUNEMYMUSIC_COLUMNS)
        writer.writeheader()
        for release_id in release_ids:
            writer.writerow(
                {
                    "Release Id": release_id,
                    "Album Name": f"Album {release_id}",
                    "Track Number": "1",
                    "Track Name": f"Track {release_id}",
                    "Artist Name": f"Artist {release_id}",
                    "Spotify Search Query": f"Artist {release_id} Track {release_id} Album {release_id}",
                }
            )


def discogs_release_payload(
    release_id: str,
    styles: list[str] | None = None,
    genres: list[str] | None = None,
    tracklist: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    default_tracklist = [
        {
            "position": "A1",
            "type_": "track",
            "title": f"Track {release_id}",
        }
    ]
    return {
        "title": f"Album {release_id}",
        "year": 2026,
        "styles": ["House"] if styles is None else styles,
        "genres": ["Electronic"] if genres is None else genres,
        "master_id": 0,
        "artists": [{"name": f"Artist {release_id}"}],
        "tracklist": default_tracklist if tracklist is None else tracklist,
    }


def discogs_track(release_id: str, position: str, title: str | None = None) -> dict[str, object]:
    return {
        "position": position,
        "type_": "track",
        "title": title or f"Track {release_id} {position}",
    }


def fake_urlopen(
    payloads_by_release_id: dict[str, dict[str, object]],
    requested_urls: list[str],
    unexpected_urls: list[str],
    blocked_release_ids: set[str] | None = None,
):
    blocked_release_ids = blocked_release_ids or set()

    def open_request(request, timeout=0):  # noqa: ARG001 - matches urllib.request.urlopen shape.
        url = request.full_url if hasattr(request, "full_url") else str(request)
        requested_urls.append(url)
        release_id = url.rstrip("/").rsplit("/", 1)[-1]
        user_agent = request.get_header("User-agent", "") if hasattr(request, "get_header") else ""
        if release_id in blocked_release_ids and user_agent.startswith("DiscogsPlaylistExporter/"):
            raise AssertionError(f"release {release_id} should have been served from cache, not fetched from Discogs")
        payload = payloads_by_release_id.get(release_id)
        if payload is None:
            unexpected_urls.append(url)
            payload = {}
        return FakeDiscogsResponse(payload)

    return open_request


@contextmanager
def patched_discogs(
    payloads_by_release_id: dict[str, dict[str, object]],
    requested_urls: list[str],
    unexpected_urls: list[str],
    blocked_release_ids: set[str] | None = None,
):
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "shared.discogs_api.urlopen",
                side_effect=fake_urlopen(
                    payloads_by_release_id,
                    requested_urls,
                    unexpected_urls,
                    blocked_release_ids=blocked_release_ids,
                ),
            )
        )
        stack.enter_context(patch("shared.discogs_api.DiscogsRateLimiter", FastDiscogsRateLimiter))
        stack.enter_context(patch("discogs_tracklists.DiscogsRateLimiter", FastDiscogsRateLimiter))
        yield


def spotify_publish_summary_stub(report_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        report_path=report_path,
        track_count=2,
        cache_hit_count=0,
        search_count=0,
        matched_count=0,
        already_present_count=0,
        would_add_count=0,
        added_count=0,
        would_include_count=0,
        included_count=0,
        duplicate_in_source_count=0,
        ambiguous_count=0,
        unmatched_count=0,
        error_count=0,
    )


def cache_record_ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return sorted(payload["records"])


def make_collection_args(
    export_path: Path,
    master_path: Path,
    playlist_config_path: Path,
    workflow_config_path: Path,
    playlist_output_dir: Path,
    enrichment_cache_path: Path,
    tracklist_cache_path: Path,
    reports_dir: Path,
    run_name: str,
    publisher: str | None = "none",
    publisher_config_path: Path | None = None,
    publishing_dry_run: bool = False,
    refresh_existing: bool = False,
    max_rows: int = 10,
) -> list[str]:
    arguments = [
        "--export",
        str(export_path),
        "--master",
        str(master_path),
        "--config",
        str(playlist_config_path),
        "--workflow-config",
        str(workflow_config_path),
        "--playlist-output-dir",
        str(playlist_output_dir),
        "--enrichment-cache",
        str(enrichment_cache_path),
        "--tracklist-cache",
        str(tracklist_cache_path),
        "--enrichment-report",
        str(reports_dir / f"{run_name}-enrichment.txt"),
        "--mapping-report",
        str(reports_dir / f"{run_name}-mapping.txt"),
        "--playlist-report",
        str(reports_dir / f"{run_name}-playlists.txt"),
        "--split-report",
        str(reports_dir / f"{run_name}-splits.txt"),
        "--no-seen-terms",
        "--no-progress",
        "--timeout-seconds",
        "1",
        "--request-interval-seconds",
        "0",
        "--max-workers",
        "1",
        "--max-rows",
        str(max_rows),
    ]
    if publisher is not None:
        arguments.extend(["--publisher", publisher])
    if publisher_config_path is not None:
        arguments.extend(["--publisher-config", str(publisher_config_path)])
    if publishing_dry_run:
        arguments.append("--publishing-dry-run")
    if refresh_existing:
        arguments.append("--refresh-existing")
    return arguments


class DiscogsReleasePlaylistE2ETests(unittest.TestCase):
    def test_collection_workflow_handles_mixed_rows_reports_and_outputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            collection_dir = directory / "collection"
            export_dir = directory / "exports"
            reports_dir = directory / "reports"
            config_dir = directory / "config"
            master_path = collection_dir / "enriched-collection.csv"
            playlist_output_dir = collection_dir / "playlists"
            enrichment_cache_path = collection_dir / "cache" / "processing.cache.json"
            tracklist_cache_path = collection_dir / "cache" / "playlist-tracks.cache.json"
            playlist_config_path = config_dir / "playlist-map.json"
            workflow_config_path = config_dir / "workflow.json"
            requested_urls: list[str] = []
            unexpected_urls: list[str] = []
            payloads = {
                "101": discogs_release_payload(
                    "101",
                    styles=["House"],
                    tracklist=[
                        discogs_track("101", "A1"),
                        discogs_track("101", "A2"),
                    ],
                ),
                "102": discogs_release_payload("102", styles=["House", "Breakbeat"]),
                "103": discogs_release_payload("103", styles=[], genres=["Jazz"]),
                "104": discogs_release_payload("104", styles=["House"], tracklist=[]),
            }

            write_json(
                playlist_config_path,
                {
                    "excluded_terms": ["Electronic"],
                    "playlists": {
                        "House": ["House"],
                        "Breakbeat": ["Breakbeat"],
                        "Jazz": ["Jazz"],
                    },
                },
            )
            write_json(
                workflow_config_path,
                {
                    "max_rows_per_split": 2,
                    "keep_release_tracks_together": True,
                    "create_new_split_files_for_new_releases": False,
                },
            )
            export_path = export_dir / "mixed.csv"
            write_discogs_export_rows(
                export_path,
                [
                    discogs_export_row("101"),
                    discogs_export_row("102"),
                    discogs_export_row("103"),
                    discogs_export_row("104"),
                    discogs_export_row("102", Artist="Duplicate Artist", Title="Duplicate Album"),
                    discogs_export_row("", Artist="Missing ID Artist", Title="Missing ID Album"),
                ],
            )

            with (
                patched_discogs(payloads, requested_urls, unexpected_urls),
                patch.object(spotify_publisher, "main", return_value=0) as publish,
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                exit_code = make_playlists.main(
                    make_collection_args(
                        export_path=export_path,
                        master_path=master_path,
                        playlist_config_path=playlist_config_path,
                        workflow_config_path=workflow_config_path,
                        playlist_output_dir=playlist_output_dir,
                        enrichment_cache_path=enrichment_cache_path,
                        tracklist_cache_path=tracklist_cache_path,
                        reports_dir=reports_dir,
                        run_name="mixed",
                        max_rows=2,
                    )
                )

            self.assertEqual(exit_code, 0)
            publish.assert_not_called()
            self.assertEqual(release_ids_from_csv(master_path, "release_id"), ["101", "102", "103", "104"])
            playlists_by_release_id = {
                row["release_id"]: row["Playlists"]
                for row in read_csv_rows(master_path)
            }
            self.assertEqual(playlists_by_release_id["101"], "House")
            self.assertEqual(playlists_by_release_id["102"], "House, Breakbeat")
            self.assertEqual(playlists_by_release_id["103"], "Jazz")
            self.assertEqual(playlists_by_release_id["104"], "House")

            house_master_path = playlist_output_dir / "House" / "House.csv"
            self.assertEqual(release_ids_from_csv(house_master_path, "Release Id"), ["101", "101", "102", "104"])
            self.assertEqual(
                release_ids_from_csv(playlist_output_dir / "Breakbeat" / "Breakbeat.csv", "Release Id"),
                ["102"],
            )
            self.assertEqual(release_ids_from_csv(playlist_output_dir / "Jazz" / "Jazz.csv", "Release Id"), ["103"])
            self.assertEqual(release_ids_from_csv(house_master_path.parent / "splits" / "1-2.csv", "Release Id"), ["101", "101"])
            self.assertEqual(release_ids_from_csv(house_master_path.parent / "splits" / "3-4.csv", "Release Id"), ["102", "104"])
            self.assertEqual(cache_record_ids(enrichment_cache_path), ["101", "102", "103", "104"])
            self.assertEqual(cache_record_ids(tracklist_cache_path), ["101", "102", "103", "104"])
            self.assertIn("duplicate release_id 102", (reports_dir / "mixed-enrichment.txt").read_text(encoding="utf-8"))
            self.assertIn("missing release_id", (reports_dir / "mixed-enrichment.txt").read_text(encoding="utf-8"))
            self.assertIn("Release-level fallback rows: 1", (reports_dir / "mixed-playlists.txt").read_text(encoding="utf-8"))
            self.assertEqual(unexpected_urls, [])

    def test_collection_incremental_run_preserves_existing_stable_splits_and_writes_new_range(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            collection_dir = directory / "collection"
            export_dir = directory / "exports"
            reports_dir = directory / "reports"
            config_dir = directory / "config"
            master_path = collection_dir / "enriched-collection.csv"
            playlist_output_dir = collection_dir / "playlists"
            enrichment_cache_path = collection_dir / "cache" / "processing.cache.json"
            tracklist_cache_path = collection_dir / "cache" / "playlist-tracks.cache.json"
            playlist_config_path = config_dir / "playlist-map.json"
            workflow_config_path = config_dir / "workflow.json"
            requested_urls: list[str] = []
            unexpected_urls: list[str] = []
            payloads = {
                release_id: discogs_release_payload(release_id)
                for release_id in ("201", "202", "203", "204", "205")
            }

            write_json(
                playlist_config_path,
                {
                    "excluded_terms": ["Electronic"],
                    "playlists": {"House": ["House"]},
                },
            )
            write_json(
                workflow_config_path,
                {
                    "max_rows_per_split": 2,
                    "keep_release_tracks_together": True,
                    "create_new_split_files_for_new_releases": True,
                },
            )

            first_export_path = export_dir / "first.csv"
            write_discogs_export(first_export_path, ("201", "202", "203"))
            with (
                patched_discogs(payloads, requested_urls, unexpected_urls),
                patch.object(spotify_publisher, "main", return_value=0),
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                first_exit_code = make_playlists.main(
                    make_collection_args(
                        export_path=first_export_path,
                        master_path=master_path,
                        playlist_config_path=playlist_config_path,
                        workflow_config_path=workflow_config_path,
                        playlist_output_dir=playlist_output_dir,
                        enrichment_cache_path=enrichment_cache_path,
                        tracklist_cache_path=tracklist_cache_path,
                        reports_dir=reports_dir,
                        run_name="incremental-first",
                        max_rows=2,
                    )
                )

            house_splits_dir = playlist_output_dir / "House" / "splits"
            split_1_2_path = house_splits_dir / "1-2.csv"
            split_3_3_path = house_splits_dir / "3-3.csv"
            self.assertEqual(first_exit_code, 0)
            self.assertEqual(release_ids_from_csv(split_1_2_path, "Release Id"), ["201", "202"])
            self.assertEqual(release_ids_from_csv(split_3_3_path, "Release Id"), ["203"])
            split_1_2_text = split_1_2_path.read_text(encoding="utf-8")
            split_3_3_text = split_3_3_path.read_text(encoding="utf-8")

            second_export_path = export_dir / "second.csv"
            write_discogs_export(second_export_path, ("204", "205"))
            with (
                patched_discogs(payloads, requested_urls, unexpected_urls),
                patch.object(spotify_publisher, "main", return_value=0),
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                second_exit_code = make_playlists.main(
                    make_collection_args(
                        export_path=second_export_path,
                        master_path=master_path,
                        playlist_config_path=playlist_config_path,
                        workflow_config_path=workflow_config_path,
                        playlist_output_dir=playlist_output_dir,
                        enrichment_cache_path=enrichment_cache_path,
                        tracklist_cache_path=tracklist_cache_path,
                        reports_dir=reports_dir,
                        run_name="incremental-second",
                        max_rows=2,
                    )
                )

            self.assertEqual(second_exit_code, 0)
            self.assertEqual(release_ids_from_csv(master_path, "release_id"), ["201", "202", "203", "204", "205"])
            self.assertEqual(split_1_2_path.read_text(encoding="utf-8"), split_1_2_text)
            self.assertEqual(split_3_3_path.read_text(encoding="utf-8"), split_3_3_text)
            self.assertEqual(release_ids_from_csv(house_splits_dir / "4-5.csv", "Release Id"), ["204", "205"])
            split_report_text = (reports_dir / "incremental-second-splits.txt").read_text(encoding="utf-8")
            self.assertIn("Split CSVs written: 1", split_report_text)
            self.assertIn("Split CSVs preserved: 2", split_report_text)
            self.assertEqual(unexpected_urls, [])

    def test_refresh_existing_replaces_metadata_and_remaps_downstream_playlist_outputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            collection_dir = directory / "collection"
            export_dir = directory / "exports"
            reports_dir = directory / "reports"
            config_dir = directory / "config"
            master_path = collection_dir / "enriched-collection.csv"
            playlist_output_dir = collection_dir / "playlists"
            enrichment_cache_path = collection_dir / "cache" / "fresh-processing.cache.json"
            tracklist_cache_path = collection_dir / "cache" / "playlist-tracks.cache.json"
            playlist_config_path = config_dir / "playlist-map.json"
            workflow_config_path = config_dir / "workflow.json"
            requested_urls: list[str] = []
            unexpected_urls: list[str] = []
            payloads = {
                "301": discogs_release_payload("301", styles=["Breakbeat"]),
            }

            write_csv_rows(
                master_path,
                ("release_id", "Artist", "Title", "Style", "Genre", "Playlists"),
                [
                    {
                        "release_id": "301",
                        "Artist": "Artist 301",
                        "Title": "Album 301",
                        "Style": "House",
                        "Genre": "Electronic",
                        "Playlists": "House",
                    }
                ],
            )
            write_tunemymusic_master(playlist_output_dir / "House" / "House.csv", ("301",))
            write_json(
                playlist_config_path,
                {
                    "excluded_terms": ["Electronic"],
                    "playlists": {
                        "House": ["House"],
                        "Breakbeat": ["Breakbeat"],
                    },
                },
            )
            write_json(
                workflow_config_path,
                {
                    "max_rows_per_split": 10,
                    "keep_release_tracks_together": True,
                    "create_new_split_files_for_new_releases": False,
                },
            )
            export_path = export_dir / "refresh.csv"
            write_discogs_export(export_path, ("301",))

            with (
                patched_discogs(payloads, requested_urls, unexpected_urls),
                patch.object(spotify_publisher, "main", return_value=0),
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                exit_code = make_playlists.main(
                    make_collection_args(
                        export_path=export_path,
                        master_path=master_path,
                        playlist_config_path=playlist_config_path,
                        workflow_config_path=workflow_config_path,
                        playlist_output_dir=playlist_output_dir,
                        enrichment_cache_path=enrichment_cache_path,
                        tracklist_cache_path=tracklist_cache_path,
                        reports_dir=reports_dir,
                        run_name="refresh",
                        refresh_existing=True,
                    )
                )

            master_rows = read_csv_rows(master_path)
            self.assertEqual(exit_code, 0)
            self.assertEqual(master_rows[0]["Style"], "Breakbeat")
            self.assertEqual(master_rows[0]["Playlists"], "Breakbeat")
            self.assertEqual(release_ids_from_csv(playlist_output_dir / "Breakbeat" / "Breakbeat.csv", "Release Id"), ["301"])
            self.assertEqual(release_ids_from_csv(playlist_output_dir / "House" / "House.csv", "Release Id"), ["301"])
            playlist_report_text = (reports_dir / "refresh-playlists.txt").read_text(encoding="utf-8")
            self.assertIn("House", playlist_report_text)
            self.assertIn("previous playlist file was not regenerated", playlist_report_text)
            self.assertEqual(cache_record_ids(enrichment_cache_path), ["301"])
            self.assertEqual(cache_record_ids(tracklist_cache_path), ["301"])
            self.assertEqual(unexpected_urls, [])

    def test_collection_workflow_hands_generated_outputs_to_configured_spotify_publisher(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            collection_dir = directory / "collection"
            export_dir = directory / "exports"
            reports_dir = directory / "reports"
            config_dir = directory / "config"
            master_path = collection_dir / "enriched-collection.csv"
            playlist_output_dir = collection_dir / "playlists"
            enrichment_cache_path = collection_dir / "cache" / "processing.cache.json"
            tracklist_cache_path = collection_dir / "cache" / "playlist-tracks.cache.json"
            playlist_config_path = config_dir / "playlist-map.json"
            workflow_config_path = config_dir / "workflow.json"
            publisher_config_path = config_dir / "publisher.json"
            requested_urls: list[str] = []
            unexpected_urls: list[str] = []
            payloads = {"401": discogs_release_payload("401")}

            write_json(
                playlist_config_path,
                {
                    "excluded_terms": ["Electronic"],
                    "playlists": {"House": ["House"]},
                },
            )
            write_json(
                workflow_config_path,
                {
                    "max_rows_per_split": 10,
                    "keep_release_tracks_together": True,
                    "create_new_split_files_for_new_releases": False,
                },
            )
            write_json(
                publisher_config_path,
                {
                    "default_publisher": "spotify",
                    "playlist_prefix": "Discogs - ",
                    "playlist_suffix": "",
                },
            )
            export_path = export_dir / "publisher.csv"
            write_discogs_export(export_path, ("401",))

            with (
                patched_discogs(payloads, requested_urls, unexpected_urls),
                patch.object(spotify_publisher, "main", return_value=0) as publish,
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                exit_code = make_playlists.main(
                    make_collection_args(
                        export_path=export_path,
                        master_path=master_path,
                        playlist_config_path=playlist_config_path,
                        workflow_config_path=workflow_config_path,
                        playlist_output_dir=playlist_output_dir,
                        enrichment_cache_path=enrichment_cache_path,
                        tracklist_cache_path=tracklist_cache_path,
                        reports_dir=reports_dir,
                        run_name="publisher",
                        publisher=None,
                        publisher_config_path=publisher_config_path,
                        publishing_dry_run=True,
                    )
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(release_ids_from_csv(playlist_output_dir / "House" / "House.csv", "Release Id"), ["401"])
            self.assertEqual(release_ids_from_csv(playlist_output_dir / "House" / "splits" / "1-1.csv", "Release Id"), ["401"])
            publish.assert_called_once()
            publisher_args = publish.call_args.args[0]
            self.assertIn("--playlist-output-dir", publisher_args)
            self.assertIn(str(playlist_output_dir), publisher_args)
            self.assertIn("--publisher-config", publisher_args)
            self.assertIn(str(publisher_config_path), publisher_args)
            self.assertIn("--publishing-dry-run", publisher_args)
            self.assertIn("--no-progress", publisher_args)
            self.assertEqual(unexpected_urls, [])

    def test_collection_workflow_stops_after_mapper_failure_without_export_split_or_publish(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            collection_dir = directory / "collection"
            export_dir = directory / "exports"
            reports_dir = directory / "reports"
            config_dir = directory / "config"
            master_path = collection_dir / "enriched-collection.csv"
            playlist_output_dir = collection_dir / "playlists"
            enrichment_cache_path = collection_dir / "cache" / "processing.cache.json"
            tracklist_cache_path = collection_dir / "cache" / "playlist-tracks.cache.json"
            playlist_config_path = config_dir / "playlist-map.json"
            workflow_config_path = config_dir / "workflow.json"
            requested_urls: list[str] = []
            unexpected_urls: list[str] = []
            payloads = {"501": discogs_release_payload("501")}

            write_json(
                playlist_config_path,
                {
                    "excluded_terms": ["Electronic"],
                    "playlist_prefix": "Discogs - ",
                    "playlists": {"House": ["House"]},
                },
            )
            write_json(
                workflow_config_path,
                {
                    "max_rows_per_split": 10,
                    "keep_release_tracks_together": True,
                    "create_new_split_files_for_new_releases": False,
                },
            )
            export_path = export_dir / "mapper-failure.csv"
            write_discogs_export(export_path, ("501",))

            with (
                patched_discogs(payloads, requested_urls, unexpected_urls),
                patch.object(spotify_publisher, "main", return_value=0) as publish,
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                exit_code = make_playlists.main(
                    make_collection_args(
                        export_path=export_path,
                        master_path=master_path,
                        playlist_config_path=playlist_config_path,
                        workflow_config_path=workflow_config_path,
                        playlist_output_dir=playlist_output_dir,
                        enrichment_cache_path=enrichment_cache_path,
                        tracklist_cache_path=tracklist_cache_path,
                        reports_dir=reports_dir,
                        run_name="mapper-failure",
                        publisher="spotify",
                        publishing_dry_run=True,
                    )
                )

            self.assertEqual(exit_code, 1)
            self.assertTrue(master_path.exists())
            self.assertEqual(release_ids_from_csv(master_path, "release_id"), ["501"])
            self.assertFalse((playlist_output_dir / "House" / "House.csv").exists())
            self.assertFalse((reports_dir / "mapper-failure-playlists.txt").exists())
            self.assertFalse((reports_dir / "mapper-failure-splits.txt").exists())
            publish.assert_not_called()
            stderr_text = stderr.getvalue()
            self.assertIn("unknown playlist config key: playlist_prefix", stderr_text)
            self.assertIn("Stopping before Discogs playlist exporter", stderr_text)
            self.assertEqual(cache_record_ids(enrichment_cache_path), ["501"])
            self.assertFalse(tracklist_cache_path.exists())
            self.assertEqual(unexpected_urls, [])

    def test_ad_hoc_cache_reuse_does_not_change_later_collection_order(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            collection_dir = directory / "collection"
            export_dir = directory / "exports"
            reports_dir = directory / "reports"
            config_dir = directory / "config"
            master_path = collection_dir / "enriched-collection.csv"
            playlist_output_dir = collection_dir / "playlists"
            on_the_fly_dir = playlist_output_dir / "on-the-fly"
            enrichment_cache_path = collection_dir / "cache" / "processing.cache.json"
            tracklist_cache_path = collection_dir / "cache" / "playlist-tracks.cache.json"
            playlist_config_path = config_dir / "playlist-map.json"
            workflow_config_path = config_dir / "workflow.json"
            ad_hoc_report_path = reports_dir / "ad-hoc-release-playlist.txt"
            ad_hoc_publisher_report_path = reports_dir / "ad-hoc-spotify-publish.txt"
            requested_urls: list[str] = []
            unexpected_urls: list[str] = []
            payloads = {
                release_id: discogs_release_payload(release_id)
                for release_id in ("111", "222", "333")
            }

            write_json(
                playlist_config_path,
                {
                    "excluded_terms": ["Electronic"],
                    "playlists": {"House": ["House"]},
                },
            )
            write_json(
                workflow_config_path,
                {
                    "max_rows_per_split": 10,
                    "keep_release_tracks_together": True,
                    "create_new_split_files_for_new_releases": False,
                },
            )

            first_export_path = export_dir / "collection-first.csv"
            write_discogs_export(first_export_path, ("111",))
            with (
                patched_discogs(payloads, requested_urls, unexpected_urls),
                patch.object(spotify_publisher, "main", return_value=0),
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                first_exit_code = make_playlists.main(
                    make_collection_args(
                        export_path=first_export_path,
                        master_path=master_path,
                        playlist_config_path=playlist_config_path,
                        workflow_config_path=workflow_config_path,
                        playlist_output_dir=playlist_output_dir,
                        enrichment_cache_path=enrichment_cache_path,
                        tracklist_cache_path=tracklist_cache_path,
                        reports_dir=reports_dir,
                        run_name="first",
                    )
                )

            self.assertEqual(first_exit_code, 0)
            self.assertEqual(release_ids_from_csv(master_path, "release_id"), ["111"])
            self.assertEqual(release_ids_from_csv(playlist_output_dir / "House" / "House.csv", "Release Id"), ["111"])
            self.assertEqual(cache_record_ids(enrichment_cache_path), ["111"])
            self.assertEqual(cache_record_ids(tracklist_cache_path), ["111"])

            with (
                patched_discogs(payloads, requested_urls, unexpected_urls),
                patch(
                    "discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args",
                    return_value=spotify_publish_summary_stub(ad_hoc_publisher_report_path),
                ) as publish,
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                ad_hoc_exit_code = release_playlist.main(
                    [
                        "--name",
                        "Friday Picks",
                        "--output-dir",
                        str(on_the_fly_dir),
                        "--report",
                        str(ad_hoc_report_path),
                        "--tracklist-cache",
                        str(tracklist_cache_path),
                        "--publisher",
                        "spotify",
                        "--publisher-report",
                        str(ad_hoc_publisher_report_path),
                        "--publishing-dry-run",
                        "--no-progress",
                        "--timeout-seconds",
                        "1",
                        "--request-interval-seconds",
                        "0",
                        "222",
                        "111",
                    ]
                )

            ad_hoc_master_path = on_the_fly_dir / "Friday Picks" / "Friday Picks.csv"
            self.assertEqual(ad_hoc_exit_code, 0)
            self.assertEqual(release_ids_from_csv(ad_hoc_master_path, "Release Id"), ["222", "111"])
            self.assertEqual(cache_record_ids(enrichment_cache_path), ["111"])
            self.assertEqual(cache_record_ids(tracklist_cache_path), ["111", "222"])
            self.assertEqual(publish.call_args.kwargs["playlist_master_paths"], (ad_hoc_master_path,))
            self.assertEqual(publish.call_args.kwargs["playlist_names_by_master_path"], {ad_hoc_master_path: "Friday Picks"})

            second_export_path = export_dir / "collection-second.csv"
            write_discogs_export(second_export_path, ("333", "222"))
            with (
                patched_discogs(payloads, requested_urls, unexpected_urls, blocked_release_ids={"222"}),
                patch.object(spotify_publisher, "main", return_value=0),
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                second_exit_code = make_playlists.main(
                    make_collection_args(
                        export_path=second_export_path,
                        master_path=master_path,
                        playlist_config_path=playlist_config_path,
                        workflow_config_path=workflow_config_path,
                        playlist_output_dir=playlist_output_dir,
                        enrichment_cache_path=enrichment_cache_path,
                        tracklist_cache_path=tracklist_cache_path,
                        reports_dir=reports_dir,
                        run_name="second",
                    )
                )

            house_master_path = playlist_output_dir / "House" / "House.csv"
            house_split_path = playlist_output_dir / "House" / "splits" / "1-3.csv"
            self.assertEqual(second_exit_code, 0)
            self.assertEqual(release_ids_from_csv(master_path, "release_id"), ["111", "333", "222"])
            self.assertEqual(release_ids_from_csv(house_master_path, "Release Id"), ["111", "333", "222"])
            self.assertEqual(release_ids_from_csv(house_split_path, "Release Id"), ["111", "333", "222"])
            self.assertEqual(release_ids_from_csv(ad_hoc_master_path, "Release Id"), ["222", "111"])
            self.assertFalse((on_the_fly_dir / "on-the-fly.csv").exists())
            self.assertEqual(cache_record_ids(enrichment_cache_path), ["111", "222", "333"])
            self.assertEqual(cache_record_ids(tracklist_cache_path), ["111", "222", "333"])

            from shared.playlist_selection import resolve_playlist_master_paths

            self.assertEqual(resolve_playlist_master_paths(playlist_output_dir), (house_master_path,))
            self.assertEqual(unexpected_urls, [])

    def test_ad_hoc_same_name_overwrites_dedupes_file_input_and_uses_exact_publisher_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            collection_dir = directory / "collection"
            reports_dir = directory / "reports"
            on_the_fly_dir = collection_dir / "playlists" / "on-the-fly"
            tracklist_cache_path = collection_dir / "cache" / "playlist-tracks.cache.json"
            first_report_path = reports_dir / "ad-hoc-first.txt"
            second_report_path = reports_dir / "ad-hoc-second.txt"
            first_publisher_report_path = reports_dir / "ad-hoc-first-spotify.txt"
            second_publisher_report_path = reports_dir / "ad-hoc-second-spotify.txt"
            release_ids_file = directory / "release-ids.txt"
            requested_urls: list[str] = []
            unexpected_urls: list[str] = []
            payloads = {
                release_id: discogs_release_payload(release_id)
                for release_id in ("111", "222", "333")
            }

            with (
                patched_discogs(payloads, requested_urls, unexpected_urls),
                patch(
                    "discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args",
                    return_value=spotify_publish_summary_stub(first_publisher_report_path),
                ) as first_publish,
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                first_exit_code = release_playlist.main(
                    [
                        "--name",
                        "Friday/Picks",
                        "--output-dir",
                        str(on_the_fly_dir),
                        "--report",
                        str(first_report_path),
                        "--tracklist-cache",
                        str(tracklist_cache_path),
                        "--publisher",
                        "spotify",
                        "--publisher-report",
                        str(first_publisher_report_path),
                        "--publishing-dry-run",
                        "--no-progress",
                        "--timeout-seconds",
                        "1",
                        "--request-interval-seconds",
                        "0",
                        "222",
                        "111",
                        "222",
                    ]
                )

            ad_hoc_master_path = on_the_fly_dir / "Friday_Picks" / "Friday_Picks.csv"
            self.assertEqual(first_exit_code, 0)
            self.assertEqual(release_ids_from_csv(ad_hoc_master_path, "Release Id"), ["222", "111"])
            self.assertIn("Duplicate release IDs skipped: 1", first_report_path.read_text(encoding="utf-8"))
            self.assertEqual(cache_record_ids(tracklist_cache_path), ["111", "222"])
            self.assertEqual(first_publish.call_args.kwargs["playlist_master_paths"], (ad_hoc_master_path,))
            self.assertEqual(
                first_publish.call_args.kwargs["playlist_names_by_master_path"],
                {ad_hoc_master_path: "Friday/Picks"},
            )

            release_ids_file.write_text("333, 333\n", encoding="utf-8")
            with (
                patched_discogs(payloads, requested_urls, unexpected_urls),
                patch(
                    "discogs_release_playlist.spotify_publisher.run_spotify_publish_from_args",
                    return_value=spotify_publish_summary_stub(second_publisher_report_path),
                ) as second_publish,
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                second_exit_code = release_playlist.main(
                    [
                        "--name",
                        "Friday/Picks",
                        "--release-ids-file",
                        str(release_ids_file),
                        "--output-dir",
                        str(on_the_fly_dir),
                        "--report",
                        str(second_report_path),
                        "--tracklist-cache",
                        str(tracklist_cache_path),
                        "--publisher",
                        "spotify",
                        "--publisher-report",
                        str(second_publisher_report_path),
                        "--publishing-dry-run",
                        "--no-progress",
                        "--timeout-seconds",
                        "1",
                        "--request-interval-seconds",
                        "0",
                    ]
                )

            self.assertEqual(second_exit_code, 0)
            self.assertEqual(release_ids_from_csv(ad_hoc_master_path, "Release Id"), ["333"])
            self.assertNotIn("222", release_ids_from_csv(ad_hoc_master_path, "Release Id"))
            self.assertNotIn("111", release_ids_from_csv(ad_hoc_master_path, "Release Id"))
            self.assertIn("Duplicate release IDs skipped: 1", second_report_path.read_text(encoding="utf-8"))
            self.assertEqual(cache_record_ids(tracklist_cache_path), ["111", "222", "333"])
            self.assertEqual(second_publish.call_args.kwargs["playlist_master_paths"], (ad_hoc_master_path,))
            self.assertEqual(
                second_publish.call_args.kwargs["playlist_names_by_master_path"],
                {ad_hoc_master_path: "Friday/Picks"},
            )
            self.assertEqual(unexpected_urls, [])

    def test_normal_playlist_selection_ignores_nested_on_the_fly_playlist_masters(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_output_dir = directory / "collection" / "playlists"
            normal_master_path = playlist_output_dir / "House" / "House.csv"
            on_the_fly_dir = playlist_output_dir / "on-the-fly"
            ad_hoc_master_path = on_the_fly_dir / "Friday Picks" / "Friday Picks.csv"
            write_tunemymusic_master(normal_master_path, ("111",))
            write_tunemymusic_master(ad_hoc_master_path, ("222",))

            from shared.playlist_selection import resolve_playlist_master_paths

            self.assertEqual(resolve_playlist_master_paths(playlist_output_dir), (normal_master_path,))
            self.assertEqual(resolve_playlist_master_paths(on_the_fly_dir), (ad_hoc_master_path,))


if __name__ == "__main__":
    unittest.main()
