import csv
import io
import argparse
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_style_enricher as enricher  # noqa: E402
from discogs_style_enricher import (  # noqa: E402
    DEFAULT_MASTER_PATH,
    DiscogsRateLimiter,
    MetadataFieldLookup,
    ProgressReporter,
    ReleaseMetadataLookup,
    build_output_fieldnames,
    ensure_processed_export_target_available,
    find_single_csv_export,
    load_lookup_cache,
    main,
    merge_master_and_export_rows,
    parse_genres_from_api_payload,
    parse_styles_from_api_payload,
    parse_args,
    resolve_release_metadata,
    run_enrichment,
    save_lookup_cache,
    update_missing_metadata,
    validate_discogs_export_fieldnames,
)


def read_csv_text(csv_text):
    return list(csv.DictReader(io.StringIO(csv_text)))


STANDARD_DISCOGS_HEADER = (
    "Catalog#,Artist,Title,Label,Format,Rating,Released,release_id,"
    "CollectionFolder,Date Added,Collection Media Condition,"
    "Collection Sleeve Condition,Collection Notes\n"
)


class FieldnameTests(unittest.TestCase):
    def test_build_output_fieldnames_adds_missing_enrichment_columns_once(self):
        input_fields = ["Catalog#", "Artist", "Title", "Released", "release_id"]

        output_fields = build_output_fieldnames(input_fields)

        self.assertEqual(
            output_fields,
            [
                "Catalog#",
                "Artist",
                "Title",
                "Released",
                "Style",
                "Genre",
                "Style Notes",
                "Genre Notes",
                "Updated At",
                "release_id",
            ],
        )
        for column_name in ("Style", "Genre", "Style Notes", "Genre Notes", "Updated At"):
            self.assertEqual(output_fields.count(column_name), 1)
        self.assertNotIn("Style Source", output_fields)
        self.assertNotIn("Style Status", output_fields)

    def test_build_output_fieldnames_removes_source_and_status_columns(self):
        input_fields = [
            "Catalog#",
            "Artist",
            "Title",
            "Released",
            "Style",
            "release_id",
            "Style Source",
            "Style Status",
            "Style Notes",
            "Style Updated At",
        ]

        output_fields = build_output_fieldnames(input_fields)

        self.assertEqual(output_fields.count("Style"), 1)
        self.assertIn("Genre", output_fields)
        self.assertIn("Style Notes", output_fields)
        self.assertIn("Genre Notes", output_fields)
        self.assertIn("Updated At", output_fields)
        self.assertNotIn("Style Updated At", output_fields)
        self.assertNotIn("Style Source", output_fields)
        self.assertNotIn("Style Status", output_fields)


class MergeTests(unittest.TestCase):
    def test_merge_appends_only_new_export_rows_and_preserves_master_styles(self):
        master_rows = read_csv_text(
            "Catalog#,Artist,Title,Released,Style,release_id,Date Added\n"
            "A1,Existing Artist,Existing Title,2024,House,111,2026-01-01\n"
        )
        export_rows = read_csv_text(
            "Catalog#,Artist,Title,Released,release_id,Date Added\n"
            "A1,Existing Artist,Existing Title,2024,111,2026-01-01\n"
            "B2,Cauê (6),Revelations,2024,30887115,2026-06-04\n"
        )
        output_fields = build_output_fieldnames(
            ["Catalog#", "Artist", "Title", "Released", "Style", "release_id", "Date Added"]
        )

        merged_rows, appended_count = merge_master_and_export_rows(
            master_rows=master_rows,
            export_rows=export_rows,
            output_fieldnames=output_fields,
        )

        self.assertEqual(appended_count, 1)
        self.assertEqual(len(merged_rows), 2)
        self.assertEqual(merged_rows[0]["Style"], "House")
        self.assertEqual(merged_rows[1]["release_id"], "30887115")
        self.assertEqual(merged_rows[1]["Style"], "")

    def test_merge_ignores_master_only_custom_columns_when_matching_export_rows(self):
        master_rows = read_csv_text(
            "Catalog#,Artist,Title,Released,Style,release_id,Date Added,My Notes\n"
            "A1,Existing Artist,Existing Title,2024,House,111,2026-01-01,keep this\n"
        )
        export_rows = read_csv_text(
            "Catalog#,Artist,Title,Released,release_id,Date Added\n"
            "A1,Existing Artist,Existing Title,2024,111,2026-01-01\n"
        )
        output_fields = build_output_fieldnames(
            [
                "Catalog#",
                "Artist",
                "Title",
                "Released",
                "Style",
                "release_id",
                "Date Added",
                "My Notes",
            ]
        )

        merged_rows, appended_count = merge_master_and_export_rows(
            master_rows=master_rows,
            export_rows=export_rows,
            output_fieldnames=output_fields,
        )

        self.assertEqual(appended_count, 0)
        self.assertEqual(len(merged_rows), 1)
        self.assertEqual(merged_rows[0]["My Notes"], "keep this")


