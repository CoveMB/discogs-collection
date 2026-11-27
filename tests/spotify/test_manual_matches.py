import csv
import json
import sys
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.manual_matches import import_manual_match_overrides
from publishers.spotify.match_cache import (
    MATCHER_VERSION,
    cache_track_match,
    cached_track_match,
    load_spotify_track_match_cache,
    save_spotify_track_match_cache,
    spotify_track_match_key,
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


class SpotifyManualMatchTests(unittest.TestCase):
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
