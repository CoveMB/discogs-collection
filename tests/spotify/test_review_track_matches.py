import argparse
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from discogs_playlist_exporter import ReleaseExportRecord, build_release_output_rows
from discogs_tracklists import release_tracklist_from_payload
from publishers.spotify import manual_matches, review_track_matches
from publishers.spotify.authorization_flow import DEFAULT_AUTHORIZE_SCOPES
from publishers.spotify.client import HttpRequest, HttpResponse
from publishers.spotify.match_cache import (
    MATCHER_VERSION, cache_track_match, record_cached_recording_conflict, save_spotify_track_match_cache,
    spotify_track_match_key,
)
from publishers.spotify.matching import AMBIGUOUS, UNMATCHED, PlaylistTrack, SpotifyTrackCandidate, TrackMatchDecision
from publishers.spotify.publish_playlist import publish_spotify_playlists
from publishers.spotify.token_cache import SpotifyToken, save_spotify_token
from tests.spotify.test_publish_playlist import PublishingSpotifyClient
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
        if url.path.startswith("/v1/tracks/"):
            self.assertEqual(url.netloc, "api.spotify.com")
            response = self.search_responses.get(url.path)
            return HttpResponse(200, {}, json.dumps(response)) if response else HttpResponse(404, {}, "{}")
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
            patch.object(review_track_matches, "MusicBrainzClient", side_effect=AssertionError("Unexpected recording lookup")),
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

    def recording_fixture(self, *, alias: bool = False) -> tuple[PlaylistTrack, dict, dict, dict]:
        track = TRACK if alias else replace(TRACK, artist_name="Mara Vale")
        self.write_master("Fictional Shelf", (track,))
        mbid = "00000000-1111-4111-8111-00000000000"
        relationship = "4a78823c-1c53-4176-a5f3-58026c76f2bc"
        resource = "https://www.discogs.com/release/321"
        code = "ZZABC2600001"
        credits = [{"name": track.artist_name, "artist": {"id": mbid + "1", "name": "Mara Vale"}}]
        self.payload.update(
            artists=[artist(81, "Mara Vale", "M. Vale" if alias else "")],
            tracklist=[discogs_track("Night Signal", "1", duration="3:02")],
            formats=[{"name": "CD", "qty": "1"}], year=2026,
            identifiers=[{"type": "Barcode", "value": "0 12345 67890 5"}],
            labels=[{"name": "Paper Records", "catno": "PAP-01"}],
        )
        link = {"resource": resource, "relations": [{
            "type-id": relationship, "target-type": "release", "direction": "backward",
            "ended": False, "release": {"id": mbid + "2"},
        }]}
        pressing = {
            "id": mbid + "2", "title": "Paper Coast", "date": "2026-05-03",
            "barcode": "0012345678905",
            "label-info": [{"catalog-number": "PAP-01", "label": {"name": "Paper Records"}}],
            "relations": [{"type-id": relationship, "target-type": "url", "direction": "forward",
                           "ended": False, "url": {"resource": resource}}],
            "media": [{"position": 1, "format": "CD", "track-count": 1, "tracks": [{
                "position": 1, "number": "1", "title": "Night Signal", "length": 182100,
                "artist-credit": credits,
                "recording": {"id": mbid + "3", "title": "Night Signal", "artist-credit": credits,
                              "isrcs": [code], "length": 190000},
            }]}],
        }
        reverse = {"isrc": code, "recordings": [{"id": mbid + "3"}]}
        return track, link, pressing, reverse

    def run_recording_review(self, link: dict, pressing: dict, reverse: dict, *,
                             output: Path | None = None, code: int = 0,
                             failures: dict[str, list[int]] | None = None,
                             other_recordings: Sequence[dict] = ()) -> tuple[dict, list[str]]:
        proposal_path = output if output is not None else self.directory / "proposals.json"
        self.proposals = proposal_path
        save_spotify_track_match_cache(self.cache, self.matches)
        stdout, stderr = io.StringIO(), io.StringIO()
        before = {path: path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}
        paths: list[str] = []
        isrc_responses = {f"/ws/2/isrc/{item['isrc']}": item for item in (reverse, *other_recordings)}

        def musicbrainz(request: Request, *, timeout: float) -> io.BytesIO:
            url = urlsplit(request.full_url)
            self.assertEqual((url.scheme, url.netloc), ("https", "musicbrainz.org"))
            self.assertGreater(timeout, 0)
            self.assertIn("Discogs", request.get_header("User-agent") or "")
            query = parse_qs(url.query)
            self.assertEqual(query["fmt"], ["json"])
            paths.append(url.path)
            statuses = failures.get(url.path, []) if failures else []
            if statuses:
                raise HTTPError(request.full_url, statuses.pop(0), "Synthetic response", Message(), None)
            if url.path == "/ws/2/url":
                self.assertEqual(query["resource"], ["https://www.discogs.com/release/321"])
                self.assertEqual(query["inc"], ["release-rels"])
                body = link
            elif url.path.startswith("/ws/2/release/"):
                self.assertEqual(set(query["inc"][0].split("+")),
                                 {"recordings", "artist-credits", "isrcs", "labels", "media", "url-rels"})
                self.assertEqual(url.path, f"/ws/2/release/{pressing['id']}")
                body = pressing
            elif url.path in isrc_responses:
                body = isrc_responses[url.path]
            else:
                self.fail(f"Unexpected request path {url.path}")
            return io.BytesIO(json.dumps(body).encode())

        with (
            patch.dict(os.environ, {"DISCOGS_TOKEN": "fictional-discogs"}, clear=True),
            patch("discogs_tracklists.http_get", return_value=json.dumps(self.payload)) as discogs,
            patch("urllib.request.OpenerDirector.open", side_effect=musicbrainz),
            patch("publishers.spotify.musicbrainz_recordings.DiscogsRateLimiter.wait_before_request"),
            patch("publishers.spotify.client.urllib_transport", side_effect=self.transport),
            patch("publishers.spotify.session.refresh_access_token", side_effect=AssertionError("Unexpected OAuth refresh")),
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            result = review_track_matches.main([
                "--release-id", RELEASE_ID, "--playlist-output-directory", str(self.root),
                "--match-cache", str(self.cache), "--env-file", str(self.env), "--token-cache", str(self.token_cache),
                "--proposals", str(proposal_path),
            ])
        self.assertEqual(result, code, stderr.getvalue())
        if not stdout.getvalue():
            discogs.assert_not_called()
        after = {path: path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}
        if proposal_path not in before:
            after.pop(proposal_path, None)
        self.assertEqual(after, before)
        return json.loads(stdout.getvalue()) if stdout.getvalue() else {}, paths

    def set_isrc_hits(self, code: str, *hits: dict[str, object], **paging: object) -> None:
        self.search_responses[f"isrc:{code}"] = {"tracks": {
            "offset": 0, "total": len(hits), "next": None, "items": hits, **paging,
        }}

    def recording_hit(self, identifier: str, code: str, *, album: str = "Field Notes") -> dict[str, object]:
        return {**spotify_hit(identifier, "Mara Vale", album=album),
                "id": identifier, "duration_ms": 182500, "is_playable": True, "external_ids": {"isrc": code}}

    def seed_recording_tie(self, track: PlaylistTrack) -> None:
        self.cache_negative(track,
            SpotifyTrackCandidate("spotify:track:studio", "Night Signal", ("Mara Vale",), "Field Notes", "FieldNotes"),
            SpotifyTrackCandidate("spotify:track:other", "Night Signal", ("Mara Vale",), "Other Disc", "OtherDisc",
                                  isrc="ZZABC2600099"),
        )

    def test_recording_proposal_for_cached_tie_without_artist_variation_previews_in_importer(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        self.write_master("Second Shelf", (track,))
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(len(paths), 3)
        self.assertEqual(len(self.requests), 1)
        row, = report["rows"]
        self.assertEqual(row["source"]["discogs_position"], "1")
        self.assertEqual(row["status"], "proposal")
        self.assertEqual(set(row["playlists"]), {"Fictional Shelf", "Second Shelf"})
        result = manual_matches.import_manual_match_overrides(self.proposals, self.root, self.cache)
        self.assertEqual((result.planned_count, result.applied_count), (1, 0))
        artifact = json.loads(self.proposals.read_text())
        self.assertEqual(artifact["matches"][0]["spotify_uri"], "spotify:track:studio")
        self.assertEqual(artifact["matches"][0]["spotify_album_name"], "Field Notes")
        self.assertEqual(self.proposals.stat().st_mode & 0o777, 0o600)

    def test_single_candidate_on_another_album_produces_reviewable_override(self) -> None:
        track, link, pressing, reverse = self.recording_fixture(alias=True)
        self.cache_negative(track)
        hit = self.recording_hit("studio", reverse["isrc"])
        self.search_responses[CANONICAL_QUERY] = [hit]
        self.set_isrc_hits(reverse["isrc"], hit)
        for both_spellings in (False, True):
            with self.subTest(both_spellings=both_spellings):
                self.requests.clear()
                self.search_responses[PRINTED_QUERY] = [hit] if both_spellings else []
                report, paths = self.run_recording_review(
                    link, pressing, reverse, output=self.directory / f"proposals-{both_spellings}.json",
                )
                self.assertEqual(report["rows"][0]["status"], "proposal")
                self.assertEqual(len(paths), 3)
                self.assertEqual(len(self.requests), 3)
                row, = json.loads(self.proposals.read_text())["matches"]
                self.assertEqual(row["spotify_uri"], "spotify:track:studio")
                self.assertEqual(row["spotify_album_name"], "Field Notes")
                preview = manual_matches.import_manual_match_overrides(self.proposals, self.root, self.cache)
                self.assertEqual((preview.planned_count, preview.applied_count), (1, 0))

    def test_recording_proposal_verifies_single_discovered_alias(self) -> None:
        track, link, pressing, reverse = self.recording_fixture(alias=True)
        self.cache_negative(track)
        self.search_responses[PRINTED_QUERY] = []
        self.search_responses[CANONICAL_QUERY] = [spotify_hit("studio", "Mara Vale", album="Field Notes")]
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(len(paths), 3)
        self.assertEqual(report["rows"][0]["status"], "proposal")

    def test_recording_proposal_verifies_singleton_with_optional_mark_and_no_artist_alias(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        track = replace(track, track_name="Night Signál")
        self.write_master("Fictional Shelf", (track,))
        self.payload["tracklist"] = [discogs_track(track.track_name, "1", duration="3:02")]
        mb_track = pressing["media"][0]["tracks"][0]
        mb_track["title"] = mb_track["recording"]["title"] = track.track_name
        self.cache_negative(track, SpotifyTrackCandidate(
            "spotify:track:studio", "Night Signal", ("Mara Vale",), "Field Notes", "FieldNotes",
        ))
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(len(paths), 3)
        row, = report["rows"]
        self.assertEqual(row["status"], "proposal")
        self.assertEqual(row["recording_proof"]["chosen"]["uri"], "spotify:track:studio")
        self.assertEqual(manual_matches.import_manual_match_overrides(self.proposals, self.root, self.cache).planned_count, 1)

    def test_recording_proposal_verifies_single_exact_title_and_artist(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.cache_negative(track, SpotifyTrackCandidate(
            "spotify:track:studio", "Night Signal", ("Mara Vale",), "Field Notes", "FieldNotes",
        ))
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(len(paths), 3)
        self.assertEqual(report["rows"][0]["status"], "proposal")

    def test_recording_review_checks_missing_cross_album_evidence_once_per_uri(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        self.write_master("Second Shelf", (track,))
        link["relations"] = []
        self.search_responses["/v1/tracks/studio"] = self.recording_hit("studio", reverse["isrc"])
        self.search_responses["/v1/tracks/other"] = self.recording_hit("other", "ZZABC2600099", album="Other Disc")
        report, _ = self.run_recording_review(link, pressing, reverse)
        self.assertCountEqual([urlsplit(request.url).path for request in self.requests],
                              ["/v1/tracks/studio", "/v1/tracks/other"])
        row, = report["rows"]
        self.assertEqual(row["status"], "recording_unverified")
        self.assertTrue(any("track_lookup" in observation["origins"] and observation["is_playable"]
                            for result in row["candidates"] for observation in result["observations"]))
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_holds_single_candidate_when_code_verifies_a_different_uri(self) -> None:
        track, link, pressing, reverse = self.recording_fixture(alias=True)
        self.cache_negative(track)
        self.search_responses[PRINTED_QUERY] = []
        self.search_responses[CANONICAL_QUERY] = [spotify_hit("studio", "Mara Vale", album="Field Notes")]
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("other", reverse["isrc"]))
        for new_manual_owner in (False, True):
            with self.subTest(new_manual_owner=new_manual_owner):
                self.requests.clear()
                latest = deepcopy(self.matches)
                if new_manual_owner:
                    latest[spotify_track_match_key(track)]["match_status"] = "manual"
                with patch.object(
                    review_track_matches, "load_spotify_track_match_cache", side_effect=(self.matches, latest),
                ):
                    report, paths = self.run_recording_review(
                        link, pressing, reverse, output=self.directory / f"wrong-uri-{new_manual_owner}.json",
                    )
                self.assertEqual((len(paths), len(self.requests)), (3, 3))
                row, = report["rows"]
                self.assertEqual(row["status"], "recording_unverified")
                self.assertEqual(row["reason"], "The complete Spotify ISRC result does not verify an unresolved candidate.")
                self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_read_only_review_checks_known_exact_tie_even_without_a_proposal_file(self) -> None:
        track, _, _, reverse = self.recording_fixture()
        self.cache_negative(
            track, SpotifyTrackCandidate("spotify:track:studio", "Night Signal", ("Mara Vale",), "", ""),
            SpotifyTrackCandidate("spotify:track:other", "Night Signal", ("Mara Vale",), "Other Disc", "OtherDisc"),
        )
        self.search_responses["/v1/tracks/studio"] = self.recording_hit("studio", reverse["isrc"])
        self.search_responses["/v1/tracks/other"] = self.recording_hit("other", "ZZABC2600099", album="Other Disc")
        report, _ = self.run_review()
        self.assertCountEqual([urlsplit(request.url).path for request in self.requests],
                              ["/v1/tracks/studio", "/v1/tracks/other"])
        row, = report["rows"]
        self.assertEqual(row["status"], "suggestions")
        self.assertCountEqual([item["uri"] for item in row["candidates"]], ["spotify:track:studio", "spotify:track:other"])

    def test_read_only_alias_review_retains_the_missing_track_lookup_diagnostic(self) -> None:
        track, _, _, _ = self.recording_fixture(alias=True)
        self.seed_recording_tie(track)
        self.search_responses.update({PRINTED_QUERY: [], CANONICAL_QUERY: []})
        report, _ = self.run_review()
        self.assertCountEqual([urlsplit(request.url).path for request in self.requests],
                              ["/v1/search", "/v1/search", "/v1/tracks/studio", "/v1/tracks/other"])
        row, = report["rows"]
        self.assertEqual(row["status"], "suggestions")
        self.assertIn("track lookup lacked complete", row["reason"])

    def test_review_keeps_conflicting_search_observations_when_track_lookups_are_unnecessary(self) -> None:
        track, _, _, reverse = self.recording_fixture()
        code = reverse["isrc"]
        row = review_track_matches.ReviewRow({}, ["Fictional Shelf"])
        review_track_matches.bind_discogs_sources(self.payload, RELEASE_ID, ((track, row),))
        first = SpotifyTrackCandidate("spotify:track:studio", "Night Signal", ("Mara Vale",),
                                      "Field Notes", "FieldNotes", code, True)
        other = replace(first, uri="spotify:track:other", album_name="Other Disc", album_id="OtherDisc")
        evidence = review_track_matches.CandidateEvidence((first, other))
        search = review_track_matches.SpotifySearch(argparse.Namespace())
        search.pages["other query"] = review_track_matches.RawSearchPage({
            "items": [{**self.recording_hit("studio", code), "name": "Night Signal (Live)"}]
        })
        with patch.object(search, "get_track", side_effect=AssertionError("Unnecessary track lookup")):
            review_track_matches.report_incomplete_recording_tie(track, row, evidence, search)
        result = next(candidate for candidate in row.candidates if candidate["uri"] == first.uri)
        self.assertTrue(result["conflicting_observations"])

    def test_recording_proposal_rejects_reverse_isrc_clash(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.cache_negative(track, SpotifyTrackCandidate(
            "spotify:track:studio", "Night Signal", ("Mara Vale",), "Field Notes", "FieldNotes",
        ))
        reverse["recordings"].append({"id": "00000000-1111-4111-8111-000000000004"})
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(len(paths), 3)
        self.assertEqual(self.requests, [])
        row, = report["rows"]
        self.assertEqual(row["status"], "recording_unverified")
        self.assertIn("reverse MusicBrainz ISRC lookup", row["reason"])
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_single_candidate_without_proposals_only_runs_spelling_searches(self) -> None:
        track, _, _, reverse = self.recording_fixture(alias=True)
        self.cache_negative(track)
        self.search_responses[PRINTED_QUERY] = []
        self.search_responses[CANONICAL_QUERY] = [self.recording_hit("studio", reverse["isrc"])]
        report, discogs_calls = self.run_review()
        self.assertEqual((discogs_calls, len(self.requests)), (1, 2))
        row, = report["rows"]
        self.assertEqual(row["status"], "suggestions")
        candidate, = row["candidates"]
        self.assertEqual(candidate["uri"], "spotify:track:studio")
        self.assertNotIn("recording_proof", row)

    def test_recording_proposal_skips_catalog_proof_without_eligible_uris(self) -> None:
        track, link, pressing, reverse = self.recording_fixture(alias=True)
        self.cache_negative(track)
        self.search_responses[PRINTED_QUERY] = []
        self.search_responses[CANONICAL_QUERY] = [spotify_hit("cover", "Another Artist")]
        with patch.object(review_track_matches, "MusicBrainzClient", side_effect=AssertionError("Unexpected recording lookup")):
            report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual((paths, len(self.requests)), ([], 2))
        self.assertEqual(report["rows"][0]["status"], "no_suggestions")
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_respects_prior_status_for_cached_single_candidate_without_variation(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        record = self.cache_negative(track, SpotifyTrackCandidate(
            "spotify:track:studio", "Night Signal", ("Mara Vale",), "Field Notes", "FieldNotes",
        ))
        record["match_status"] = UNMATCHED
        with patch.object(review_track_matches, "MusicBrainzClient", side_effect=AssertionError("Unexpected recording lookup")):
            report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual((paths, self.requests), ([], []))
        self.assertEqual(report["rows"][0]["status"], "no_variation")
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

        record["match_status"] = AMBIGUOUS
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        report, paths = self.run_recording_review(link, pressing, reverse, output=self.directory / "ambiguous.json")
        self.assertEqual((len(paths), len(self.requests)), (3, 1))
        self.assertEqual(report["rows"][0]["status"], "proposal")
        row, = json.loads(self.proposals.read_text())["matches"]
        self.assertEqual(row["spotify_uri"], "spotify:track:studio")

    def test_recording_proposal_rejects_incomplete_or_unplayable_isrc_page(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        for incomplete, playable in ((True, True), (False, None)):
            with self.subTest(incomplete=incomplete):
                hit = self.recording_hit("studio", reverse["isrc"])
                hit["is_playable"] = playable
                self.set_isrc_hits(reverse["isrc"], hit, total=2 if incomplete else 1)
                report, _ = self.run_recording_review(link, pressing, reverse, code=int(incomplete))
                self.assertNotEqual(report["rows"][0]["status"], "proposal")
                self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])
                self.proposals.unlink()

    def test_recording_proposal_for_alias_revealed_tie_prefers_original_album(self) -> None:
        track, link, pressing, reverse = self.recording_fixture(alias=True)
        self.cache_negative(track)
        compilation = self.recording_hit("Acompile", reverse["isrc"])
        original = self.recording_hit("Zoriginal", reverse["isrc"], album=track.album_name)
        live = self.recording_hit("LiveTake", "ZZABC2600099", album="Live at Harbor Hall")
        self.search_responses[PRINTED_QUERY] = [original, live]
        self.search_responses[CANONICAL_QUERY] = [compilation]
        self.set_isrc_hits(reverse["isrc"], compilation, original)
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(len(paths), 3)
        self.assertEqual(len(self.requests), 3)
        proof = report["rows"][0]["recording_proof"]
        self.assertEqual(proof["chosen"]["uri"], "spotify:track:Zoriginal")
        self.assertEqual(proof["alternatives"][0]["uri"], "spotify:track:Acompile")
        self.assertEqual(proof["chosen"]["isrc"], proof["source_isrc"])
        self.assertEqual(proof["musicbrainz_track_duration_ms"], 182100)
        self.assertEqual(proof["chosen"]["duration_ms"], 182500)

    def test_recording_proposal_aligns_printed_sides_to_two_vinyl_media(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        track = replace(track, track_number="3")
        self.write_master("Fictional Shelf", (track,))
        self.seed_recording_tie(track)
        self.payload["formats"] = [{"name": "Vinyl", "qty": "2"}]
        titles = ("Paper Boat", "Running Tide", track.track_name, "Foam")
        positions = ("A1", "B1", "C1", "D1")
        self.payload["tracklist"] = [
            discogs_track(title, position, duration="3:02")
            for title, position in zip(titles, positions, strict=True)
        ]
        exemplar = pressing["media"][0]["tracks"][0]
        tracks = [{
            **exemplar, "number": position, "position": index % 2 + 1, "title": title,
            "recording": {
                **exemplar["recording"], "title": title, "isrcs": [reverse["isrc"]] if index == 2 else [],
                "id": f"00000000-1111-4111-8111-0000000000{20 + index}",
            },
        } for index, (title, position) in enumerate(zip(titles, positions, strict=True))]
        reverse["recordings"] = [{"id": tracks[2]["recording"]["id"]}]
        pressing["media"] = [
            {"position": 1, "format": '12" Vinyl', "track-count": 2, "tracks": tracks[:2]},
            {"position": 2, "format": '12" Vinyl', "track-count": 2, "tracks": tracks[2:]},
        ]
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        report, _ = self.run_recording_review(link, pressing, reverse)
        row, = report["rows"]
        self.assertEqual((row["source"]["track_number"], row["source"]["discogs_position"]), ("3", "C1"))
        self.assertEqual(row["status"], "proposal")
        self.assertEqual(row["recording_proof"]["musicbrainz_medium"], 2)
        self.assertEqual(row["recording_proof"]["musicbrainz_track_number"], "C1")

        self.requests.clear()
        pressing["media"][1]["tracks"].reverse()
        report, paths = self.run_recording_review(link, pressing, reverse, output=self.directory / "swapped.json")
        self.assertEqual(len(paths), 2)
        self.assertEqual(self.requests, [])
        self.assertEqual(report["rows"][0]["status"], "recording_unverified")

    def test_recording_proposal_holds_live_recording_under_generic_track_title(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        pressing["media"][0]["tracks"][0]["recording"]["title"] = "Night Signal (Live)"
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(len(paths), 2)
        self.assertEqual(self.requests, [])
        self.assertEqual(report["rows"][0]["status"], "recording_unverified")
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_holds_same_album_uris_and_repeated_conflicting_raw_uris(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        initial = self.recording_hit("studio", reverse["isrc"])
        for index, (second, reason) in enumerate((
            (self.recording_hit("other", reverse["isrc"]), "same Spotify album ID"),
            ({**initial, "duration_ms": 182600}, "contradictory ISRC observations"),
            ({**initial, "duration_ms": 180099}, "contradicts the source"),
        )):
            with self.subTest(reason=reason):
                self.set_isrc_hits(reverse["isrc"], initial, second)
                report, _ = self.run_recording_review(link, pressing, reverse, output=self.directory / f"held-{index}.json")
                self.assertIn(reason, report["rows"][0]["reason"])
                self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_holds_a_conflicting_repeat_from_discovery(self) -> None:
        track, link, pressing, reverse = self.recording_fixture(alias=True)
        self.cache_negative(track)
        result = self.recording_hit("Acompile", reverse["isrc"])
        self.search_responses[PRINTED_QUERY] = [result]
        self.search_responses[CANONICAL_QUERY] = [
            {**result, "duration_ms": 182600},
            self.recording_hit("live", "ZZABC2600099", album="Live at Harbor Hall"),
        ]
        self.set_isrc_hits(reverse["isrc"], result)
        report, _ = self.run_recording_review(link, pressing, reverse)
        self.assertIn("contradicts Spotify", report["rows"][0]["reason"])
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_holds_key_only_manual_record_without_opening_catalogs(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.matches[spotify_track_match_key(track)] = {"match_status": "manual", "spotify_uri": ""}
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(paths, [])
        self.assertEqual(self.requests, [])
        self.assertEqual(report["rows"][0]["status"], "manual_hold")
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_protects_dangling_links_and_missing_default_cache_path(self) -> None:
        _, link, pressing, reverse = self.recording_fixture()
        dangling = self.directory / "dangling.json"
        dangling.symlink_to(self.directory / "never-create.json")
        for path in (dangling, review_track_matches.DEFAULT_SPOTIFY_MATCH_CACHE_PATH.resolve()):
            with self.subTest(path=path):
                report, paths = self.run_recording_review(link, pressing, reverse, output=path, code=1)
                self.assertEqual((report, paths, self.requests), ({}, [], []))
        self.assertTrue(dangling.is_symlink())
        self.assertFalse(dangling.exists())

    def test_recording_proposal_does_not_bind_a_source_collision_in_another_master(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        self.write_master("Second Shelf", (replace(track, track_name="Night-Signal"),))
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(paths, [])
        self.assertEqual(self.requests, [])
        self.assertEqual({row["status"] for row in report["rows"]}, {"source_collision"})
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_retries_transient_musicbrainz_fault_once(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        isrc_path = f"/ws/2/isrc/{reverse['isrc']}"
        report, paths = self.run_recording_review(link, pressing, reverse, failures={isrc_path: [503]})
        self.assertEqual(paths.count(isrc_path), 2)
        self.assertEqual(report["rows"][0]["status"], "proposal")

    def test_recording_proposal_holds_an_unreadable_second_plausible_pressing(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        second = "00000000-1111-4111-8111-000000000099"
        link["relations"].append({**link["relations"][0], "release": {"id": second}})
        second_path = f"/ws/2/release/{second}"
        report, paths = self.run_recording_review(link, pressing, reverse, failures={second_path: [429]}, code=1)
        self.assertEqual(paths[-1], second_path)
        self.assertEqual(self.requests, [])
        self.assertEqual(report["rows"][0]["status"], "incomplete")
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def two_track_recording_fixture(self) -> tuple[PlaylistTrack, PlaylistTrack, dict, dict, dict, dict]:
        track, link, pressing, first_code = self.recording_fixture()
        second_track = replace(track, track_number="2", track_name="Late Tide")
        self.write_master("Fictional Shelf", (track, second_track))
        self.payload["tracklist"] = [
            discogs_track(track.track_name, "1", duration="3:02"),
            discogs_track(second_track.track_name, "2", duration="3:02"),
        ]
        medium = pressing["media"][0]
        first = medium["tracks"][0]
        second = {**first, "number": "2", "position": 2, "title": second_track.track_name,
                  "recording": {**first["recording"], "title": second_track.track_name,
                                "id": "00000000-1111-4111-8111-000000000004", "isrcs": ["ZZABC2600002"]}}
        medium["track-count"] = 2
        medium["tracks"].append(second)
        second_code = {"isrc": second["recording"]["isrcs"][0], "recordings": [{"id": second["recording"]["id"]}]}
        self.seed_recording_tie(track)
        self.cache_negative(second_track, *(
            SpotifyTrackCandidate(f"spotify:track:{uri}", second_track.track_name, ("Mara Vale",), album, album)
            for uri, album in (("secondcopy", "Second"), ("secondalternate", "Third"))
        ))
        return track, second_track, link, pressing, first_code, second_code

    def test_recording_proposal_checks_source_bound_observations_from_startup_holds(self) -> None:
        _, held, link, pressing, reverse, _ = self.two_track_recording_fixture()
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        held_record = self.cache_negative(held, SpotifyTrackCandidate(
            "spotify:track:studio", held.track_name, (held.artist_name,), "Field Notes", "FieldNotes",
            isrc="ZZABC2600099",
        ))
        record_cached_recording_conflict(held_record, "Synthetic recording conflict")
        held_key = spotify_track_match_key(held)
        for case, blocked in (
            ("current", True), ("removed_before_export", True), ("added_before_export", True),
            ("stale", False), ("different_source", False),
        ):
            with self.subTest(case=case):
                self.requests.clear()
                initial = deepcopy(self.matches)
                if case == "stale":
                    initial[held_key]["matcher_version"] = MATCHER_VERSION - 1
                elif case == "different_source":
                    initial[held_key]["track_name"] = "Another source"
                latest = deepcopy(initial)
                if case == "removed_before_export":
                    latest[held_key]["review_candidates"] = []
                elif case == "added_before_export":
                    initial[held_key]["review_candidates"] = []
                with patch.object(
                    review_track_matches, "load_spotify_track_match_cache", side_effect=(initial, latest),
                ):
                    report, paths = self.run_recording_review(
                        link, pressing, reverse, output=self.directory / f"startup-{case}.json",
                    )
                self.assertEqual((len(paths), len(self.requests)), (3, 1))
                candidate, held_row = report["rows"]
                self.assertEqual(held_row["status"], "recording_conflict")
                self.assertNotIn("recording_proof", held_row)
                overrides = json.loads(self.proposals.read_text())["matches"]
                if blocked:
                    self.assertEqual(candidate["status"], "recording_unverified")
                    self.assertIn("A cached observation", candidate["reason"])
                    self.assertEqual(overrides, [])
                else:
                    self.assertEqual(candidate["status"], "proposal")
                    self.assertEqual([override["spotify_uri"] for override in overrides], ["spotify:track:studio"])

    def test_recording_proposal_rechecks_new_cached_contradictions_without_holding_independent_rows(self) -> None:
        track, second, link, pressing, first, other = self.two_track_recording_fixture()
        self.set_isrc_hits(first["isrc"], self.recording_hit("studio", first["isrc"]))
        self.set_isrc_hits(other["isrc"], {
            **self.recording_hit("secondcopy", other["isrc"], album="Second"), "name": second.track_name,
        })
        for label, observed_track, uri, code in (
            ("same-row", track, "studio", "ZZABC2600099"),
            ("same-uri", second, "studio", "ZZABC2600099"),
            ("same-isrc", second, "different", first["isrc"]),
            ("held-source", second, "studio", "ZZABC2600099"),
        ):
            with self.subTest(contradiction=label):
                latest = deepcopy(self.matches)
                record = latest[spotify_track_match_key(observed_track)]
                if label == "held-source":
                    record_cached_recording_conflict(record, "Synthetic recording conflict")
                observations = record["review_candidates"]
                assert isinstance(observations, list)
                observations.append({
                    "spotify_uri": f"spotify:track:{uri}", "spotify_track_name": observed_track.track_name,
                    "spotify_artist_names": ["Mara Vale"], "spotify_album_name": "Field Notes",
                    "spotify_album_id": "FieldNotes", "spotify_isrc": code,
                })
                snapshots = iter((self.matches, latest))
                requests_at_read: list[int] = []

                def read_matches(_: Path) -> dict[str, dict[str, object]]:
                    requests_at_read.append(len(self.requests))
                    return next(snapshots)

                self.requests.clear()
                with patch.object(review_track_matches, "load_spotify_track_match_cache", side_effect=read_matches):
                    report, paths = self.run_recording_review(
                        link, pressing, first, other_recordings=(other,), output=self.directory / f"{label}.json",
                    )
                self.assertEqual(requests_at_read, [0, 2])
                self.assertEqual((len(paths), len(self.requests)), (4, 2))
                held, other_row = report["rows"]
                self.assertEqual(held["status"], "recording_unverified")
                self.assertIn("A cached observation", held["reason"])
                overrides = json.loads(self.proposals.read_text())["matches"]
                if label == "held-source":
                    self.assertEqual(other_row["status"], "recording_conflict")
                    self.assertEqual(overrides, [])
                else:
                    self.assertEqual(other_row["status"], "proposal")
                    row, = overrides
                    self.assertEqual(row["spotify_uri"], "spotify:track:secondcopy")

    def test_recording_proposal_requires_eligibility_in_both_cache_snapshots(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        code = reverse["isrc"]
        original = SpotifyTrackCandidate(
            "spotify:track:original", track.track_name, (track.artist_name,), track.album_name, "PaperCoast",
        )
        alternate = SpotifyTrackCandidate(
            "spotify:track:studio", track.track_name, (track.artist_name,), "Field Notes", "FieldNotes",
        )
        new = replace(alternate, uri="spotify:track:new")
        original_hit = self.recording_hit("original", code, album=track.album_name)
        alternate_hit = self.recording_hit("studio", code)
        new_hit = self.recording_hit("new", code)
        self.cache_negative(track, original, alternate)
        initial = self.matches
        for label, current_candidates, isrc_hits, expected_uri in (
            ("removed", (alternate,), (original_hit,), ""),
            ("fallback", (alternate,), (original_hit, alternate_hit), alternate.uri),
            ("added", (new,), (new_hit,), ""),
            ("unmatched", (original, alternate), (original_hit,), ""),
            ("timestamp", (original, alternate), (original_hit, alternate_hit), original.uri),
        ):
            with self.subTest(change=label):
                self.requests.clear()
                self.matches = {}
                current_record = self.cache_negative(track, *current_candidates)
                current_record["last_seen_at"] = "2026-11-25T00:00:00+00:00"
                if label == "unmatched":
                    current_record["match_status"] = UNMATCHED
                latest = self.matches
                self.matches = initial
                self.set_isrc_hits(code, *isrc_hits)
                with patch.object(review_track_matches, "load_spotify_track_match_cache", side_effect=(initial, latest)):
                    report, paths = self.run_recording_review(
                        link, pressing, reverse, output=self.directory / f"{label}.json",
                    )
                self.assertEqual((len(paths), len(self.requests)), (3, 1))
                row, = report["rows"]
                overrides = json.loads(self.proposals.read_text())["matches"]
                if expected_uri:
                    self.assertEqual(row["status"], "proposal")
                    self.assertEqual([override["spotify_uri"] for override in overrides], [expected_uri])
                    if label == "fallback":
                        self.assertEqual(row["recording_proof"]["alternatives"], [])
                else:
                    self.assertEqual(row["status"], "recording_unverified")
                    self.assertEqual(overrides, [])

    def test_recording_proposal_honors_later_source_and_manual_ownership_holds(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        for hold in ("source_collision", "manual_hold"):
            with self.subTest(hold=hold):
                self.requests.clear()
                latest = deepcopy(self.matches)
                current_track = track
                if hold == "source_collision":
                    current_track = replace(track, track_name="Changed title")
                else:
                    latest[spotify_track_match_key(track)]["match_status"] = "manual"
                with (
                    patch.object(review_track_matches, "read_playlist_tracks", side_effect=((track,), (current_track,))),
                    patch.object(review_track_matches, "load_spotify_track_match_cache", side_effect=(self.matches, latest)),
                ):
                    report, paths = self.run_recording_review(
                        link, pressing, reverse, output=self.directory / f"{hold}.json",
                    )
                self.assertEqual((len(paths), len(self.requests)), (3, 1))
                self.assertEqual(report["rows"][0]["status"], hold)
                self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_rejects_code_reused_on_different_printed_tracks(self) -> None:
        _, _, link, pressing, first, _ = self.two_track_recording_fixture()
        pressing["media"][0]["tracks"][1]["recording"]["isrcs"] = [first["isrc"]]
        report, paths = self.run_recording_review(link, pressing, first)
        self.assertEqual(len(paths), 2)
        self.assertFalse(self.requests)
        self.assertEqual(len(report["rows"]), 2)
        self.assertTrue(all("more than one printed" in row["reason"] for row in report["rows"]))

    def test_recording_proposal_retains_uri_contradiction_from_another_rows_isrc_search(self) -> None:
        _, second, link, pressing, first, other = self.two_track_recording_fixture()
        self.set_isrc_hits(first["isrc"], self.recording_hit("studio", first["isrc"]))
        repeated = {**self.recording_hit("studio", other["isrc"]), "name": second.track_name}
        self.set_isrc_hits(other["isrc"], repeated)
        report, _ = self.run_recording_review(link, pressing, first, other_recordings=(other,))
        self.assertEqual(len(self.requests), 2)
        self.assertIn("contradicts Spotify", report["rows"][0]["reason"])
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_leaves_other_unresolved_sources_blocking_exact_replace(self) -> None:
        track, unresolved, link, pressing, reverse, _ = self.two_track_recording_fixture()
        self.cache_negative(unresolved)
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        self.run_recording_review(link, pressing, reverse)
        self.assertEqual(manual_matches.import_manual_match_overrides(self.proposals, self.root, self.cache).planned_count, 1)
        # Model a reviewed manual binding in the synthetic cache without applying the file.
        self.matches[spotify_track_match_key(track)].update(
            match_status="manual", spotify_uri="spotify:track:studio",
            spotify_track_name=track.track_name, spotify_artist_names=[track.artist_name], spotify_album_name="Field Notes",
        )
        save_spotify_track_match_cache(self.cache, self.matches)
        client = PublishingSpotifyClient({})
        master = self.root / "Fictional Shelf" / "Fictional Shelf.csv"
        with self.assertRaisesRegex(ValueError, r"replace mode aborted before writing.*unmatched=1"):
            publish_spotify_playlists(
                self.root, self.directory / "publisher-report.txt", client, "synthetic", match_cache_path=self.cache,
                playlist_master_paths=(master,), apply=True, publisher_sync_mode="replace",
            )
        self.assertEqual((client.created_playlists, client.replace_calls, client.add_calls), ([], [], []))

    def test_recording_proposal_holds_conflicting_codes_and_titles_under_one_musicbrainz_recording(self) -> None:
        _, second, link, pressing, first, other = self.two_track_recording_fixture()
        shared_recording = first["recordings"][0]["id"]
        pressing["media"][0]["tracks"][1]["recording"]["id"] = shared_recording
        other["recordings"] = [{"id": shared_recording}]
        self.set_isrc_hits(first["isrc"], self.recording_hit("studio", first["isrc"]))
        self.set_isrc_hits(other["isrc"], {
            **self.recording_hit("secondcopy", other["isrc"], album="Second"), "name": second.track_name,
        })
        report, paths = self.run_recording_review(link, pressing, first, other_recordings=(other,))
        self.assertEqual(len(paths), 2)
        self.assertFalse(self.requests)
        self.assertTrue(all("recording ID" in row["reason"] for row in report["rows"]))
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_checks_excess_items_for_cross_search_contradictions(self) -> None:
        _, second, link, pressing, first, other = self.two_track_recording_fixture()
        self.set_isrc_hits(first["isrc"], self.recording_hit("studio", first["isrc"]))
        over_limit = [{
            **self.recording_hit(f"Excess{index}", other["isrc"], album=f"Excess {index}"),
            "name": second.track_name,
        } for index in range(10)]
        over_limit.append({**self.recording_hit("studio", other["isrc"]), "name": second.track_name})
        self.set_isrc_hits(other["isrc"], *over_limit)
        report, _ = self.run_recording_review(link, pressing, first, other_recordings=(other,), code=1)
        proposed, incomplete = report["rows"]
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertIn("contradicts Spotify", proposed["reason"])
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_rejects_tracks_outside_the_musicbrainz_audio_list(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        medium = pressing["media"][0]
        for field, value in (("pregap", {"position": 0}), ("data-tracks", [{"position": 2}])):
            with self.subTest(field=field):
                medium[field] = value
                report, paths = self.run_recording_review(link, pressing, reverse, output=self.directory / f"{field}.json")
                self.assertEqual(len(paths), 2)
                self.assertFalse(self.requests)
                self.assertIn("outside", report["rows"][0]["reason"])
                self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])
                del medium[field]

    def repeat_recording_fixture(self) -> tuple[PlaylistTrack, PlaylistTrack, dict, dict, dict]:
        track, _, link, pressing, reverse, _ = self.two_track_recording_fixture()
        repeat = replace(track, track_number="2")
        self.write_master("Fictional Shelf", (track, repeat))
        self.write_master("Second Shelf", (repeat,))
        self.seed_recording_tie(repeat)
        self.payload["tracklist"] = [discogs_track(track.track_name, position, duration="3:02") for position in ("1", "2")]
        medium = pressing["media"][0]
        first = medium["tracks"][0]
        medium["tracks"][1] = {**first, "position": 2, "number": "2", "recording": dict(first["recording"])}
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        return track, repeat, link, pressing, reverse

    def test_recording_proposal_rejects_shared_candidate_with_two_catalog_codes(self) -> None:
        _, _, link, pressing, first = self.repeat_recording_fixture()
        other_id = "00000000-1111-4111-8111-000000000004"
        other_code = "ZZABC2600002"
        pressing["media"][0]["tracks"][1]["recording"].update(id=other_id, isrcs=[other_code])
        other = {"isrc": other_code, "recordings": [{"id": other_id}]}
        self.set_isrc_hits(other_code, self.recording_hit("studio", other_code))
        report, paths = self.run_recording_review(link, pressing, first, other_recordings=(other,))
        self.assertEqual((len(paths), len(self.requests)), (4, 2))
        for row in report["rows"]:
            self.assertEqual(row["status"], "recording_unverified")
            self.assertIn("contradicts Spotify's ISRC evidence for one URI", row["reason"])
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_intentional_repeat_previews_one_override_per_printed_position(self) -> None:
        _, _, link, pressing, reverse = self.repeat_recording_fixture()
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(len(paths), 3)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([row["status"] for row in report["rows"]], ["proposal", "proposal"])
        self.assertEqual(set(report["rows"][1]["playlists"]), {"Fictional Shelf", "Second Shelf"})
        result = manual_matches.import_manual_match_overrides(self.proposals, self.root, self.cache)
        self.assertEqual((result.planned_count, result.applied_count), (2, 0))
        overrides = json.loads(self.proposals.read_text())["matches"]
        self.assertEqual([(item["track_number"], item["spotify_uri"]) for item in overrides],
                         [("1", "spotify:track:studio"), ("2", "spotify:track:studio")])

    def test_recording_proposal_intentional_repeat_holds_conflicting_or_invalid_sibling_evidence(self) -> None:
        _, _, link, pressing, reverse = self.repeat_recording_fixture()
        second = pressing["media"][0]["tracks"][1]
        different_artist = [{
            "name": "Mara Vale",
            "artist": {"id": "00000000-1111-4111-8111-000000000088", "name": "Mara Vale"},
        }]
        for index, overrides in enumerate((
            {"length": 182200},
            {"length": None},
            {"recording": {**second["recording"], "artist-credit": different_artist}},
            {"artist-credit": different_artist, "recording": {**second["recording"], "artist-credit": different_artist}},
        )):
            with self.subTest(overrides=overrides):
                pressing["media"][0]["tracks"][1] = {**second, **overrides}
                report, paths = self.run_recording_review(
                    link, pressing, reverse, output=self.directory / f"repeat-held-{index}.json",
                )
                self.assertEqual(len(paths), 2)
                self.assertFalse(self.requests)
                self.assertEqual([row["status"] for row in report["rows"]], ["recording_unverified"] * 2)
                self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_intentional_repeat_shares_an_incomplete_reverse_lookup(self) -> None:
        _, _, link, pressing, reverse = self.repeat_recording_fixture()
        isrc_path = f"/ws/2/isrc/{reverse['isrc']}"
        report, paths = self.run_recording_review(
            link, pressing, reverse, failures={isrc_path: [500, 500]}, code=1,
        )
        self.assertEqual(paths.count(isrc_path), 2)  # One lookup and its single retry.
        self.assertFalse(self.requests)
        self.assertEqual([row["status"] for row in report["rows"]], ["incomplete", "incomplete"])

    def test_recording_proposal_never_uses_absent_workflow_cache_destinations(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        for constant, filename in (
            ("DEFAULT_PROCESSING_CACHE_PATH", "processing.cache.json"),
            ("DEFAULT_SEEN_TERMS_CACHE_PATH", "collected.cache.json"),
            ("DEFAULT_TRACKLIST_CACHE_PATH", "playlist-tracks.cache.json"),
            ("DEFAULT_SPOTIFY_PUBLISH_STATE_CACHE_PATH", "spotify-publish-state.cache.json"),
            ("DEFAULT_SPOTIFY_MATCH_CACHE_PATH", "spotify-track-matches.cache.json"),
            ("DEFAULT_TOKEN_CACHE_PATH", "spotify-token.cache.json"),
        ):
            with self.subTest(constant=constant):
                self.requests.clear()
                destination = self.directory / filename
                with patch.object(manual_matches, constant, destination):
                    report, paths = self.run_recording_review(link, pressing, reverse, output=destination, code=1)
                self.assertEqual((report, paths, self.requests), ({}, [], []))
                self.assertFalse(destination.exists())

    def typography_recording_fixture(self) -> tuple[PlaylistTrack, dict, dict, dict]:
        _, link, pressing, reverse = self.recording_fixture(alias=True)
        track = replace(TRACK, artist_name="M. Vale-Snow")
        self.write_master("Fictional Shelf", (track,))
        self.cache_negative(track)
        self.payload["artists"] = [artist(81, "Mara Vale-Snow", track.artist_name)]
        mb_track = pressing["media"][0]["tracks"][0]
        artist_id = mb_track["artist-credit"][0]["artist"]["id"]
        mb_track["artist-credit"] = [
            {"name": "M .Vale Snow", "artist": {"name": "Mara Vale Snow", "id": artist_id}},
        ]
        mb_track["recording"]["artist-credit"] = [
            {"name": "M. Vale‐Snow", "artist": {"name": "Mara Vale‑Snow", "id": artist_id}},
        ]
        studio = {**self.recording_hit("studio", reverse["isrc"]), "artists": [{"name": "Mara Vale Snow"}]}
        rival = spotify_hit("rival", "Mara Vale-Snow", album="Other Disc")
        self.search_responses['track:"Night Signal" artist:"M. Vale-Snow"'] = [studio]
        self.search_responses['track:"Night Signal" artist:"Mara Vale-Snow"'] = [rival]
        self.set_isrc_hits(reverse["isrc"], studio)
        return track, link, pressing, reverse

    def test_recording_proposal_applies_approved_typography_to_both_musicbrainz_credits(self) -> None:
        _, link, pressing, reverse = self.typography_recording_fixture()
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(len(paths), 3)
        self.assertEqual(report["rows"][0]["status"], "proposal")
        self.assertEqual(manual_matches.import_manual_match_overrides(self.proposals, self.root, self.cache).planned_count, 1)

    def test_recording_proposal_typography_leaves_conflicting_recording_versions_and_credits_held(self) -> None:
        _, link, pressing, reverse = self.typography_recording_fixture()
        tracks = pressing["media"][0]["tracks"]
        source = tracks[0]
        recording = source["recording"]
        credit = recording["artist-credit"][0]
        for index, changed in enumerate((
            {**recording, "artist-credit": [{**credit, "name": "M. Vole Snow"}]},
            {**recording, "artist-credit": [{**credit, "name": "M. Valé Snow"}]},
            {**recording, "artist-credit": []},
            {**recording, "title": "Night-Signal"},
            {**recording, "title": "Night Signal (Live)"},
        )):
            with self.subTest(changed=changed):
                tracks[0] = {**source, "recording": changed}
                report, paths = self.run_recording_review(link, pressing, reverse, output=self.directory / f"held-typography-{index}.json")
                self.assertEqual(len(paths), 2)
                self.assertIn("linked MusicBrainz recording", report["rows"][0]["reason"])
                self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_holds_unmarked_live_disambiguation(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        self.seed_recording_tie(track)
        pressing["media"][0]["tracks"][0]["recording"]["disambiguation"] = "live, 2026-05-03: Harbour Hall"
        self.set_isrc_hits(reverse["isrc"], self.recording_hit("studio", reverse["isrc"]))
        report, paths = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(len(paths), 2)
        self.assertFalse(self.requests)
        self.assertIn("live disambiguation", report["rows"][0]["reason"])
        self.assertEqual(json.loads(self.proposals.read_text())["matches"], [])

    def test_recording_proposal_accepts_exactly_corroborated_live_disambiguation(self) -> None:
        track, link, pressing, reverse = self.recording_fixture()
        title = "Night Signal (Live, 2026-05-03: Harbour Hall)"
        track = replace(track, track_name=title)
        self.write_master("Fictional Shelf", (track,))
        self.payload["tracklist"] = [discogs_track(title, "1", duration="3:02")]
        mb_track = pressing["media"][0]["tracks"][0]
        mb_track["title"] = title
        mb_track["recording"].update(title=title, disambiguation="live, 2026-05-03: Harbour Hall")
        self.cache_negative(track, *(
            SpotifyTrackCandidate(f"spotify:track:{uri}", title, ("Mara Vale",), album, album_id)
            for uri, album, album_id in (("studio", "Field Notes", "FieldNotes"), ("other", "Other Disc", "OtherDisc"))
        ))
        self.set_isrc_hits(reverse["isrc"], {**self.recording_hit("studio", reverse["isrc"]), "name": title})
        report, _ = self.run_recording_review(link, pressing, reverse)
        self.assertEqual(report["rows"][0]["status"], "proposal")

    def test_recording_proposal_reports_missing_spotify_metadata_without_holding_other_rows(self) -> None:
        _, second, link, pressing, first, other = self.two_track_recording_fixture()
        self.set_isrc_hits(first["isrc"], self.recording_hit("studio", first["isrc"]))
        complete_hit = {**self.recording_hit("secondcopy", other["isrc"], album="Second"), "name": second.track_name}
        for field in ("duration_ms", "external_ids"):
            with self.subTest(field=field):
                incomplete_hit = dict(complete_hit)
                del incomplete_hit[field]
                self.set_isrc_hits(other["isrc"], incomplete_hit)
                report, _ = self.run_recording_review(
                    link, pressing, first, other_recordings=(other,), code=1, output=self.directory / f"missing-{field}.json",
                )
                self.assertEqual([row["status"] for row in report["rows"]], ["proposal", "incomplete"])
                self.assertEqual(manual_matches.import_manual_match_overrides(self.proposals, self.root, self.cache).planned_count, 1)

        complete_hit["external_ids"] = {"isrc": "ZZABC2600099"}
        self.set_isrc_hits(other["isrc"], complete_hit)
        report, _ = self.run_recording_review(
            link, pressing, first, other_recordings=(other,), output=self.directory / "different-code.json",
        )
        self.assertEqual([row["status"] for row in report["rows"]], ["proposal", "recording_unverified"])

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
        self.assertCountEqual([urlsplit(request.url).path for request in self.requests], [
            "/v1/search", "/v1/search", "/v1/tracks/printed", "/v1/tracks/canonical", "/v1/tracks/oldcanonical",
        ])
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
        self.assertEqual([candidate["uri"] for candidate in row["candidates"]],
                         ["spotify:track:formatting", "spotify:track:accent", "spotify:track:titleaccent"])

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
