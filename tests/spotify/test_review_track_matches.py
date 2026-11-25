import csv
import io
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from discogs_playlist_exporter import ReleaseExportRecord, build_release_output_rows
from discogs_tracklists import release_tracklist_from_payload
from publishers.spotify import review_track_matches
from publishers.spotify.authorization_flow import DEFAULT_AUTHORIZE_SCOPES
from publishers.spotify.client import HttpRequest, HttpResponse
from publishers.spotify.match_cache import MATCHER_VERSION, cache_track_match, save_spotify_track_match_cache, spotify_track_match_key
from publishers.spotify.matching import AMBIGUOUS, UNMATCHED, PlaylistTrack, SpotifyTrackCandidate, TrackMatchDecision
from publishers.spotify.token_cache import SpotifyToken, save_spotify_token
from shared.tunemymusic import TUNEMYMUSIC_COLUMNS


RELEASE_ID = "321"
TRACK = PlaylistTrack("Fictional Shelf", RELEASE_ID, "Paper Coast", "1", "Night Signal", "M. Vale", "")
PRINTED_QUERY = 'track:"Night Signal" artist:"M. Vale"'
CANONICAL_QUERY = 'track:"Night Signal" artist:"Mara Vale"'


def artist(artist_id: int, name: str, variation: str = "") -> dict[str, object]:
    return {"id": artist_id, "name": name, "anv": variation}


def discogs_track(title: str, position: str, **fields: object) -> dict[str, object]:
    return {"type_": "track", "title": title, "position": position, **fields}


def spotify_hit(identifier: str, *artists: str, title: str = "Night Signal", album: str = "Harbour Sessions") -> dict[str, object]:
    return {
        "type": "track",
        "uri": f"spotify:track:{identifier}",
        "name": title,
        "artists": [{"name": name} for name in artists],
        "album": {"id": album.replace(" ", ""), "name": album},
    }


class SpotifyArtistReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "generated"
        self.cache = self.directory / "match-cache.json"
        self.token_cache = self.directory / "token-cache.json"
        self.env = self.directory / "settings.env"
        self.env.write_text("SPOTIFY_CLIENT_ID=fictional-client\nSPOTIFY_REDIRECT_URI=http://127.0.0.1:9876/callback\n")
        save_spotify_token(self.token_cache, SpotifyToken(
            "fictional-access", "", 4_000_000_000, " ".join(DEFAULT_AUTHORIZE_SCOPES), "Bearer",
        ))
        self.payload: dict[str, object] = {
            "id": int(RELEASE_ID),
            "title": "Paper Coast",
            "artists": [artist(81, "Mara Vale", "M. Vale")],
            "tracklist": [
                {"type_": "heading", "title": "Side One", "position": ""},
                discogs_track("Night Signal", "A1"),
            ],
        }
        self.search_responses: dict[str, object] = {}
        self.requests: list[HttpRequest] = []
        self.matches: dict[str, dict[str, object]] = {}

    def write_master(self, name: str, tracks: Sequence[PlaylistTrack]) -> None:
        self.write_master_rows(name, [{
            "Release Id": track.release_id,
            "Album Name": track.album_name,
            "Track Number": track.track_number,
            "Track Name": track.track_name,
            "Artist Name": track.artist_name,
            "Spotify Search Query": track.spotify_search_query,
        } for track in tracks])

    def write_master_rows(self, name: str, rows: Sequence[Mapping[str, str]]) -> None:
        path = self.root / name / f"{name}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as output:
            fieldnames: list[str] = list(TUNEMYMUSIC_COLUMNS)
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def cache_negative(self, track: PlaylistTrack, *candidates: SpotifyTrackCandidate) -> dict[str, object]:
        cache_track_match(self.matches, TrackMatchDecision(
            track=track, status=AMBIGUOUS if candidates else UNMATCHED, spotify_uri="",
            reason="Synthetic publisher assessment", review_candidates=candidates, search_queries=("old search",),
        ))
        return self.matches[spotify_track_match_key(track)]

    def transport(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.headers["Authorization"], "Bearer fictional-access")
        url = urlsplit(request.url)
        self.assertEqual((url.netloc, url.path), ("api.spotify.com", "/v1/search"))
        params = parse_qs(url.query)
        self.assertEqual(params["type"], ["track"])
        self.assertEqual(params["limit"], ["10"])
        query = params["q"][0]
        self.assertIn(query, self.search_responses)
        response = self.search_responses[query]
        if isinstance(response, HttpResponse):
            return response
        if isinstance(response, bytes):
            return HttpResponse(200, {}, response.decode("utf-8"))
        return HttpResponse(200, {}, json.dumps(response if isinstance(response, Mapping) else {"tracks": {"items": response}}))

    def run_review(self, *, code: int = 0) -> tuple[dict, int]:
        save_spotify_track_match_cache(self.cache, self.matches)
        before = {path: path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {"DISCOGS_TOKEN": "fictional-discogs"}, clear=True),
            patch("discogs_tracklists.http_get", return_value=json.dumps(self.payload)) as discogs,
            patch("publishers.spotify.client.urllib_transport", side_effect=self.transport),
            patch("publishers.spotify.session.refresh_access_token", side_effect=AssertionError("Unexpected OAuth refresh")),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = review_track_matches.main([
                "--release-id", RELEASE_ID, "--playlist-output-directory", str(self.root),
                "--match-cache", str(self.cache), "--env-file", str(self.env), "--token-cache", str(self.token_cache),
            ])
        self.assertEqual(result, code, stderr.getvalue())
        self.assertEqual({path: path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}, before)
        if discogs.called:
            self.assertEqual(discogs.call_args.args[0], "https://api.discogs.com/releases/321")
        return json.loads(stdout.getvalue()), discogs.call_count

    def test_canonical_search_finds_recording_absent_from_printed_search(self) -> None:
        source = {"release_id": RELEASE_ID}
        tracklist = release_tracklist_from_payload(RELEASE_ID, self.payload, source)
        rows, _ = build_release_output_rows(ReleaseExportRecord(source, tracklist))
        self.write_master_rows("Shelf A", rows)
        self.write_master_rows("Shelf B", rows)
        self.cache_negative(TRACK, SpotifyTrackCandidate(
            "spotify:track:oldcanonical", "Night Signal", ("Mara Vale",), "Obscure EP", "oldalbum",
        ))
        self.search_responses = {
            PRINTED_QUERY: [spotify_hit("printed", "M.Vale", album="Paper Coast")],
            CANONICAL_QUERY: [spotify_hit("canonical", "Mara Vale"), spotify_hit("printed", "M.Vale", album="Paper Coast"),
                              spotify_hit("wrongversion", "Mara Vale", title="Night Signal (Live)")],
        }

        report, discogs_calls = self.run_review()
        row, = report["rows"]
        self.assertEqual(discogs_calls, 1)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(row["source"]["track_number"], "1")
        self.assertEqual(row["source"]["discogs_position"], "A1")
        self.assertEqual(row["source"]["artist"], "M. Vale")
        self.assertEqual(row["playlists"], ["Shelf A", "Shelf B"])
        by_uri = {candidate["uri"]: candidate for candidate in row["candidates"]}
        self.assertEqual(set(by_uri), {"spotify:track:printed", "spotify:track:canonical", "spotify:track:oldcanonical"})
        printed, = by_uri["spotify:track:printed"]["observations"]
        canonical, = by_uri["spotify:track:canonical"]["observations"]
        old, = by_uri["spotify:track:oldcanonical"]["observations"]
        self.assertEqual(printed["origins"], ["printed_search", "canonical_search"])
        self.assertEqual(canonical["origins"], ["canonical_search"])
        self.assertEqual(canonical["album"], "Harbour Sessions")
        self.assertEqual(old["origins"], ["cached_review"])
        self.assertEqual(old["credit_evidence"][0]["spelling"], "canonical")
        self.assertEqual(printed["credit_evidence"][0]["spelling"], "printed")

    def test_mixed_credits_use_one_contributor_and_preserve_artist_boundaries(self) -> None:
        track = replace(TRACK, artist_name="M. Vale, Finch, Jr.")
        self.write_master("Shelf A", (track,))
        self.cache_negative(track)
        self.payload["artists"] = [artist(2, "Wrong Release Artist", "W.R.A.")]
        self.payload["tracklist"] = [{
            "type_": "index", "title": "Side One", "sub_tracks": [
                discogs_track("Night Signal", "A1", artists=[
                    artist(81, "Mara Vale", "M. Vale"),
                    artist(82, "Otto Finch", "Finch, Jr."),
                ]),
            ],
        }]
        self.search_responses = {
            PRINTED_QUERY: [spotify_hit("mixed", "M. Vale", "Otto Finch"), spotify_hit("extra", "M. Vale", "Otto Finch", "Guest")],
            CANONICAL_QUERY: [spotify_hit("othermixed", "Mara Vale", "Finch, Jr."),
                              spotify_hit("split", "Mara Vale", "Finch", "Jr."),
                              spotify_hit("repeated", "Mara Vale", "Mara Vale")],
        }

        report, _ = self.run_review()
        row, = report["rows"]
        self.assertEqual(len(self.requests), 2)
        by_uri = {candidate["uri"]: candidate["observations"][0] for candidate in row["candidates"]}
        self.assertEqual(set(by_uri), {"spotify:track:mixed", "spotify:track:othermixed"})
        self.assertEqual([entry["spelling"] for entry in by_uri["spotify:track:mixed"]["credit_evidence"]], ["printed", "canonical"])
        self.assertEqual(by_uri["spotify:track:othermixed"]["origins"], ["canonical_search"])

    def test_cache_holds_and_stale_or_cross_source_decisions_avoid_all_catalog_calls(self) -> None:
        rows = tuple(replace(TRACK, track_number=str(index)) for index in range(1, 7))
        self.write_master("Shelf A", rows)
        self.cache_negative(rows[0]).update({"match_status": "manual", "spotify_uri": ""})
        cache_track_match(self.matches, TrackMatchDecision(rows[1], "matched", "spotify:track:current", "Fictional match"))
        record = self.cache_negative(rows[2])
        record.update({"match_status": "matched", "spotify_uri": "spotify:track:stale",
                       "version_sensitive": True, "matcher_version": MATCHER_VERSION - 1})
        self.cache_negative(rows[3])["track_name"] = "Night Signal!"
        self.cache_negative(rows[4])["album_hint_recording_conflict"] = True
        self.env.unlink()

        report, discogs_calls = self.run_review()
        self.assertEqual(discogs_calls, 0)
        self.assertFalse(self.requests)
        self.assertEqual([row["status"] for row in report["rows"]],
                         ["manual_hold", "already_matched", "unassessed", "unassessed", "recording_conflict", "unassessed"])

    def test_colliding_manual_owner_protects_even_a_current_automatic_sidecar(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(TRACK).update({"match_status": "manual", "track_name": "Night Signal!", "spotify_uri": ""})
        self.assertTrue(cache_track_match(self.matches, TrackMatchDecision(TRACK, UNMATCHED, "", "Synthetic sidecar")))
        self.assertEqual(len(self.matches), 2)

        report, discogs_calls = self.run_review()
        self.assertEqual(discogs_calls, 0)
        self.assertEqual(report["rows"][0]["status"], "manual_hold")

    def test_distinct_generated_sources_sharing_import_key_are_held(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.write_master("Shelf B", (replace(TRACK, track_name="Night Signal!"),))
        self.cache_negative(TRACK)

        report, discogs_calls = self.run_review()
        self.assertEqual(discogs_calls, 0)
        self.assertEqual([row["status"] for row in report["rows"]], ["source_collision", "source_collision"])

    def test_wrong_pressing_malformed_credits_and_ambiguous_positions_do_not_search(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(TRACK)
        scenarios = (
            {"id": "321"}, {"id": True}, {"id": 322}, {"tracklist": {}},
            {"tracklist": [discogs_track("Night Signal", "A1", artists=[{"id": 81, "name": "Mara Vale", "anv": True}])]},
            {"tracklist": [discogs_track("Night Signal", "A1"), discogs_track("Different Song", "a1")]},
            {"tracklist": [discogs_track("Night Signal", "")]},
            {"tracklist": [{"type_": "index", "title": "Medley", "artists": [artist(9, "Another Artist")],
                            "sub_tracks": [discogs_track("Night Signal", "A1")]}]},
            *(
                {"tracklist": [{"type_": "index", "title": "Medley", "extraartists": invalid_credits,
                                "sub_tracks": [discogs_track("Night Signal", "A1")]}]}
                for invalid_credits in (None, {}, False)
            ),
        )
        original = self.payload.copy()
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.payload = {**original, **scenario}
                report, _ = self.run_review()
                self.assertEqual(report["rows"][0]["status"], "source_unverified")
        self.assertFalse(self.requests)

    def test_malformed_search_is_incomplete_and_conflicting_uri_observations_survive(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(TRACK)
        incomplete_hit = spotify_hit("partial", "Mara Vale")
        incomplete_hit["artists"] = [{"name": "Mara Vale"}, {"name": ""}]
        self.search_responses = {
            PRINTED_QUERY: [spotify_hit("shared", "M. Vale")],
            CANONICAL_QUERY: [spotify_hit("shared", "Mara Vale", title="Night Signal (Live)"), incomplete_hit],
        }

        report, _ = self.run_review(code=1)
        row, = report["rows"]
        self.assertEqual(row["status"], "incomplete")
        candidate, = row["candidates"]
        self.assertTrue(candidate["conflicting_observations"])
        self.assertEqual([item["title"] for item in candidate["observations"]], ["Night Signal", "Night Signal (Live)"])
        self.assertIsNone(candidate["observations"][1]["credit_evidence"])

    def test_empty_valid_page_is_distinct_from_missing_items(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(TRACK)
        self.search_responses = {PRINTED_QUERY: [], CANONICAL_QUERY: {"tracks": {}}}

        report, _ = self.run_review(code=1)
        row, = report["rows"]
        self.assertEqual([query["status"] for query in row["queries"]], ["complete", "incomplete"])
        self.assertFalse(row["candidates"])

    def test_repeated_titles_share_queries_without_merging_source_ordinals(self) -> None:
        second = replace(TRACK, track_number="2")
        self.write_master("Shelf A", (TRACK, second))
        self.cache_negative(TRACK)
        self.cache_negative(second)
        self.payload["tracklist"] = [discogs_track("Night Signal", "A1"), discogs_track("Night Signal", "B1")]
        self.search_responses = {PRINTED_QUERY: [], CANONICAL_QUERY: [spotify_hit("canonical", "Mara Vale")]}

        report, _ = self.run_review()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([row["source"]["discogs_position"] for row in report["rows"]], ["A1", "B1"])
        self.assertEqual([row["status"] for row in report["rows"]], ["suggestions", "suggestions"])

    def test_ignores_mixed_source_and_partial_cached_candidates(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        record = self.cache_negative(
            TRACK, SpotifyTrackCandidate("spotify:track:foreigncache", "Night Signal", ("Mara Vale",), "Other", "other"),
            SpotifyTrackCandidate("spotify:track:partialcache", "Night Signal", ("Mara Vale", ""), "Other", "other"),
        )
        raw = record["review_candidates"]
        assert isinstance(raw, list) and isinstance(raw[0], dict)
        raw[0]["release_id"] = "322"
        self.search_responses = {PRINTED_QUERY: [], CANONICAL_QUERY: []}

        report, _ = self.run_review()
        row, = report["rows"]
        self.assertEqual(row["status"], "no_suggestions")
        self.assertFalse(row["candidates"])

    def test_source_bound_cached_hits_can_contradict_fresh_hits_and_vice_versa(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(
            TRACK,
            SpotifyTrackCandidate("spotify:track:freshlead", "Night Signal (Live)", ("Mara Vale",), "Other", "other"),
            SpotifyTrackCandidate("spotify:track:cachelead", "Night Signal", ("Mara Vale",), "Other", "other"),
            SpotifyTrackCandidate("spotify:track:irrelevant", "Another Track", ("Mara Vale",), "Other", "other"),
        )
        self.search_responses = {
            PRINTED_QUERY: [],
            CANONICAL_QUERY: [
                spotify_hit("freshlead", "Mara Vale"),
                spotify_hit("cachelead", "Mara Vale", title="Night Signal (Live)"),
            ],
        }

        report, _ = self.run_review()
        row, = report["rows"]
        by_uri = {candidate["uri"]: candidate for candidate in row["candidates"]}
        self.assertEqual(set(by_uri), {"spotify:track:freshlead", "spotify:track:cachelead"})
        for uri, lead in (("spotify:track:freshlead", "canonical_search"), ("spotify:track:cachelead", "cached_review")):
            candidate = by_uri[uri]
            self.assertTrue(candidate["conflicting_observations"])
            self.assertEqual([item["origins"] for item in candidate["observations"]], [["cached_review"], ["canonical_search"]])
            self.assertEqual([item["origins"] for item in candidate["observations"] if item["credit_evidence"]], [[lead]])

    def test_same_title_requires_accurate_punctuation_unicode_and_complete_credits(self) -> None:
        track = replace(TRACK, track_name="À la Mer?", artist_name="J. Mérin")
        self.write_master("Shelf A", (track,))
        self.cache_negative(track)
        self.payload["artists"] = [artist(85, "Jean‐Luc Mérin", "J. Mérin")]
        self.payload["tracklist"] = [discogs_track(track.track_name, "A1")]
        self.search_responses = {
            'track:"À la Mer?" artist:"J. Mérin"': [],
            'track:"À la Mer?" artist:"Jean‐Luc Mérin"': [
                spotify_hit("formatting", "Jean Luc Mérin", title="À la Mer?"),
                spotify_hit("accent", "Jean Luc Merin", title="À la Mer?"),
                spotify_hit("dash", "Jean—Luc Mérin", title="À la Mer?"),
                spotify_hit("punctuation", "Jean Luc Mérin", title="À la Mer"),
                spotify_hit("titleaccent", "Jean Luc Mérin", title="A la Mer?"),
                spotify_hit("titlefeature", "Jean Luc Mérin", title="À la Mer? (feat. Guest)"),
            ],
        }

        report, _ = self.run_review()
        row, = report["rows"]
        self.assertEqual([candidate["uri"] for candidate in row["candidates"]], ["spotify:track:formatting"])

    def test_separate_featuring_credits_hold_the_source(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(TRACK)
        separately_credited_artist = {**artist(89, "Otto Finch"), "role": "Featuring [Vocals]"}
        original = self.payload.copy()
        for fields in (
            {"tracklist": [discogs_track("Night Signal", "A1", extraartists=[separately_credited_artist])]},
            {"extraartists": [{**separately_credited_artist, "tracks": "A1"}]},
        ):
            with self.subTest(fields=fields):
                self.payload = {**original, **fields}
                # A solo hit looks eligible if the extra Discogs credit is ignored.
                self.search_responses = {PRINTED_QUERY: [], CANONICAL_QUERY: [spotify_hit("solo", "Mara Vale")]}
                report, _ = self.run_review()
                row, = report["rows"]
                self.assertEqual(row["status"], "source_unverified")
                self.assertIn("featuring", row["reason"])
        self.assertFalse(self.requests)

    def test_malformed_extra_artist_credits_hold_the_source(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(TRACK)
        malformed_feature = {**artist(89, "Otto Finch"), "role": "Featuring"}
        original = self.payload.copy()
        for fields in (
            {"extraartists": malformed_feature},
            {"extraartists": [None]},
            {"tracklist": [discogs_track("Night Signal", "A1", extraartists=malformed_feature)]},
        ):
            with self.subTest(fields=fields):
                self.payload = {**original, **fields}
                self.search_responses = {PRINTED_QUERY: [], CANONICAL_QUERY: [spotify_hit("solo", "Mara Vale")]}
                report, _ = self.run_review()
                row, = report["rows"]
                self.assertEqual(row["status"], "source_unverified")
                self.assertIn("malformed separate artist credits", row["reason"])
        self.assertFalse(self.requests)

    def test_invalid_utf8_keeps_earlier_spotify_observations(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(TRACK)
        self.search_responses = {
            PRINTED_QUERY: [spotify_hit("printed", "M. Vale")],
            CANONICAL_QUERY: bytes([255]),
        }

        report, _ = self.run_review(code=1)
        row, = report["rows"]
        self.assertEqual(row["status"], "incomplete")
        self.assertEqual([query["status"] for query in row["queries"]], ["complete", "incomplete"])
        self.assertIn("decoded", row["queries"][1]["reason"])
        self.assertEqual([candidate["uri"] for candidate in row["candidates"]], ["spotify:track:printed"])

    def test_incomplete_same_uri_hit_preserves_verified_disagreement(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(TRACK)
        partial = spotify_hit("disputed", "Mara Vale", title="Night Signal (Live)")
        partial["artists"] = [{"name": "Mara Vale"}, {"name": None}]
        self.search_responses = {
            PRINTED_QUERY: [spotify_hit("disputed", "M. Vale")],
            CANONICAL_QUERY: [partial],
        }

        report, _ = self.run_review(code=1)
        row, = report["rows"]
        candidate, = row["candidates"]
        self.assertTrue(candidate["conflicting_observations"])
        _, incomplete = candidate["observations"]
        self.assertEqual(incomplete["origins"], ["canonical_search"])
        self.assertEqual(incomplete["title"], "Night Signal (Live)")
        self.assertFalse(incomplete["metadata_complete"])
        self.assertIsNone(incomplete["credit_evidence"])

    def test_partial_artist_credit_keeps_known_names_and_separate_origins(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(TRACK)
        contradictory = spotify_hit("conflict", "Otto Finch")
        contradictory["artists"] = [{"name": "Otto Finch"}, {"name": None}]
        consistent = spotify_hit("same", "M. Vale")
        consistent["artists"] = [{"name": "M. Vale"}, {"name": None}]
        self.search_responses = {
            PRINTED_QUERY: [spotify_hit("conflict", "M. Vale"), spotify_hit("same", "M. Vale")],
            CANONICAL_QUERY: [contradictory, consistent],
        }

        report, _ = self.run_review(code=1)
        row, = report["rows"]
        conflict, same = row["candidates"]
        self.assertTrue(conflict["conflicting_observations"])
        self.assertEqual(conflict["observations"][1]["artists"], ["Otto Finch"])
        self.assertFalse(same["conflicting_observations"])
        self.assertEqual([item["origins"] for item in same["observations"]], [["printed_search"], ["canonical_search"]])
        self.assertFalse(same["observations"][1]["metadata_complete"])

    def test_partial_hits_with_equal_display_keep_distinct_artist_evidence_in_either_order(self) -> None:
        track = replace(TRACK, artist_name="M. Vale, Otto Finch")
        self.write_master("Shelf A", (track,))
        self.cache_negative(track)
        self.payload["artists"] = [artist(81, "Mara Vale", "M. Vale"), artist(89, "Otto Finch")]
        complete_solo = spotify_hit("disputed", "M. Vale")
        complete_solo["album"] = {"name": "Harbour Sessions"}
        partial_credit = {**complete_solo, "artists": [{"name": "M. Vale"}, {"name": None}]}

        for replies in ((complete_solo, partial_credit), (partial_credit, complete_solo)):
            with self.subTest(replies=replies):
                self.search_responses = {
                    PRINTED_QUERY: list(replies),
                    CANONICAL_QUERY: [spotify_hit("disputed", "M. Vale", "Otto Finch")],
                }

                report, _ = self.run_review(code=1)
                candidate, = report["rows"][0]["candidates"]
                self.assertTrue(candidate["conflicting_observations"])
                self.assertEqual(len(candidate["observations"]), 3)
                self.assertEqual({item["artist_credits_complete"] for item in candidate["observations"][:2]}, {True, False})

    def test_empty_page_with_positive_total_is_incomplete(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(TRACK)
        self.search_responses = {
            PRINTED_QUERY: [],
            CANONICAL_QUERY: {"tracks": {"offset": 0, "total": 12, "items": []}},
        }

        report, _ = self.run_review(code=1)
        row, = report["rows"]
        self.assertEqual(row["status"], "incomplete")
        self.assertFalse(row["candidates"])

    def test_authentication_and_rate_limit_stop_later_queries_with_partial_report(self) -> None:
        self.write_master("Shelf A", (TRACK,))
        self.cache_negative(TRACK)
        for response, detail in ((HttpResponse(403, {}, "private response"), "authentication"),
                                 (HttpResponse(429, {"Retry-After": "7200"}, "private response"), "rate limit")):
            with self.subTest(detail=detail):
                self.requests.clear()
                self.search_responses = {PRINTED_QUERY: response}
                report, _ = self.run_review(code=1)
                row, = report["rows"]
                self.assertEqual(row["status"], "incomplete")
                self.assertEqual(len(self.requests), 1)
                self.assertIn(detail, row["queries"][0]["reason"].lower())
                self.assertEqual(row["queries"][1]["status"], "not_run")
                self.assertNotIn("private response", json.dumps(report))