class EnrichmentTests(unittest.TestCase):
    def test_update_missing_metadata_preserves_existing_values_and_fills_blanks_independently(self):
        rows = [
            {"release_id": "111", "Style": "House", "Genre": "", "Updated At": "old"},
            {"release_id": "30887115", "Style": "", "Genre": "", "Updated At": ""},
        ]
        calls = []

        def lookup_metadata(release_id):
            calls.append(release_id)
            return ReleaseMetadataLookup(
                release_id=release_id,
                looked_up_at="2026-06-05T10:55:00Z",
                master_id=3509550,
                style=MetadataFieldLookup(
                    values=("Deep Techno", "Ambient"),
                    source="api_release",
                    status="filled",
                    notes="",
                ),
                genre=MetadataFieldLookup(
                    values=("Electronic",),
                    source="api_release",
                    status="filled",
                    notes="",
                ),
            )

        summary = update_missing_metadata(
            rows=rows,
            lookup_metadata=lookup_metadata,
            updated_at="2026-06-05T11:00:00Z",
        )

        self.assertEqual(calls, ["111", "30887115"])
        self.assertEqual(rows[0]["Style"], "House")
        self.assertEqual(rows[0]["Genre"], "Electronic")
        self.assertEqual(rows[0]["Updated At"], "2026-06-05T11:00:00Z")
        self.assertEqual(rows[1]["Style"], "Deep Techno, Ambient")
        self.assertEqual(rows[1]["Genre"], "Electronic")
        self.assertEqual(summary.filled_style_count, 1)
        self.assertEqual(summary.filled_genre_count, 2)
        self.assertEqual(summary.preserved_style_count, 1)
        self.assertEqual(summary.preserved_genre_count, 0)

    def test_update_missing_metadata_marks_not_sure_without_guessing(self):
        rows = [{"release_id": "8150100", "Style": "", "Genre": ""}]

        def lookup_metadata(release_id):
            return ReleaseMetadataLookup(
                release_id=release_id,
                looked_up_at="2026-06-05T10:55:00Z",
                master_id=0,
                style=MetadataFieldLookup(
                    values=(),
                    source="api_release",
                    status="blank",
                    notes="no explicit styles found",
                ),
                genre=MetadataFieldLookup(
                    values=(),
                    source="api_release",
                    status="blank",
                    notes="no explicit genres found",
                ),
            )

        summary = update_missing_metadata(
            rows=rows,
            lookup_metadata=lookup_metadata,
            updated_at="2026-06-05T11:00:00Z",
        )

        self.assertEqual(rows[0]["Style"], "")
        self.assertEqual(rows[0]["Genre"], "")
        self.assertEqual(rows[0]["Style Notes"], "no explicit styles found")
        self.assertEqual(rows[0]["Genre Notes"], "no explicit genres found")
        self.assertEqual(summary.blank_count, 1)
        self.assertEqual(summary.not_sure_release_ids, ("8150100",))

    def test_update_missing_metadata_preserves_existing_values_when_release_id_is_missing(self):
        rows = [
            {
                "release_id": "",
                "Style": "House",
                "Genre": "Electronic",
                "Updated At": "2026-06-01T10:00:00Z",
            }
        ]

        def lookup_metadata(_release_id):
            raise AssertionError("missing release_id rows should not be looked up")

        summary = update_missing_metadata(
            rows=rows,
            lookup_metadata=lookup_metadata,
            updated_at="2026-06-05T11:00:00Z",
        )

        self.assertEqual(rows[0]["Style"], "House")
        self.assertEqual(rows[0]["Genre"], "Electronic")
        self.assertEqual(rows[0]["Style Notes"], "")
        self.assertEqual(rows[0]["Genre Notes"], "")
        self.assertEqual(rows[0]["Updated At"], "2026-06-01T10:00:00Z")
        self.assertEqual(summary.preserved_style_count, 1)
        self.assertEqual(summary.preserved_genre_count, 1)
        self.assertEqual(summary.blank_count, 0)
        self.assertEqual(summary.not_sure_release_ids, ())

    def test_update_missing_metadata_reports_row_progress(self):
        rows = [
            {"release_id": "111", "Style": "", "Genre": ""},
            {"release_id": "222", "Style": "", "Genre": ""},
        ]
        progress_stream = TerminalStream()
        progress = ProgressReporter(stream=progress_stream)

        def lookup_metadata(release_id):
            return ReleaseMetadataLookup(
                release_id=release_id,
                looked_up_at="2026-06-05T10:55:00Z",
                master_id=0,
                style=MetadataFieldLookup(
                    values=(f"Style {release_id}",),
                    source="api_release",
                    status="filled",
                    notes="",
                ),
                genre=MetadataFieldLookup(
                    values=("Electronic",),
                    source="api_release",
                    status="filled",
                    notes="",
                ),
            )

        update_missing_metadata(
            rows=rows,
            lookup_metadata=lookup_metadata,
            updated_at="2026-06-05T11:00:00Z",
            progress=progress,
        )

        progress_text = progress_stream.getvalue()
        self.assertIn("\rEnriching rows [", progress_text)
        self.assertIn("1/2", progress_text)
        self.assertIn("50%", progress_text)
        self.assertIn("2/2", progress_text)
        self.assertIn("100%", progress_text)
        self.assertTrue(progress_text.endswith("\n"))

    def test_update_missing_metadata_can_lookup_in_parallel_without_reordering_rows(self):
        rows = [
            {"release_id": "111", "Style": "", "Genre": ""},
            {"release_id": "222", "Style": "", "Genre": ""},
        ]
        second_lookup_started = threading.Event()

        def lookup_metadata(release_id):
            if release_id == "111":
                self.assertTrue(second_lookup_started.wait(timeout=1))
            if release_id == "222":
                second_lookup_started.set()
            return ReleaseMetadataLookup(
                release_id=release_id,
                looked_up_at="2026-06-05T10:55:00Z",
                master_id=0,
                style=MetadataFieldLookup(
                    values=(f"Style {release_id}",),
                    source="api_release",
                    status="filled",
                    notes="",
                ),
                genre=MetadataFieldLookup(
                    values=(f"Genre {release_id}",),
                    source="api_release",
                    status="filled",
                    notes="",
                ),
            )

        summary = update_missing_metadata(
            rows=rows,
            lookup_metadata=lookup_metadata,
            updated_at="2026-06-05T11:00:00Z",
            max_workers=2,
        )

        self.assertEqual(rows[0]["release_id"], "111")
        self.assertEqual(rows[0]["Style"], "Style 111")
        self.assertEqual(rows[1]["release_id"], "222")
        self.assertEqual(rows[1]["Style"], "Style 222")
        self.assertEqual(summary.filled_style_count, 2)


