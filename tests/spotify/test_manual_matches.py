import csv
import io
import os
import json
import sys
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.manual_matches import import_manual_match_overrides, main
from publishers.spotify.match_cache import (
    MATCHER_VERSION,
    cache_track_match,
    cached_track_match,
    load_spotify_track_match_cache,
    save_spotify_track_match_cache,
    spotify_track_match_key,
    track_match_cache_entry,
)
from publishers.spotify.matching import MATCHED, UNMATCHED, PlaylistTrack, TrackMatchDecision
from shared.tunemymusic import TUNEMYMUSIC_COLUMNS


def write_playlist_master(path: Path, *, source_values: Mapping[str, str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output_file:
        fieldnames: list[str] = list(TUNEMYMUSIC_COLUMNS)
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        row = {
            "Release Id": "release-1",
            "Album Name": "Source Album",
            "Track Number": "1",
            "Track Name": "Source Track",
            "Artist Name": "Source Artist",
            "Spotify Search Query": "Source Artist Source Track Source Album",
        }
        if source_values is not None:
            row.update(source_values)
        writer.writerow(row)


def write_override_file(
    path: Path,
    *,
    spotify_uri: str = "spotify:track:manual-one",
    source_values: Mapping[str, str] | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_type": "spotify_manual_match_overrides",
                "matches": [
                    {
                        "release_id": "release-1",
                        "track_number": "1",
                        "artist_name": "Source Artist",
                        "album_name": "Source Album",
                        "track_name": "Source Track",
                        "spotify_uri": spotify_uri,
                        "spotify_track_name": "Spotify Track",
                        "spotify_artist_names": ["Spotify Artist"],
                        "spotify_album_name": "Spotify Album",
                        "spotify_album_id": "spotify-album-one",
                        **(source_values or {}),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


V2_SOURCE = {
    "release_id": "321",
    "track_number": "1",
    "artist_name": "Source Artist",
    "album_name": "Source Album",
    "track_name": "Source Track",
}
V2_URI = "spotify:track:handpicked"
V2_TARGET_LABELS = {
    "spotify_track_name": "Élan 星",
    "spotify_artist_names": ["First Artist", "Second Artist"],
    "spotify_album_name": "Target Album",
    "spotify_album_id": "target-album-id",
}


def write_v2_review(path: Path, *rows: Mapping[str, object]) -> None:
    path.write_text(json.dumps({
        "schema_version": 2,
        "record_type": "spotify_manual_match_overrides",
        "matches": rows,
    }), encoding="utf-8")


def run_cli(masters: Path, cache: Path, *options: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with (
        redirect_stdout(stdout),
        redirect_stderr(stderr),
        patch("socket.socket.connect", side_effect=AssertionError("manual overrides must remain offline")),
    ):
        try:
            result = main(["--playlist-output-directory", str(masters), "--match-cache", str(cache), *options])
        except SystemExit as error:
            assert isinstance(error.code, int)
            result = error.code
    return result, stdout.getvalue(), stderr.getvalue()


class SpotifyManualMatchTests(unittest.TestCase):
    def test_prepare_map_selects_spotify_and_round_trips_without_catalog_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            masters = directory / "masters"
            write_playlist_master(masters / "First" / "First.csv", source_values={"Release Id": "321"})
            write_playlist_master(masters / "Second" / "Second.csv", source_values={
                "Release Id": "321", "Track Number": "2", "Track Name": "Second Track",
            })
            draft = directory / "choices.json"
            draft.write_text(json.dumps({"publishers": {
                "spotify": {"321": {"1": "spotify:track:studio", "2": V2_URI}},
                "other": {"321": {"1": {"catalogue_number": 445}}},
            }}), encoding="utf-8")
            cache = directory / "matches.json"
            key = "321|1|source artist|source album|source track"
            save_spotify_track_match_cache(cache, {key: {
                **V2_SOURCE, "match_status": UNMATCHED,
                "review_candidates": [{**V2_TARGET_LABELS, "spotify_uri": "spotify:track:studio"}],
            }})
            original_cache = cache.read_bytes()
            output = directory / "review.json"

            result, stdout, stderr = run_cli(masters, cache, "--prepare-map", str(draft), "--output", str(output))
            self.assertEqual((result, stderr), (0, ""))
            for text in ("other", "321", "Source Track", "Source Artist", "Source Album",
                         "Élan 星", "Target Album", V2_URI, "unknown"):
                self.assertIn(text, stdout)
            snapshot = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["schema_version"], 2)
            by_uri = {row["spotify_uri"]: row for row in snapshot["matches"]}
            self.assertEqual(set(by_uri), {"spotify:track:studio", V2_URI})
            self.assertEqual(by_uri["spotify:track:studio"], {**V2_SOURCE, **V2_TARGET_LABELS,
                                                            "spotify_uri": "spotify:track:studio"})
            self.assertNotIn("spotify_album_name", by_uri[V2_URI])
            self.assertEqual(cache.read_bytes(), original_cache)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

            result, stdout, stderr = run_cli(masters, cache, "--overrides", str(output))
            self.assertEqual((result, stderr), (0, ""))
            self.assertIn("Would apply manual Spotify matches: 2", stdout)
            self.assertEqual(cache.read_bytes(), original_cache)
            result, stdout, stderr = run_cli(masters, cache, "--overrides", str(output), "--apply")
            self.assertEqual((result, stderr), (0, ""))
            self.assertIn("Applied manual Spotify matches: 2", stdout)
            records = load_spotify_track_match_cache(cache)
            self.assertEqual(records[key]["spotify_album_name"], V2_TARGET_LABELS["spotify_album_name"])
            self.assertEqual({record["spotify_uri"] for record in records.values()}, {"spotify:track:studio", V2_URI})
            self.assertEqual(records["321|2|source artist|source album|second track"]["match_strategy"], "manual_uri_only")

    def test_prepare_map_rejects_invalid_or_ambiguous_selected_input_and_accepts_an_empty_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            masters = directory / "masters"
            write_playlist_master(masters / "Set" / "Set.csv", source_values={"Release Id": "321"})
            draft = directory / "choices.json"
            output = directory / "review.json"
            cache = directory / "matches.json"
            targets = '"1": "spotify:track:studio"'
            drafts = (
                ('{"spotify": {}}', "publishers"),
                ('{"publishers": {"spotfy": {}}}', "spotify"),
                ('{"publishers": {"spotify": []}}', "spotify"),
                ('{"publishers": {"spotify": {}, "spotify": {}}}', "duplicate"),
                ('{"publishers": {"spotify": {"321": {}, "321": {}}}}', "duplicate"),
                ('{"publishers": {"spotify": {"321": {%s, %s}}}}' % (targets, targets), "duplicate"),
                ('{"publishers": {"spotify": {}, "other": {"x": 1, "x": 2}}}', "duplicate"),
                ('{"publishers": {"spotify": {"01": {}}}}', "release ID"),
                ('{"publishers": {"spotify": {"321": {"01": "spotify:track:studio"}}}}', "track number"),
                ('{"publishers": {"spotify": {"321": {"١": "spotify:track:studio"}}}}', "track number"),
                ('{"publishers": {"spotify": {"321": {"A1": "spotify:track:studio"}}}}', "track number"),
                ('{"publishers": {"spotify": {"321": {"1": " "}}}}', "track 1"),
                ('{"publishers": {"spotify": {"321": {"1": 345}}}}', "track 1"),
                ('{"publishers": {"spotify": {"321": {"1": "spotify:album:studio"}}}}', "track 1"),
            )
            for raw, expected_error in drafts:
                with self.subTest(raw=raw):
                    draft.write_text(raw, encoding="utf-8")
                    result, stdout, stderr = run_cli(masters, cache, "--prepare-map", str(draft), "--output", str(output))
                    self.assertEqual((result, stdout), (2, ""))
                    self.assertIn(expected_error, stderr)
                    self.assertFalse(output.exists())
                    self.assertFalse(cache.exists())

            draft.write_text('{"publishers": {"spotify": {}, "other": ["opaque"]}}', encoding="utf-8")
            result, stdout, stderr = run_cli(masters, cache, "--prepare-map", str(draft), "--output", str(output))
            self.assertEqual((result, stderr), (0, ""))
            self.assertIn("other", stdout)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["matches"], [])
            self.assertFalse(cache.exists())

    def test_manual_cli_handles_json_beyond_the_parser_depth_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            masters = directory / "masters"
            masters.mkdir()
            draft = directory / "nested.json"
            excessive_depth = sys.getrecursionlimit() + 10
            draft.write_text("[" * excessive_depth + "0" + "]" * excessive_depth, encoding="utf-8")
            cache = directory / "matches.json"
            output = directory / "review.json"
            for flags in (
                ("--prepare-map", str(draft), "--output", str(output)),
                ("--overrides", str(draft), "--apply"),
            ):
                with self.subTest(flags=flags):
                    result, stdout, stderr = run_cli(masters, cache, *flags)
                    self.assertEqual((result, stdout), (2, ""))
                    self.assertIn("malformed", stderr)
                    self.assertFalse(output.exists() or cache.exists())

    def test_prepare_map_cli_requires_an_output_and_cannot_import_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            masters = directory / "masters"
            masters.mkdir()
            cache = directory / "matches.json"
            draft = directory / "choices.json"
            draft.write_text('{"publishers": {"spotify": {}}}', encoding="utf-8")
            output = directory / "review.json"
            draft_mode = ("--prepare-map", str(draft))
            output_argument = ("--output", str(output))
            overrides_mode = ("--overrides", str(draft))
            for options in (
                draft_mode, (*draft_mode, *output_argument, "--apply"),
                (*draft_mode, *overrides_mode, *output_argument), (*overrides_mode, *output_argument),
                (*overrides_mode, "--apply"),
            ):
                with self.subTest(options=options):
                    result, stdout, stderr = run_cli(masters, cache, *options)
                    self.assertEqual((result, stdout), (2, ""))
                    self.assertTrue(stderr)
                    self.assertFalse(output.exists())
                    self.assertFalse(cache.exists())

    def test_prepare_map_and_import_leave_files_untouched_when_a_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            masters = directory / "masters"
            first = masters / "First" / "First.csv"
            second = masters / "Second" / "Second.csv"
            write_playlist_master(first, source_values={"Release Id": "321"})
            write_playlist_master(second, source_values={"Release Id": "321"})
            draft = directory / "choices.json"
            cache = directory / "matches.json"
            save_spotify_track_match_cache(cache, {})
            original_cache = cache.read_bytes()
            output = directory / "review.json"
            for bad_selection in ({"2": "spotify:track:missing"}, {"3": "spotify:album:wrong"}):
                draft.write_text(json.dumps({"publishers": {"spotify": {"321": {"1": V2_URI, **bad_selection}}}}),
                                 encoding="utf-8")
                result, _, stderr = run_cli(masters, cache, "--prepare-map", str(draft), "--output", str(output))
                self.assertEqual(result, 2)
                self.assertIn("321", stderr)
                self.assertFalse(output.exists())
                self.assertEqual(cache.read_bytes(), original_cache)

            draft.write_text(json.dumps({"publishers": {"spotify": {"321": {"1": V2_URI}}}}), encoding="utf-8")
            result, _, stderr = run_cli(masters, cache, "--prepare-map", str(draft), "--output", str(output))
            self.assertEqual((result, stderr), (0, ""))
            original_review = output.read_bytes()
            write_playlist_master(second, source_values={"Release Id": "321", "Track Name": "New Song"})
            for flags in ((), ("--apply", "--replace-existing")):
                result, _, stderr = run_cli(masters, cache, "--overrides", str(output), *flags)
                self.assertEqual(result, 2)
                self.assertIn("conflicting", stderr)
                self.assertEqual((output.read_bytes(), cache.read_bytes()), (original_review, original_cache))

    def test_prepare_map_replacement_preview_does_not_authorize_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            masters = directory / "masters"
            write_playlist_master(masters / "Set" / "Set.csv", source_values={"Release Id": "321"})
            draft = directory / "choices.json"
            draft.write_text(json.dumps({"publishers": {"spotify": {"321": {"1": V2_URI}}}}), encoding="utf-8")
            output = directory / "review.json"
            cache = directory / "matches.json"
            key = "321|1|source artist|source album|source track"
            save_spotify_track_match_cache(cache, {key: {
                **V2_SOURCE, "match_status": "manual", "spotify_uri": "spotify:track:old", **V2_TARGET_LABELS,
            }})
            original_cache = cache.read_bytes()
            prepare = ("--prepare-map", str(draft), "--output", str(output))
            result, _, stderr = run_cli(masters, cache, *prepare)
            self.assertEqual(result, 2)
            self.assertIn("--replace-existing", stderr)
            self.assertFalse(output.exists())
            result, _, stderr = run_cli(masters, cache, *prepare, "--replace-existing")
            self.assertEqual((result, stderr), (0, ""))
            self.assertEqual(cache.read_bytes(), original_cache)
            self.assertNotIn("spotify_track_name", json.loads(output.read_text(encoding="utf-8"))["matches"][0])

            apply = ("--overrides", str(output), "--apply")
            result, _, stderr = run_cli(masters, cache, *apply)
            self.assertEqual(result, 2)
            self.assertIn("--replace-existing", stderr)
            self.assertEqual(cache.read_bytes(), original_cache)
            result, _, stderr = run_cli(masters, cache, *apply, "--replace-existing")
            self.assertEqual((result, stderr), (0, ""))
            self.assertEqual(load_spotify_track_match_cache(cache)[key]["spotify_uri"], V2_URI)

    def test_prepare_map_protects_inputs_absent_state_files_and_managed_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            masters = directory / "masters"
            write_playlist_master(masters / "Set" / "Set.csv", source_values={"Release Id": "321"})
            draft = directory / "choices.json"
            draft.write_text('{"publishers": {"spotify": {}}}', encoding="utf-8")
            original_draft = draft.read_bytes()
            cache = directory / "matches.json"
            state = directory / "spotify-publish-state.cache.json"
            managed = masters / "Set" / "review.json"
            hardlink = directory / "input-alias.json"
            os.link(draft, hardlink)
            symlink = directory / "dangling.json"
            symlink.symlink_to(directory / "missing.json")

            for output in (cache, state, managed, draft, hardlink, symlink):
                with self.subTest(output=output):
                    result, stdout, stderr = run_cli(masters, cache, "--prepare-map", str(draft), "--output", str(output))
                    self.assertEqual((result, stdout), (2, ""))
                    self.assertTrue(stderr)
                    self.assertEqual((draft.read_bytes(), hardlink.read_bytes()), (original_draft, original_draft))
                    self.assertTrue(symlink.is_symlink())
                    self.assertFalse(cache.exists() or state.exists() or managed.exists())

            for name in ("DEFAULT_SPOTIFY_MATCH_CACHE_PATH", "DEFAULT_SPOTIFY_PUBLISH_STATE_CACHE_PATH",
                         "DEFAULT_PROCESSING_CACHE_PATH", "DEFAULT_SEEN_TERMS_CACHE_PATH",
                         "DEFAULT_TRACKLIST_CACHE_PATH", "DEFAULT_TOKEN_CACHE_PATH", "DEFAULT_PUBLISHER_CONFIG_PATH"):
                with self.subTest(constant=name):
                    reserved = directory / f"{name}.json"
                    with patch(f"publishers.spotify.manual_matches.{name}", reserved):
                        result, _, stderr = run_cli(masters, cache, "--prepare-map", str(draft), "--output", str(reserved))
                    self.assertEqual(result, 2)
                    self.assertIn("protected", stderr)
                    self.assertFalse(reserved.exists())

    def test_prepare_map_does_not_overwrite_a_competing_writer_after_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            masters = directory / "masters"
            write_playlist_master(masters / "Set" / "Set.csv", source_values={"Release Id": "321"})
            draft = directory / "choices.json"
            draft.write_text(json.dumps({"publishers": {"spotify": {"321": {"1": V2_URI}}}}), encoding="utf-8")
            cache = directory / "matches.json"
            save_spotify_track_match_cache(cache, {})
            original_cache = cache.read_bytes()
            output = directory / "review.json"
            earlier_bytes = b"competing review bytes\n"

            def preview_then_create_new_output(*args, **kwargs):
                summary = import_manual_match_overrides(*args, **kwargs)
                with output.open("xb") as competing_file:
                    competing_file.write(earlier_bytes)
                return summary

            with patch("publishers.spotify.manual_matches.import_manual_match_overrides", side_effect=preview_then_create_new_output):
                result, stdout, stderr = run_cli(masters, cache, "--prepare-map", str(draft), "--output", str(output))
            self.assertEqual((result, stdout), (2, ""))
            self.assertIn("exists", stderr)
            self.assertEqual(output.read_bytes(), earlier_bytes)
            self.assertEqual(cache.read_bytes(), original_cache)
            self.assertEqual(list(directory.glob(".spotify-proposals-*")), [])

    def test_v2_rechecks_every_master_at_the_reviewed_position(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            masters = directory / "masters"
            first = masters / "First" / "First.csv"
            second = masters / "Second" / "Second.csv"
            source = {"Release Id": "321"}
            write_playlist_master(first, source_values=source)
            write_playlist_master(second, source_values=source)
            review = directory / "review.json"
            write_v2_review(review, {**V2_SOURCE, "spotify_uri": V2_URI})
            cache = directory / "matches.json"
            save_spotify_track_match_cache(cache, {})
            original_cache = cache.read_bytes()

            self.assertEqual(import_manual_match_overrides(review, masters, cache).planned_count, 1)
            for change in ({"Track Name": "Different Song"}, {"Track Name": "Source Track 星"}, {"Artist Name": ""}):
                write_playlist_master(second, source_values={**source, **change})
                for apply in (False, True):
                    with self.subTest(change=change, apply=apply), self.assertRaises(ValueError):
                        import_manual_match_overrides(review, masters, cache, apply=apply, replace_existing=True)
                self.assertEqual(cache.read_bytes(), original_cache)

            second.unlink()
            self.assertEqual(import_manual_match_overrides(review, masters, cache).planned_count, 1)
            write_playlist_master(first, source_values={**source, "Track Number": "2"})
            for remove in (False, True):
                if remove:
                    first.unlink()
                for apply in (False, True):
                    with self.subTest(remove=remove, apply=apply), self.assertRaises(ValueError):
                        import_manual_match_overrides(review, masters, cache, apply=apply, replace_existing=True)
                self.assertEqual(cache.read_bytes(), original_cache)

    def test_v2_applies_equivalent_source_spellings_to_every_current_cache_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            masters = directory / "masters"
            cache = directory / "matches.json"
            variants = {
                "First": {**V2_SOURCE, "track_name": "Straße"},
                "Second": {**V2_SOURCE, "track_name": "STRASSE"},
            }
            tracks = [
                PlaylistTrack(playlist_name=playlist, spotify_search_query="", **source)
                for playlist, source in variants.items()
            ]
            for track in tracks:
                write_playlist_master(masters / track.playlist_name / f"{track.playlist_name}.csv", source_values={
                    "Release Id": "321", "Track Name": track.track_name,
                })
            keys = [spotify_track_match_key(track) for track in tracks]
            self.assertNotEqual(*keys)
            save_spotify_track_match_cache(cache, {keys[1]: {
                **variants["Second"], "match_status": UNMATCHED,
                "review_candidates": [{**V2_TARGET_LABELS, "spotify_uri": V2_URI}],
            }})
            original_cache = cache.read_bytes()
            draft = directory / "choices.json"
            draft.write_text(json.dumps({"publishers": {"spotify": {"321": {"1": V2_URI}}}}), encoding="utf-8")
            review = directory / "review.json"

            result, _, stderr = run_cli(masters, cache, "--prepare-map", str(draft), "--output", str(review))
            self.assertEqual((result, stderr), (0, ""))
            row, = json.loads(review.read_text(encoding="utf-8"))["matches"]
            self.assertEqual(row, {**variants["First"], **V2_TARGET_LABELS, "spotify_uri": V2_URI})
            self.assertEqual(cache.read_bytes(), original_cache)

            self.assertEqual(import_manual_match_overrides(review, masters, cache, apply=True).applied_count, 1)
            records = load_spotify_track_match_cache(cache)
            for track, key in zip(tracks, keys):
                cached = cached_track_match(track, records)
                assert cached is not None
                self.assertEqual(cached.decision.spotify_uri, V2_URI)
                self.assertEqual(records[key]["track_name"], track.track_name)

            records[keys[1]]["spotify_uri"] = "spotify:track:other"
            save_spotify_track_match_cache(cache, records)
            original_cache = cache.read_bytes()
            for apply in (False, True):
                with self.subTest(apply=apply), self.assertRaisesRegex(ValueError, "--replace-existing"):
                    import_manual_match_overrides(review, masters, cache, apply=apply)
                self.assertEqual(cache.read_bytes(), original_cache)

            import_manual_match_overrides(review, masters, cache, apply=True, replace_existing=True)
            self.assertEqual({record["spotify_uri"] for record in load_spotify_track_match_cache(cache).values()}, {V2_URI})

    @unittest.skipUnless(sys.platform == "darwin", "macOS can alias reserved paths by case")
    def test_prepare_map_protects_alternate_spellings_of_reserved_paths_on_macos(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            masters = directory / "masters"
            write_playlist_master(masters / "Set" / "Set.csv", source_values={"Release Id": "321"})
            draft = directory / "choices.json"
            draft.write_text('{"publishers": {"spotify": {}}}', encoding="utf-8")
            cache = directory / "matches.json"
            reserved = directory / "spotify-publish-state.cache.json"
            outputs = (
                reserved.with_name(reserved.name.upper()),
                masters.with_name("MASTERS") / "Set" / "review.json",
            )
            for output in outputs:
                with self.subTest(output=output):
                    result, stdout, stderr = run_cli(masters, cache, "--prepare-map", str(draft), "--output", str(output))
                    self.assertEqual((result, stdout), (2, ""))
                    self.assertIn("protected", stderr)
                    self.assertFalse(output.exists())
            self.assertFalse(reserved.exists() or cache.exists())

    def test_review_rejects_duplicate_members_before_dispatching_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_playlist_master(directory / "Set" / "Set.csv")
            review = directory / "review.json"
            write_override_file(review)
            unique = review.read_text(encoding="utf-8")
            cache = directory / "matches.json"
            save_spotify_track_match_cache(cache, {})
            original_cache = cache.read_bytes()
            self.assertEqual(import_manual_match_overrides(review, directory, cache).planned_count, 1)

            for old, ambiguous in (
                ('"schema_version": 1', '"schema_version": 2, "schema_version": 1'),
                ('"spotify_uri": "spotify:track:manual-one"',
                 '"spotify_uri": "spotify:track:replaced", "spotify_uri": "spotify:track:manual-one"'),
            ):
                review.write_text(unique.replace(old, ambiguous), encoding="utf-8")
                for apply in (False, True):
                    with self.subTest(member=old, apply=apply), self.assertRaisesRegex(ValueError, "duplicate"):
                        import_manual_match_overrides(review, directory, cache, apply=apply)
                self.assertEqual(cache.read_bytes(), original_cache)

    def test_v2_requires_complete_typed_source_and_either_all_or_no_core_target_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_playlist_master(directory / "Set" / "Set.csv", source_values={"Release Id": "321"})
            review = directory / "review.json"
            cache = directory / "matches.json"
            selected = {**V2_SOURCE, "spotify_uri": V2_URI}
            for invalid in (
                {"release_id": 321}, {"album_name": ""}, {"spotify_uri": "spotify:album:no"},
                {"spotify_uri": f"{V2_URI} "},
                {"spotify_album_id": "orphan"}, {"spotify_track_name": "Partial"},
                {**V2_TARGET_LABELS, "spotify_artist_names": ["First Artist", 99]},
                {**V2_TARGET_LABELS, "spotify_album_name": None},
                {**V2_TARGET_LABELS, "spotify_album_id": []},
                {"unexpected": True},
            ):
                write_v2_review(review, {**selected, **invalid})
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "override 1"):
                    import_manual_match_overrides(review, directory, cache, apply=True)
                self.assertFalse(cache.exists())
            write_v2_review(review, selected, selected)
            with self.assertRaisesRegex(ValueError, "duplicates source"):
                import_manual_match_overrides(review, directory, cache, apply=True)
            write_v2_review(review)
            self.assertEqual(import_manual_match_overrides(review, directory, cache, apply=True).applied_count, 0)
            self.assertFalse(cache.exists())

    def test_v2_accepts_only_target_labels_from_the_exact_source_and_uri(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_playlist_master(directory / "Set" / "Set.csv", source_values={"Release Id": "321"})
            review = directory / "review.json"
            cache = directory / "matches.json"
            track = PlaylistTrack(playlist_name="Set", spotify_search_query="", **V2_SOURCE)
            key = spotify_track_match_key(track)
            write_v2_review(review, {**V2_SOURCE, "spotify_uri": V2_URI})
            for record in (
                {**V2_SOURCE, "match_status": MATCHED, "spotify_uri": V2_URI, **V2_TARGET_LABELS},
                {**V2_SOURCE, "match_status": UNMATCHED, "review_candidates": [
                    {**V2_TARGET_LABELS, "spotify_uri": V2_URI, "version_title_review_only": True},
                ]},
                {**V2_SOURCE, "track_name": "Source Track 星", "match_status": UNMATCHED, "review_candidates": [
                    {**V2_TARGET_LABELS, "spotify_uri": V2_URI},
                ]},
                {**V2_SOURCE, "match_status": "manual", "spotify_uri": "spotify:track:other", **V2_TARGET_LABELS},
            ):
                with self.subTest(record=record):
                    save_spotify_track_match_cache(cache, {key: record})
                    import_manual_match_overrides(review, directory, cache, apply=True, replace_existing=True)
                    stored = load_spotify_track_match_cache(cache)
                    selected_key, selected = track_match_cache_entry(track, stored)
                    assert selected is not None
                    self.assertEqual(selected["match_strategy"], "manual_uri_only")
                    self.assertTrue(V2_TARGET_LABELS.keys().isdisjoint(selected))
                    if selected_key != key:
                        self.assertEqual(stored[key], record)
                    cached = cached_track_match(track, stored)
                    assert cached is not None and cached.decision.candidate is not None
                    self.assertEqual(cached.decision.candidate.name, "")
                    self.assertEqual(cached.decision.candidate.artists, ())
                    self.assertFalse(cached.decision.candidate.has_complete_recording_metadata)

    def test_v2_preserves_reviewed_spelling_and_rejects_conflicting_target_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_playlist_master(directory / "Set" / "Set.csv", source_values={"Release Id": "321"})
            review = directory / "review.json"
            cache = directory / "matches.json"
            track = PlaylistTrack(playlist_name="Set", spotify_search_query="", **V2_SOURCE)
            key = spotify_track_match_key(track)
            source = {**V2_SOURCE, "spotify_uri": V2_URI}
            reviewed = {**source, **V2_TARGET_LABELS}
            del reviewed["spotify_album_id"]
            matched = {
                **source, **V2_TARGET_LABELS, "match_status": "manual",
                "spotify_track_name": "e\u0301LAN 星",
                "spotify_artist_names": ["second artist", "first artist"],
            }
            write_v2_review(review, reviewed)
            save_spotify_track_match_cache(cache, {key: matched})
            import_manual_match_overrides(review, directory, cache, apply=True)
            record = load_spotify_track_match_cache(cache)[key]
            for field, value in V2_TARGET_LABELS.items():
                self.assertEqual(record[field], value)
            self.assertNotEqual(record.get("match_strategy"), "manual_uri_only")

            write_v2_review(review, {**source, **V2_TARGET_LABELS})
            for conflict in (
                {"spotify_track_name": "Élan 月"}, {"spotify_artist_names": ["First Artist"]},
                {"spotify_album_name": "Another Album"}, {"spotify_album_id": "other-id"},
            ):
                save_spotify_track_match_cache(cache, {key: {**matched, **conflict}})
                original_cache = cache.read_bytes()
                for apply in (False, True):
                    with self.subTest(conflict=conflict, apply=apply), self.assertRaisesRegex(
                        ValueError, "override 1 .*conflicting Spotify",
                    ):
                        import_manual_match_overrides(review, directory, cache, apply=apply, replace_existing=True)
                self.assertEqual(cache.read_bytes(), original_cache)

            write_v2_review(review, source)
            save_spotify_track_match_cache(cache, {key: matched})
            import_manual_match_overrides(review, directory, cache, apply=True)
            updated = load_spotify_track_match_cache(cache)[key]
            self.assertEqual(updated["match_strategy"], "manual_uri_only")
            self.assertEqual(updated["spotify_track_name"], matched["spotify_track_name"])

    def test_preview_validates_override_without_writing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
            overrides_path = directory / "manual-matches.json"
            write_override_file(overrides_path)
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"

            summary = import_manual_match_overrides(
                overrides_path=overrides_path,
                playlist_output_directory=playlist_directory,
                match_cache_path=match_cache_path,
                apply=False,
            )
            cache_exists_after_preview = match_cache_path.exists()

        self.assertEqual(summary.planned_count, 1)
        self.assertEqual(summary.applied_count, 0)
        self.assertFalse(cache_exists_after_preview)

    def test_apply_writes_authoritative_manual_cache_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
            overrides_path = directory / "manual-matches.json"
            write_override_file(overrides_path)
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"

            summary = import_manual_match_overrides(
                overrides_path=overrides_path,
                playlist_output_directory=playlist_directory,
                match_cache_path=match_cache_path,
                apply=True,
                timestamp="2026-07-17T00:00:00Z",
            )
            payload = json.loads(match_cache_path.read_text(encoding="utf-8"))
            records = load_spotify_track_match_cache(match_cache_path)
            track = PlaylistTrack(
                playlist_name="Techno",
                release_id="release-1",
                album_name="Source Album",
                track_number="1",
                track_name="Source Track",
                artist_name="Source Artist",
                spotify_search_query="Source Artist Source Track Source Album",
            )
            cached = cached_track_match(track, records)

        self.assertEqual(summary.applied_count, 1)
        record = payload["matches"]["release-1|1|source artist|source album|source track"]
        self.assertEqual(record["match_status"], "manual")
        self.assertEqual(record["match_reason"], "manually selected Spotify match")
        self.assertEqual(record["spotify_uri"], "spotify:track:manual-one")
        self.assertEqual(record["spotify_album_id"], "spotify-album-one")
        self.assertEqual(record["matched_at"], "2026-07-17T00:00:00Z")
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.decision.spotify_uri, "spotify:track:manual-one")

        key = "release-1|1|source artist|source album|source track"
        for field, wrong_value in (
            ("release_id", "release-2"),
            ("track_number", "2"),
            ("artist_name", "Source Artist 月"),
            ("album_name", "Source Album 月"),
            ("track_name", "Source Track 月"),
        ):
            with self.subTest(cache_field=field):
                wrong_record = {**records[key], field: wrong_value}
                self.assertIsNone(cached_track_match(track, {key: wrong_record}, seen_at="later"))
                self.assertEqual(wrong_record["last_seen_at"], "2026-07-17T00:00:00Z")

    def test_rejects_override_for_absent_source_with_same_cache_key(self) -> None:
        for master_field, override_field in (
            ("Track Name", "track_name"),
            ("Artist Name", "artist_name"),
            ("Album Name", "album_name"),
        ):
            with self.subTest(field=master_field), tempfile.TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                write_playlist_master(directory / "Mix" / "Mix.csv", source_values={master_field: "Signal 月"})
                overrides_path = directory / "manual.json"
                write_override_file(overrides_path, source_values={override_field: "Signal 星"})
                cache_path = directory / "matches.json"

                for apply in (False, True):
                    with self.subTest(apply=apply), self.assertRaisesRegex(
                        ValueError, "override 1 does not match a generated playlist row",
                    ):
                        import_manual_match_overrides(overrides_path, directory, cache_path, apply=apply)
                self.assertFalse(cache_path.exists())

    def test_rejects_ambiguous_master_rows_without_partially_applying_overrides(self) -> None:
        for first_title, last_title in (("Signal 星", "Signal 月"), ("Signal 月", "Signal 星")):
            with self.subTest(first_title=first_title), tempfile.TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                write_playlist_master(directory / "First" / "First.csv", source_values={"Track Name": first_title})
                write_playlist_master(directory / "Last" / "Last.csv", source_values={"Track Name": last_title})
                write_playlist_master(directory / "Control" / "Control.csv")
                overrides_path = directory / "manual.json"
                write_override_file(overrides_path)
                payload = json.loads(overrides_path.read_text(encoding="utf-8"))
                payload["matches"].append({**payload["matches"][0], "track_name": "Signal 星"})
                overrides_path.write_text(json.dumps(payload), encoding="utf-8")
                cache_path = directory / "matches.json"
                save_spotify_track_match_cache(cache_path, {"existing": {"match_status": "unmatched"}})
                original_cache = cache_path.read_bytes()

                for apply in (False, True):
                    with self.subTest(apply=apply), self.assertRaisesRegex(ValueError, "override 2 .*conflicting"):
                        import_manual_match_overrides(
                            overrides_path, directory, cache_path, apply=apply, replace_existing=True,
                        )
                self.assertEqual(cache_path.read_bytes(), original_cache)

    def test_accepts_same_source_across_playlist_masters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for playlist_name, track_name in (
                ("First", "Signal 星 É"),
                ("Second", "Signal 星 É"),
                ("Third", "signal 星 e\u0301"),
            ):
                write_playlist_master(
                    directory / playlist_name / f"{playlist_name}.csv",
                    source_values={"Track Name": track_name},
                )
            overrides_path = directory / "manual.json"
            write_override_file(overrides_path, source_values={"track_name": "Signal 星 É"})
            cache_path = directory / "matches.json"

            summary = import_manual_match_overrides(overrides_path, directory, cache_path, apply=True)
            records = load_spotify_track_match_cache(cache_path)

        self.assertEqual(summary.applied_count, 1)
        self.assertEqual(list(records), ["release-1|1|source artist|source album|signal e"])

    def test_rejects_override_for_unknown_source_track(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
            overrides_path = directory / "manual-matches.json"
            write_override_file(overrides_path)
            payload = json.loads(overrides_path.read_text(encoding="utf-8"))
            payload["matches"][0]["track_name"] = "Unknown Track"
            overrides_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "does not match a generated playlist row"):
                import_manual_match_overrides(
                    overrides_path=overrides_path,
                    playlist_output_directory=playlist_directory,
                    match_cache_path=directory / "matches.json",
                    apply=False,
                )

    def test_rejects_non_track_spotify_uri(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
            overrides_path = directory / "manual-matches.json"
            write_override_file(overrides_path, spotify_uri="https://open.spotify.com/track/manual-one")

            with self.assertRaisesRegex(ValueError, "spotify_uri must be spotify:track:<identifier>"):
                import_manual_match_overrides(
                    overrides_path=overrides_path,
                    playlist_output_directory=playlist_directory,
                    match_cache_path=directory / "matches.json",
                    apply=False,
                )

    def test_rejects_malformed_track_identifiers_without_mutating_cache(self) -> None:
        invalid_spotify_uris = (
            "spotify:track:",
            "spotify:track:manual one",
            "spotify:track:manual:one",
        )
        for spotify_uri in invalid_spotify_uris:
            with self.subTest(spotify_uri=spotify_uri), tempfile.TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                playlist_directory = directory / "collection" / "playlists"
                write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
                overrides_path = directory / "manual-matches.json"
                write_override_file(overrides_path, spotify_uri=spotify_uri)
                match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
                match_cache_path.parent.mkdir(parents=True)
                match_cache_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "record_type": "spotify_track_match_cache",
                            "matches": {},
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                original_cache = match_cache_path.read_bytes()

                with self.assertRaisesRegex(ValueError, "spotify_uri must be spotify:track:<identifier>"):
                    import_manual_match_overrides(
                        overrides_path=overrides_path,
                        playlist_output_directory=playlist_directory,
                        match_cache_path=match_cache_path,
                        apply=True,
                        replace_existing=True,
                    )

                self.assertEqual(match_cache_path.read_bytes(), original_cache)

    def test_accepts_short_structurally_valid_placeholder_uri(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
            overrides_path = directory / "manual-matches.json"
            write_override_file(overrides_path, spotify_uri="spotify:track:x")
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"

            summary = import_manual_match_overrides(
                overrides_path=overrides_path,
                playlist_output_directory=playlist_directory,
                match_cache_path=match_cache_path,
                apply=True,
            )
            payload = json.loads(match_cache_path.read_text(encoding="utf-8"))

        record = payload["matches"]["release-1|1|source artist|source album|source track"]
        self.assertEqual(summary.applied_count, 1)
        self.assertEqual(record["spotify_uri"], "spotify:track:x")

    def test_cached_automatic_collision_requires_explicit_source_strings(self) -> None:
        source = PlaylistTrack("Mix", "release-1", "Source Album", "1", "Source Track", "Source Artist", "")
        for field in ("track_name", "artist_name", "album_name"):
            with self.subTest(field=field):
                ordinary = replace(source, **{field: ""})
                verified = replace(source, **{field: "月"})
                manual_key = spotify_track_match_key(verified)
                self.assertEqual(spotify_track_match_key(ordinary), manual_key)
                records: dict[str, dict[str, object]] = {manual_key: {
                    "release_id": verified.release_id, "track_number": verified.track_number,
                    "track_name": verified.track_name, "artist_name": verified.artist_name, "album_name": verified.album_name,
                    "match_status": "manual", "spotify_uri": "spotify:track:verified",
                }}
                self.assertTrue(cache_track_match(records, TrackMatchDecision(ordinary, UNMATCHED, "", "no candidates")))
                replay = cached_track_match(ordinary, records)
                self.assertIsNotNone(replay)
                assert replay is not None
                self.assertEqual(replay.decision.status, UNMATCHED)

                saved_record = records[replay.cache_key]
                records[replay.cache_key] = {key: value for key, value in saved_record.items() if key != field}
                self.assertIsNone(cached_track_match(ordinary, records))
                for malformed in (None, 0, [], "月"):
                    with self.subTest(saved_value=malformed):
                        records[replay.cache_key] = {**saved_record, field: malformed}
                        self.assertIsNone(cached_track_match(ordinary, records))

    def test_rebinding_manual_match_with_same_uri_requires_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            master = directory / "Mix" / "Mix.csv"
            overrides_path = directory / "manual.json"
            cache_path = directory / "matches.json"

            write_playlist_master(master, source_values={"Track Name": "Signal 星"})
            write_override_file(overrides_path, source_values={"track_name": "Signal 星"})
            import_manual_match_overrides(overrides_path, directory, cache_path, apply=True)
            same_source = import_manual_match_overrides(overrides_path, directory, cache_path, apply=True)
            self.assertEqual(same_source.applied_count, 1)

            write_playlist_master(master, source_values={"Track Name": "Signal 月"})
            write_override_file(overrides_path, source_values={"track_name": "Signal 月"})
            colliding_track = PlaylistTrack("Mix", "release-1", "Source Album", "1", "Signal 月", "Source Artist", "")
            records = load_spotify_track_match_cache(cache_path)
            self.assertTrue(cache_track_match(records, TrackMatchDecision(
                colliding_track, MATCHED, "spotify:track:earlier-automatic", "synthetic earlier search",
            )))
            save_spotify_track_match_cache(cache_path, records)
            original_cache = cache_path.read_bytes()
            for apply in (False, True):
                with self.subTest(apply=apply), self.assertRaisesRegex(ValueError, "use --replace-existing"):
                    import_manual_match_overrides(overrides_path, directory, cache_path, apply=apply)
            self.assertEqual(cache_path.read_bytes(), original_cache)

            result = import_manual_match_overrides(
                overrides_path, directory, cache_path, apply=True, replace_existing=True,
            )
            self.assertEqual(result.applied_count, 1)
            rebound = cached_track_match(colliding_track, load_spotify_track_match_cache(cache_path))
            assert rebound is not None
            self.assertEqual((rebound.decision.track.track_name, rebound.decision.spotify_uri),
                             ("Signal 月", "spotify:track:manual-one"))
            self.assertTrue(rebound.is_manual)

            write_playlist_master(master, source_values={"Track Name": "Signal 星"})
            write_override_file(overrides_path, source_values={"track_name": "Signal 星"})
            import_manual_match_overrides(overrides_path, directory, cache_path, apply=True, replace_existing=True)
            preserved = cached_track_match(colliding_track, load_spotify_track_match_cache(cache_path))
            self.assertEqual(preserved, rebound)

    def test_rebinding_partial_manual_match_requires_flag_for_each_conflicting_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            master = directory / "Mix" / "Mix.csv"
            overrides_path = directory / "manual.json"
            cache_path = directory / "matches.json"
            source = {
                "release_id": "release-1", "track_number": "1", "track_name": "Signal 西",
                "artist_name": "Artist 西", "album_name": "Album 西",
            }
            write_playlist_master(master, source_values={
                "Track Name": source["track_name"], "Artist Name": source["artist_name"], "Album Name": source["album_name"],
            })
            write_override_file(overrides_path, source_values=source)
            key = "release-1|1|artist|album|signal"
            manual_uri = "spotify:track:manual-one"
            unrelated = {"match_status": "unmatched", "track_name": "Other Synthetic Track"}

            for field, wrong_value, saved_uri in (
                ("release_id", "release-2", manual_uri), ("track_number", "2", manual_uri),
                ("track_name", "Signal 東", manual_uri), ("artist_name", "Artist 東", manual_uri),
                ("album_name", "Album 東", manual_uri),
                ("track_name", "Signal 東", ""), ("track_name", "Signal 東", None),
            ):
                with self.subTest(field=field, saved_uri=saved_uri):
                    record = {"match_status": "manual", field: wrong_value}
                    if saved_uri is not None:
                        record["spotify_uri"] = saved_uri
                    save_spotify_track_match_cache(cache_path, {key: record, "unrelated": unrelated})
                    before = cache_path.read_bytes()

                    for apply in (False, True):
                        with self.subTest(apply=apply), self.assertRaisesRegex(ValueError, "use --replace-existing"):
                            import_manual_match_overrides(overrides_path, directory, cache_path, apply=apply)
                    self.assertEqual(cache_path.read_bytes(), before)

                    preview = import_manual_match_overrides(overrides_path, directory, cache_path, replace_existing=True)
                    self.assertEqual((preview.planned_count, preview.applied_count), (1, 0))
                    self.assertEqual(cache_path.read_bytes(), before)
                    result = import_manual_match_overrides(
                        overrides_path, directory, cache_path, apply=True, replace_existing=True,
                    )
                    self.assertEqual(result.applied_count, 1)
                    updated = load_spotify_track_match_cache(cache_path)
                    self.assertEqual(updated[key][field], source[field])
                    self.assertEqual(updated[key]["spotify_uri"], manual_uri)
                    self.assertEqual(updated["unrelated"], unrelated)

    def test_reimport_binds_key_only_and_compatible_partial_manual_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            master = directory / "Mix" / "Mix.csv"
            overrides_path = directory / "manual.json"
            cache_path = directory / "matches.json"
            write_playlist_master(master, source_values={"Track Name": "Signal 西 É"})
            write_override_file(overrides_path, source_values={"track_name": "Signal 西 É"})
            key = "release-1|1|source artist|source album|signal e"
            compatible_fields = {"track_name": " SIGNAL  西 e\u0301 ", "release_id": " release-1 ", "album_name": "  "}

            for saved_fields in (
                {"spotify_uri": "spotify:track:manual-one"},
                {},
                {**compatible_fields, "spotify_uri": "spotify:track:manual-one"},
                {**compatible_fields, "spotify_uri": ""},
            ):
                with self.subTest(saved_fields=saved_fields):
                    record = {"match_status": "manual", **saved_fields}
                    save_spotify_track_match_cache(cache_path, {key: record})
                    before = cache_path.read_bytes()
                    preview = import_manual_match_overrides(overrides_path, directory, cache_path)
                    self.assertEqual((preview.planned_count, preview.applied_count), (1, 0))
                    self.assertEqual(cache_path.read_bytes(), before)

                    result = import_manual_match_overrides(overrides_path, directory, cache_path, apply=True)
                    self.assertEqual(result.applied_count, 1)
                    updated = load_spotify_track_match_cache(cache_path)[key]
                    self.assertEqual(updated["track_name"], "Signal 西 É")
                    self.assertEqual(updated["album_name"], "Source Album")

    def test_replacing_different_matched_record_requires_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            playlist_directory = directory / "collection" / "playlists"
            write_playlist_master(playlist_directory / "Techno" / "Techno.csv")
            overrides_path = directory / "manual-matches.json"
            write_override_file(overrides_path)
            match_cache_path = directory / "collection" / "cache" / "spotify-track-matches.cache.json"
            match_cache_path.parent.mkdir(parents=True)
            match_cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "spotify_track_match_cache",
                        "matches": {
                            "release-1|1|source artist|source album|source track": {
                                "release_id": "release-1",
                                "track_number": "1",
                                "artist_name": "Source Artist",
                                "album_name": "Source Album",
                                "track_name": "Source Track",
                                "match_status": "matched",
                                "match_reason": "track, artist, and album matched",
                                "matcher_version": MATCHER_VERSION,
                                "spotify_uri": "spotify:track:existing",
                                "spotify_track_name": "Source Track",
                                "spotify_artist_names": ["Source Artist"],
                                "spotify_album_name": "Source Album",
                                "matched_at": "2026-07-17T00:00:00Z",
                                "last_seen_at": "2026-07-17T00:00:00Z",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "use --replace-existing"):
                import_manual_match_overrides(
                    overrides_path=overrides_path,
                    playlist_output_directory=playlist_directory,
                    match_cache_path=match_cache_path,
                    apply=True,
                )

            summary = import_manual_match_overrides(
                overrides_path=overrides_path,
                playlist_output_directory=playlist_directory,
                match_cache_path=match_cache_path,
                apply=True,
                replace_existing=True,
            )

        self.assertEqual(summary.applied_count, 1)


if __name__ == "__main__":
    unittest.main()