class TerminalStream(io.StringIO):
    def isatty(self):
        return True


class NonTerminalStream(io.StringIO):
    def isatty(self):
        return False


class ProgressReporterTests(unittest.TestCase):
    def test_progress_reporter_updates_same_terminal_line_with_percentage(self):
        stream = TerminalStream()
        progress = ProgressReporter(stream=stream, width=10)

        progress.start(total=4)
        progress.update(current=2)
        progress.update(current=4)
        progress.finish()

        output = stream.getvalue()
        self.assertIn("\rEnriching rows [----------] 0/4 0%", output)
        self.assertIn("\rEnriching rows [#####-----] 2/4 50%", output)
        self.assertIn("\rEnriching rows [##########] 4/4 100%", output)
        self.assertTrue(output.endswith("\n"))

    def test_progress_reporter_stays_quiet_when_stream_is_not_terminal(self):
        stream = NonTerminalStream()
        progress = ProgressReporter(stream=stream)

        progress.start(total=2)
        progress.update(current=1)
        progress.finish()

        self.assertEqual(stream.getvalue(), "")


class ReportTests(unittest.TestCase):
    def test_write_report_distinguishes_which_metadata_fields_are_missing(self):
        rows = [
            {
                "release_id": "111",
                "Artist": "Style Only Artist",
                "Title": "Style Only Title",
                "Style": "",
                "Genre": "Electronic",
                "Style Notes": "no explicit styles found",
                "Genre Notes": "",
            },
            {
                "release_id": "222",
                "Artist": "Genre Only Artist",
                "Title": "Genre Only Title",
                "Style": "House",
                "Genre": "",
                "Style Notes": "",
                "Genre Notes": "no explicit genres found",
            },
            {
                "release_id": "333",
                "Artist": "Both Missing Artist",
                "Title": "Both Missing Title",
                "Style": "",
                "Genre": "",
                "Style Notes": "no explicit styles found",
                "Genre Notes": "no explicit genres found",
            },
        ]
        summary = enricher.RunSummary(
            input_rows=3,
            master_rows_before=0,
            output_rows=3,
            appended_rows=3,
            filled_style_count=0,
            filled_genre_count=0,
            preserved_style_count=0,
            preserved_genre_count=0,
            blank_count=3,
            error_count=0,
            not_sure_release_ids=("111", "222", "333"),
            output_path=Path("collection/enriched-collection.csv"),
            report_path=Path("reports/report.txt"),
            cache_path=Path("collection/processing.cache.json"),
            processed_export_path=None,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "report.txt"

            enricher.write_report(report_path, summary, rows)

            report_text = report_path.read_text(encoding="utf-8")

        self.assertIn(
            "- 111: Style Only Artist - Style Only Title (missing: Style; style: no explicit styles found)",
            report_text,
        )
        self.assertIn(
            "- 222: Genre Only Artist - Genre Only Title (missing: Genre; genre: no explicit genres found)",
            report_text,
        )
        self.assertIn(
            "- 333: Both Missing Artist - Both Missing Title (missing: Style, Genre; "
            "style: no explicit styles found; genre: no explicit genres found)",
            report_text,
        )


class LookupTests(unittest.TestCase):
    def test_parse_styles_from_api_payload_returns_clean_tuple(self):
        payload = {"styles": ["Deep Techno", "Ambient", "", None]}

        self.assertEqual(parse_styles_from_api_payload(payload), ("Deep Techno", "Ambient"))

    def test_parse_genres_from_api_payload_returns_clean_tuple(self):
        payload = {"genres": ["Electronic", "Hip Hop", "", None]}

        self.assertEqual(parse_genres_from_api_payload(payload), ("Electronic", "Hip Hop"))

    def test_resolve_release_metadata_uses_release_for_style_and_genre(self):
        requested_json_urls = []

        def get_json(url):
            requested_json_urls.append(url)
            if url.endswith("/releases/123"):
                return {"styles": ["Deep Techno"], "genres": ["Electronic"], "master_id": 456}
            raise AssertionError(f"unexpected JSON URL {url}")

        result = resolve_release_metadata("123", get_json=get_json)

        self.assertEqual(result.style.values, ("Deep Techno",))
        self.assertEqual(result.style.source, "api_release")
        self.assertEqual(result.genre.values, ("Electronic",))
        self.assertEqual(result.genre.source, "api_release")
        self.assertEqual(result.master_id, 456)
        self.assertEqual(requested_json_urls, [enricher.release_api_url("123")])

    def test_resolve_release_metadata_uses_master_for_missing_fields(self):
        requested_json_urls = []

        def get_json(url):
            requested_json_urls.append(url)
            if url.endswith("/releases/123"):
                return {"styles": [], "genres": ["Electronic"], "master_id": 456}
            if url.endswith("/masters/456"):
                return {"styles": ["Boom Bap"], "genres": ["Hip Hop"]}
            raise AssertionError(f"unexpected JSON URL {url}")

        result = resolve_release_metadata("123", get_json=get_json)

        self.assertEqual(result.style.values, ("Boom Bap",))
        self.assertEqual(result.style.source, "api_master")
        self.assertEqual(result.style.notes, "master_id=456")
        self.assertEqual(result.genre.values, ("Electronic",))
        self.assertEqual(result.genre.source, "api_release")
        self.assertEqual(len(requested_json_urls), 2)

    def test_resolve_release_metadata_leaves_missing_fields_blank_without_guessing(self):
        def get_json(url):
            if url.endswith("/releases/123"):
                return {"styles": [], "genres": [], "master_id": 0}
            raise AssertionError(f"unexpected JSON URL {url}")

        result = resolve_release_metadata("123", get_json=get_json)

        self.assertEqual(result.style.values, ())
        self.assertEqual(result.style.source, "api_release")
        self.assertEqual(result.style.status, "blank")
        self.assertEqual(result.style.notes, "no explicit styles found")
        self.assertEqual(result.genre.values, ())
        self.assertEqual(result.genre.source, "api_release")
        self.assertEqual(result.genre.status, "blank")
        self.assertEqual(result.genre.notes, "no explicit genres found")

    def test_resolve_release_metadata_marks_both_fields_error_when_release_api_fails(self):
        def get_json(url):
            if url.endswith("/releases/123"):
                raise RuntimeError("api unavailable")
            raise AssertionError(f"unexpected JSON URL {url}")

        result = resolve_release_metadata("123", get_json=get_json)

        self.assertEqual(result.style.values, ())
        self.assertEqual(result.style.source, "api_release")
        self.assertEqual(result.style.status, "error")
        self.assertIn("api_release failed", result.style.notes)
        self.assertEqual(result.genre.values, ())
        self.assertEqual(result.genre.source, "api_release")
        self.assertEqual(result.genre.status, "error")
        self.assertIn("api_release failed", result.genre.notes)

    def test_resolve_release_metadata_marks_missing_fields_error_when_master_api_fails(self):
        def get_json(url):
            if url.endswith("/releases/123"):
                return {"styles": [], "genres": [], "master_id": 456}
            if url.endswith("/masters/456"):
                raise RuntimeError("master api unavailable")
            raise AssertionError(f"unexpected JSON URL {url}")

        result = resolve_release_metadata("123", get_json=get_json)

        self.assertEqual(result.style.values, ())
        self.assertEqual(result.style.source, "api_release+api_master")
        self.assertEqual(result.style.status, "error")
        self.assertIn("api_master failed", result.style.notes)
        self.assertEqual(result.genre.values, ())
        self.assertEqual(result.genre.source, "api_release+api_master")
        self.assertEqual(result.genre.status, "error")
        self.assertIn("api_master failed", result.genre.notes)


class RateLimitTests(unittest.TestCase):
    def test_rate_limiter_uses_discogs_headers_to_space_requests(self):
        current_time = [100.0]
        sleeps = []

        def now():
            return current_time[0]

        def sleep(seconds):
            sleeps.append(seconds)
            current_time[0] += seconds

        limiter = DiscogsRateLimiter(
            fallback_request_interval_seconds=0,
            now=now,
            sleep=sleep,
        )
        limiter.update_from_headers({"x-discogs-ratelimit": "25"})

        limiter.wait_before_request()
        limiter.wait_before_request()

        self.assertAlmostEqual(sleeps[0], 60 / 23)

    def test_http_get_updates_rate_limiter_from_response_headers(self):
        class FakeResponse:
            headers = {
                "x-discogs-ratelimit": "25",
                "x-discogs-ratelimit-remaining": "24",
            }

            def __enter__(self):
                return self

            def __exit__(self, _error_type, _error, _traceback):
                return False

            def read(self):
                return b'{"ok": true}'

        class FakeLimiter:
            def __init__(self):
                self.waited = False
                self.updated_headers = None

            def wait_before_request(self):
                self.waited = True

            def update_from_headers(self, headers):
                self.updated_headers = headers

        limiter = FakeLimiter()

        with patch.object(enricher, "urlopen", return_value=FakeResponse()):
            body = enricher.http_get(
                "https://api.discogs.com/releases/1",
                user_agent="test",
                token="",
                timeout_seconds=1,
                accept="application/json",
                rate_limiter=limiter,
            )

        self.assertEqual(body, '{"ok": true}')
        self.assertTrue(limiter.waited)
        self.assertEqual(limiter.updated_headers["x-discogs-ratelimit"], "25")


class CacheTests(unittest.TestCase):
    def test_save_and_load_lookup_cache_uses_schema_versioned_release_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "cache.json"
            cache = {
                "30887115": ReleaseMetadataLookup(
                    release_id="30887115",
                    looked_up_at="2026-06-05T11:00:00Z",
                    master_id=3509550,
                    style=MetadataFieldLookup(
                        values=("Deep Techno", "Ambient"),
                        source="api_release",
                        status="filled",
                        notes="",
                    ),
                    genre=MetadataFieldLookup(
                        values=("Electronic",),
                        source="api_release",
                        status="filled",
                        notes="",
                    ),
                )
            }

            save_lookup_cache(cache_path, cache)

            cache_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(cache_payload["schema_version"], 2)
            self.assertEqual(cache_payload["record_type"], "discogs_release_metadata")
            self.assertNotIn("attempted_sources", cache_payload["records"]["30887115"])
            self.assertEqual(cache_payload["records"]["30887115"]["genre"]["values"], ["Electronic"])
            self.assertEqual(load_lookup_cache(cache_path), cache)

    def test_load_lookup_cache_rejects_old_flat_style_cache(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "30887115": {
                            "styles": ["Deep Techno"],
                            "source": "api_release",
                            "status": "filled",
                            "notes": "",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unsupported cache format"):
                load_lookup_cache(cache_path)


class RunEnrichmentTests(unittest.TestCase):
    def test_parse_args_defaults_to_export_folder_and_reports_folder(self):
        default_export_path = Path("export/discogs-export.csv")

        with patch.object(enricher, "find_single_csv_export", return_value=default_export_path) as find_export:
            args = parse_args([])

        find_export.assert_called_once_with(Path("export"))
        self.assertEqual(enricher.DEFAULT_INPUT_DIRECTORY, Path("export"))
        self.assertEqual(args.export, default_export_path)
        self.assertEqual(args.report.parent, Path("reports"))
        self.assertRegex(args.report.name, r"^enriched-collection_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_report\.txt$")

    def test_parse_args_uses_single_csv_from_input_folder_and_default_master(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_directory = directory / "export"
            input_directory.mkdir()
            export_path = input_directory / "discogs-export.csv"
            export_path.write_text(STANDARD_DISCOGS_HEADER, encoding="utf-8")

            args = parse_args(["--input-dir", str(input_directory)])

            self.assertEqual(args.export, export_path)
            self.assertEqual(DEFAULT_MASTER_PATH, Path("collection/enriched-collection.csv"))
            self.assertEqual(args.master, DEFAULT_MASTER_PATH)
            self.assertEqual(args.output, DEFAULT_MASTER_PATH)
            self.assertEqual(args.cache, Path("collection/processing.cache.json"))
            self.assertEqual(args.max_workers, 3)
            self.assertTrue(args.move_processed_export)

    def test_find_single_csv_export_rejects_multiple_csv_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            input_directory = Path(temporary_directory)
            (input_directory / "first.csv").write_text(STANDARD_DISCOGS_HEADER, encoding="utf-8")
            (input_directory / "second.csv").write_text(STANDARD_DISCOGS_HEADER, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "expected exactly one CSV export"):
                find_single_csv_export(input_directory)

    def test_validate_discogs_export_fieldnames_rejects_missing_standard_columns(self):
        with self.assertRaisesRegex(ValueError, "missing required Discogs export columns"):
            validate_discogs_export_fieldnames(["release_id"])

    def test_validate_discogs_export_fieldnames_rejects_duplicate_columns(self):
        fieldnames = STANDARD_DISCOGS_HEADER.strip().split(",")
        fieldnames.append("release_id")

        with self.assertRaisesRegex(ValueError, "duplicate columns: release_id"):
            validate_discogs_export_fieldnames(fieldnames)

    def test_processed_export_target_rejects_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            export_path = directory / "export" / "discogs-export.csv"
            processed_path = directory / "processed" / "discogs-export.csv"
            export_path.parent.mkdir()
            processed_path.parent.mkdir()
            export_path.write_text(STANDARD_DISCOGS_HEADER, encoding="utf-8")
            processed_path.write_text(STANDARD_DISCOGS_HEADER, encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "processed export already exists"):
                ensure_processed_export_target_available(export_path, processed_path.parent)

    def test_run_enrichment_updates_master_from_new_export_without_network_in_tests(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            master_path = directory / "master.csv"
            export_path = directory / "export.csv"
            output_path = directory / "output.csv"
            report_path = directory / "report.txt"
            cache_path = directory / "cache.json"
            master_path.write_text(
                "Catalog#,Artist,Title,Label,Format,Rating,Released,Style,release_id,CollectionFolder,Date Added,Collection Media Condition,Collection Sleeve Condition,Collection Notes\n"
                "A1,Existing Artist,Existing Title,Existing Label,LP,5,2024,House,111,Collection,2026-01-01,Near Mint,Near Mint,\n",
                encoding="utf-8",
            )
            export_path.write_text(
                STANDARD_DISCOGS_HEADER
                + "A1,Existing Artist,Existing Title,Existing Label,LP,5,2024,111,Collection,2026-01-01,Near Mint,Near Mint,\n"
                + "B2,Cauê (6),Revelations,Freestyle Man,12\",5,2024,30887115,Collection,2026-06-04,Near Mint,Near Mint,\n",
                encoding="utf-8",
            )

            def fake_json_getter(_user_agent, _token, _timeout_seconds, _request_interval_seconds=0):
                def get_json(url):
                    if url.endswith("/releases/30887115"):
                        return {
                            "styles": ["Deep Techno", "Ambient"],
                            "genres": ["Electronic"],
                            "master_id": 3509550,
                        }
                    raise AssertionError(f"unexpected JSON URL {url}")

                return get_json

            args = argparse.Namespace(
                export=export_path,
                master=master_path,
                output=output_path,
                report=report_path,
                cache=cache_path,
                user_agent="test",
                discogs_token="",
                timeout_seconds=1,
                request_interval_seconds=0,
                refresh_existing=False,
            )

            with patch.object(enricher, "make_http_json_getter", fake_json_getter):
                summary = run_enrichment(args)

            output_rows = read_csv_text(output_path.read_text(encoding="utf-8"))
            output_fieldnames = list(csv.DictReader(io.StringIO(output_path.read_text(encoding="utf-8"))).fieldnames or [])
            cache_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            report_text = report_path.read_text(encoding="utf-8")

            self.assertEqual(summary.appended_rows, 1)
            self.assertEqual(summary.filled_style_count, 1)
            self.assertEqual(summary.filled_genre_count, 1)
            self.assertEqual(summary.preserved_style_count, 1)
            self.assertEqual(output_rows[0]["Style"], "House")
            self.assertEqual(output_rows[1]["Style"], "Deep Techno, Ambient")
            self.assertEqual(output_rows[1]["Genre"], "Electronic")
            self.assertNotIn("Style Source", output_fieldnames)
            self.assertNotIn("Style Status", output_fieldnames)
            self.assertNotIn("Style Updated At", output_fieldnames)
            self.assertEqual(cache_payload["schema_version"], 2)
            self.assertEqual(cache_payload["records"]["30887115"]["style"]["source"], "api_release")
            self.assertEqual(cache_payload["records"]["30887115"]["genre"]["status"], "filled")
            self.assertIn("Appended rows: 1", report_text)
            self.assertIn("Filled missing genres: 1", report_text)
            self.assertIn("Left blank / not sure: 0", report_text)

    def test_run_enrichment_moves_default_folder_export_after_success(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            export_path = directory / "export" / "discogs-export.csv"
            processed_directory = directory / "processed"
            output_path = directory / "enriched-collection.csv"
            report_path = directory / "report.txt"
            cache_path = directory / "cache.json"
            export_path.parent.mkdir()
            export_path.write_text(
                STANDARD_DISCOGS_HEADER
                + "B2,Cauê (6),Revelations,Freestyle Man,12\",5,2024,30887115,Collection,2026-06-04,Near Mint,Near Mint,\n",
                encoding="utf-8",
            )

            def fake_json_getter(_user_agent, _token, _timeout_seconds, _request_interval_seconds=0):
                def get_json(url):
                    if url.endswith("/releases/30887115"):
                        return {
                            "styles": ["Deep Techno", "Ambient"],
                            "genres": ["Electronic"],
                            "master_id": 3509550,
                        }
                    raise AssertionError(f"unexpected JSON URL {url}")

                return get_json

            args = argparse.Namespace(
                export=export_path,
                master=output_path,
                output=output_path,
                report=report_path,
                cache=cache_path,
                user_agent="test",
                discogs_token="",
                timeout_seconds=1,
                request_interval_seconds=0,
                refresh_existing=False,
                processed_dir=processed_directory,
                move_processed_export=True,
            )

            with patch.object(enricher, "make_http_json_getter", fake_json_getter):
                summary = run_enrichment(args)

            processed_path = processed_directory / "discogs-export.csv"
            self.assertFalse(export_path.exists())
            self.assertTrue(processed_path.exists())
            self.assertEqual(summary.processed_export_path, processed_path)

    def test_parsed_export_folder_run_moves_export_to_processed_after_success(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            export_directory = directory / "export"
            processed_directory = directory / "processed"
            export_path = export_directory / "discogs-export.csv"
            output_path = directory / "enriched-collection.csv"
            report_path = directory / "report.txt"
            cache_path = directory / "cache.json"
            export_directory.mkdir()
            export_path.write_text(
                STANDARD_DISCOGS_HEADER
                + "B2,Cauê (6),Revelations,Freestyle Man,12\",5,2024,30887115,Collection,2026-06-04,Near Mint,Near Mint,\n",
                encoding="utf-8",
            )

            def fake_json_getter(_user_agent, _token, _timeout_seconds, _request_interval_seconds=0):
                def get_json(url):
                    if url.endswith("/releases/30887115"):
                        return {
                            "styles": ["Deep Techno", "Ambient"],
                            "genres": ["Electronic"],
                            "master_id": 3509550,
                        }
                    raise AssertionError(f"unexpected JSON URL {url}")

                return get_json

            args = parse_args(
                [
                    "--input-dir",
                    str(export_directory),
                    "--processed-dir",
                    str(processed_directory),
                    "--master",
                    str(output_path),
                    "--cache",
                    str(cache_path),
                    "--report",
                    str(report_path),
                    "--no-progress",
                ]
            )

            with patch.object(enricher, "make_http_json_getter", fake_json_getter):
                summary = run_enrichment(args)

            processed_path = processed_directory / "discogs-export.csv"
            self.assertTrue(args.move_processed_export)
            self.assertFalse(export_path.exists())
            self.assertTrue(processed_path.exists())
            self.assertEqual(summary.processed_export_path, processed_path)

    def test_run_enrichment_keeps_export_in_input_folder_when_lookup_errors(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            export_path = directory / "export" / "discogs-export.csv"
            processed_directory = directory / "processed"
            output_path = directory / "enriched-collection.csv"
            report_path = directory / "report.txt"
            cache_path = directory / "cache.json"
            export_path.parent.mkdir()
            export_path.write_text(
                STANDARD_DISCOGS_HEADER
                + "B2,Cauê (6),Revelations,Freestyle Man,12\",5,2024,30887115,Collection,2026-06-04,Near Mint,Near Mint,\n",
                encoding="utf-8",
            )

            def fake_json_getter(_user_agent, _token, _timeout_seconds, _request_interval_seconds=0):
                def get_json(_url):
                    raise RuntimeError("api unavailable")

                return get_json

            args = argparse.Namespace(
                export=export_path,
                master=output_path,
                output=output_path,
                report=report_path,
                cache=cache_path,
                user_agent="test",
                discogs_token="",
                timeout_seconds=1,
                request_interval_seconds=0,
                refresh_existing=False,
                processed_dir=processed_directory,
                move_processed_export=True,
            )

            with patch.object(enricher, "make_http_json_getter", fake_json_getter):
                summary = run_enrichment(args)

            self.assertEqual(summary.error_count, 1)
            self.assertTrue(export_path.exists())
            self.assertFalse((processed_directory / "discogs-export.csv").exists())

    def test_main_prints_clear_error_for_invalid_export_header(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            export_path = directory / "export.csv"
            export_path.write_text("release_id\n30887115\n", encoding="utf-8")

            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                exit_code = main(
                    [
                        "--export",
                        str(export_path),
                        "--master",
                        str(directory / "master.csv"),
                        "--cache",
                        str(directory / "cache.json"),
                        "--report",
                        str(directory / "report.txt"),
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("Error: export CSV header is missing required Discogs export columns", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
