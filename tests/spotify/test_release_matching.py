import sys
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.matching import PlaylistTrack, SpotifyTrackCandidate
from publishers.spotify.match_cache import (
    MATCHER_VERSION,
    cache_track_match,
    cached_track_match,
    spotify_track_match_key,
)
from publishers.spotify.release_matching import (
    ALBUM_ALPHANUMERIC_SPACING_MATCH_STRATEGY,
    ALBUM_EXACT_TRACK_MATCH_STRATEGY,
    ALBUM_POSITION_MATCH_STRATEGY,
    TRACK_CANDIDATE_ALBUM_EXACT_MATCH_STRATEGY,
    SpotifyAlbumCandidate,
    SpotifyAlbumTrack,
    SpotifyReleaseAlbumMatchResult,
    analyze_album_sequence,
    build_spotify_album_search_query,
    choose_validated_album_match,
    match_release_tracks_to_album,
    resolve_release_with_album,
)


def source_track(
    track_number: int,
    track_name: str,
    artist_name: str = "Example Artist",
    album_name: str = "Numbers and Colours",
) -> PlaylistTrack:
    return PlaylistTrack(
        playlist_name="Techno",
        release_id="release-1",
        album_name=album_name,
        track_number=str(track_number),
        track_name=track_name,
        artist_name=artist_name,
        spotify_search_query=f"{artist_name} {track_name} Numbers and Colours",
    )


def album_track(
    track_number: int,
    track_name: str,
    artist_names: tuple[str, ...] = ("Example Artist",),
    *,
    is_playable: bool | None = True,
) -> SpotifyAlbumTrack:
    return SpotifyAlbumTrack(
        uri=f"spotify:track:{track_number}",
        name=track_name,
        artists=artist_names,
        disc_number=1,
        track_number=track_number,
        is_playable=is_playable,
    )


def album_candidate(
    album_id: str = "album-1",
    album_name: str = "Numbers and Colours",
) -> SpotifyAlbumCandidate:
    return SpotifyAlbumCandidate(
        album_id=album_id,
        uri=f"spotify:album:{album_id}",
        name=album_name,
        artists=("Example Artist",),
        total_tracks=6,
    )


class FakeAlbumLookupClient:
    def __init__(
        self,
        candidates: tuple[SpotifyAlbumCandidate, ...],
        tracks_by_album_id: dict[str, tuple[SpotifyAlbumTrack, ...]],
    ) -> None:
        self.candidates = candidates
        self.tracks_by_album_id = tracks_by_album_id
        self.searches: list[tuple[str, str, int]] = []
        self.album_track_requests: list[tuple[str, str]] = []

    def search_albums(
        self,
        access_token: str,
        query: str,
        limit: int = 10,
    ) -> tuple[SpotifyAlbumCandidate, ...]:
        self.searches.append((access_token, query, limit))
        return self.candidates

    def get_album_tracks(
        self,
        access_token: str,
        album_id: str,
    ) -> tuple[SpotifyAlbumTrack, ...]:
        self.album_track_requests.append((access_token, album_id))
        return self.tracks_by_album_id[album_id]


class SpotifyReleaseMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_tracks = (
            source_track(1, "1", "Example Artist, Collaborator"),
            source_track(2, "2", "Example Artist, Collaborator"),
            source_track(3, "3", "Example Artist, Collaborator"),
            source_track(4, "Silent Anchor", "Example Artist, Collaborator"),
            source_track(5, "Hidden Anchor", "Example Artist, Collaborator"),
            source_track(6, "Final Anchor"),
        )
        self.spotify_tracks = (
            album_track(1, "White", ("Example Artist", "Collaborator")),
            album_track(2, "Grey", ("Example Artist", "Collaborator")),
            album_track(3, "Black", ("Example Artist", "Collaborator")),
            album_track(4, "Silent Anchor", ("Example Artist", "Collaborator")),
            album_track(5, "Hidden Anchor", ("Example Artist", "Collaborator")),
            album_track(6, "Final Anchor"),
        )

    def test_builds_quoted_album_search_query(self) -> None:
        self.assertEqual(
            build_spotify_album_search_query('Numbers "and" Colours'),
            'album:"Numbers and Colours"',
        )

    def test_exact_anchors_validate_positional_mapping_for_renamed_prefix(self) -> None:
        decisions = match_release_tracks_to_album(
            source_tracks=self.source_tracks,
            album=album_candidate(),
            spotify_tracks=self.spotify_tracks,
            album_search_query='album:"Numbers and Colours"',
        )

        self.assertEqual(tuple(decision.spotify_uri for decision in decisions), tuple(
            f"spotify:track:{track_number}" for track_number in range(1, 7)
        ))
        self.assertEqual(
            tuple(decision.match_strategy for decision in decisions[:3]),
            (ALBUM_POSITION_MATCH_STRATEGY,) * 3,
        )
        self.assertEqual(
            tuple(decision.match_strategy for decision in decisions[3:]),
            (ALBUM_EXACT_TRACK_MATCH_STRATEGY,) * 3,
        )
        self.assertTrue(all(decision.candidate and decision.candidate.album_id == "album-1" for decision in decisions))

    def test_positional_mapping_leaves_artist_mismatch_unresolved(self) -> None:
        spotify_tracks = (
            self.spotify_tracks[0],
            album_track(2, "Grey", ("Different Artist",)),
            *self.spotify_tracks[2:],
        )

        decisions = match_release_tracks_to_album(
            source_tracks=self.source_tracks,
            album=album_candidate(),
            spotify_tracks=spotify_tracks,
            album_search_query='album:"Numbers and Colours"',
        )

        self.assertEqual(
            tuple(decision.track.track_number for decision in decisions),
            ("1", "3", "4", "5", "6"),
        )

    def test_does_not_infer_positions_without_two_exact_anchors(self) -> None:
        spotify_tracks = tuple(
            album_track(index, colour, ("Example Artist", "Collaborator"))
            for index, colour in enumerate(("White", "Grey", "Black", "Blue", "Red", "Gold"), start=1)
        )

        decisions = match_release_tracks_to_album(
            source_tracks=self.source_tracks,
            album=album_candidate(),
            spotify_tracks=spotify_tracks,
            album_search_query='album:"Numbers and Colours"',
        )

        self.assertEqual(decisions, ())

    def test_does_not_infer_positions_when_exact_anchors_conflict_in_order(self) -> None:
        spotify_tracks = (
            *self.spotify_tracks[:3],
            self.spotify_tracks[4],
            self.spotify_tracks[3],
            self.spotify_tracks[5],
        )

        decisions = match_release_tracks_to_album(
            source_tracks=self.source_tracks,
            album=album_candidate(),
            spotify_tracks=spotify_tracks,
            album_search_query='album:"Numbers and Colours"',
        )

        self.assertEqual(decisions, ())

    def test_unequal_gap_lengths_only_return_exact_anchor_matches(self) -> None:
        spotify_tracks = (
            *self.spotify_tracks[:3],
            album_track(4, "Bonus Track", ("Example Artist", "Collaborator")),
            album_track(5, "Silent Anchor", ("Example Artist", "Collaborator")),
            album_track(6, "Hidden Anchor", ("Example Artist", "Collaborator")),
            album_track(7, "Final Anchor"),
        )

        decisions = match_release_tracks_to_album(
            source_tracks=self.source_tracks,
            album=album_candidate(),
            spotify_tracks=spotify_tracks,
            album_search_query='album:"Numbers and Colours"',
        )

        self.assertEqual(
            tuple(decision.track.track_number for decision in decisions),
            ("4", "5", "6"),
        )
        self.assertTrue(all(decision.match_strategy == ALBUM_EXACT_TRACK_MATCH_STRATEGY for decision in decisions))

    def test_does_not_positionally_map_a_gap_larger_than_three_tracks(self) -> None:
        source_tracks = (
            source_track(1, "1"),
            source_track(2, "2"),
            source_track(3, "3"),
            source_track(4, "4"),
            source_track(5, "Anchor Five"),
            source_track(6, "Anchor Six"),
        )
        spotify_tracks = (
            album_track(1, "White"),
            album_track(2, "Grey"),
            album_track(3, "Black"),
            album_track(4, "Gold"),
            album_track(5, "Anchor Five"),
            album_track(6, "Anchor Six"),
        )

        decisions = match_release_tracks_to_album(
            source_tracks=source_tracks,
            album=album_candidate(),
            spotify_tracks=spotify_tracks,
            album_search_query='album:"Numbers and Colours"',
        )

        self.assertEqual(
            tuple(decision.track.track_number for decision in decisions),
            ("5", "6"),
        )

    def test_multiple_valid_spotify_editions_are_treated_as_ambiguous(self) -> None:
        first = match_release_tracks_to_album(
            self.source_tracks,
            album_candidate("album-1"),
            self.spotify_tracks,
            'album:"Numbers and Colours"',
        )
        second = match_release_tracks_to_album(
            self.source_tracks,
            album_candidate("album-2"),
            self.spotify_tracks,
            'album:"Numbers and Colours"',
        )

        self.assertEqual(choose_validated_album_match((first, second)), ())

    def test_release_lookup_searches_album_once_and_fetches_exact_title_candidate(self) -> None:
        client = FakeAlbumLookupClient(
            candidates=(
                SpotifyAlbumCandidate(
                    album_id="other-album",
                    uri="spotify:album:other-album",
                    name="A Different Album",
                    artists=("Example Artist",),
                    total_tracks=6,
                ),
                album_candidate(),
            ),
            tracks_by_album_id={"album-1": self.spotify_tracks},
        )

        result = resolve_release_with_album(
            source_tracks=self.source_tracks,
            spotify_client=client,
            access_token="access-token",
            search_limit=10,
        )

        self.assertEqual(result.search_count, 1)
        self.assertEqual(len(result.decisions), 6)
        self.assertFalse(result.used_fallback_track_candidates)
        self.assertEqual(client.searches, [("access-token", 'album:"Numbers and Colours"', 10)])
        self.assertEqual(client.album_track_requests, [("access-token", "album-1")])

    def test_release_lookup_accepts_letter_number_spacing_in_album_and_track_titles(self) -> None:
        source_tracks = (
            source_track(1, "Chroma001 Helium", album_name="Chroma000"),
            source_track(2, "Chroma002 L.A.V.A", album_name="Chroma000"),
            source_track(3, "Chroma003 Bi83", album_name="Chroma000"),
        )
        spotify_tracks = (
            album_track(1, "CHROMA 001 HELIUM"),
            album_track(2, "CHROMA 002 L.A.V.A"),
            album_track(3, "CHROMA 003 Bi83"),
        )
        candidate = album_candidate(album_name="CHROMA 000")
        client = FakeAlbumLookupClient(
            candidates=(candidate,),
            tracks_by_album_id={"album-1": spotify_tracks},
        )

        result = resolve_release_with_album(
            source_tracks=source_tracks,
            spotify_client=client,
            access_token="access-token",
            search_limit=10,
        )
        match_cache: dict[str, dict[str, object]] = {}
        for decision in result.decisions:
            cache_track_match(
                match_cache,
                decision,
                matched_at="2026-01-01T00:00:00Z",
            )

        self.assertEqual(len(result.decisions), 3)
        self.assertEqual(result.diagnostic, "")
        self.assertEqual(client.album_track_requests, [("access-token", "album-1")])
        self.assertTrue(
            all(
                decision.match_strategy == ALBUM_ALPHANUMERIC_SPACING_MATCH_STRATEGY
                for decision in result.decisions
            )
        )
        self.assertTrue(
            all(record.get("version_sensitive") is True for record in match_cache.values())
        )

        first_cache_key = spotify_track_match_key(source_tracks[0])
        legacy_record = dict(match_cache[first_cache_key])
        legacy_record.pop("version_sensitive")
        legacy_record["match_strategy"] = ALBUM_EXACT_TRACK_MATCH_STRATEGY
        legacy_record["matcher_version"] = MATCHER_VERSION - 1

        self.assertIsNone(
            cached_track_match(
                source_tracks[0],
                {first_cache_key: legacy_record},
            )
        )

    def test_ordinary_exact_album_match_remains_version_stable(self) -> None:
        source_tracks = tuple(
            source_track(index, f"Anchor {index}")
            for index in range(1, 4)
        )
        spotify_tracks = tuple(
            album_track(index, f"Anchor {index}")
            for index in range(1, 4)
        )

        decisions = match_release_tracks_to_album(
            source_tracks=source_tracks,
            album=album_candidate(),
            spotify_tracks=spotify_tracks,
            album_search_query='album:"Numbers and Colours"',
        )
        match_cache: dict[str, dict[str, object]] = {}
        cache_track_match(
            match_cache,
            decisions[0],
            matched_at="2026-01-01T00:00:00Z",
        )
        cache_key = spotify_track_match_key(source_tracks[0])
        match_cache[cache_key]["matcher_version"] = MATCHER_VERSION - 1

        self.assertEqual(
            decisions[0].match_strategy,
            ALBUM_EXACT_TRACK_MATCH_STRATEGY,
        )
        self.assertNotIn("version_sensitive", match_cache[cache_key])
        self.assertIsNotNone(cached_track_match(source_tracks[0], match_cache))

    def test_release_lookup_reports_why_matching_album_candidates_were_not_used(self) -> None:
        no_title_match_client = FakeAlbumLookupClient(
            candidates=(album_candidate(album_name="Different Album"),),
            tracks_by_album_id={},
        )
        too_many_client = FakeAlbumLookupClient(
            candidates=tuple(album_candidate(f"album-{index}") for index in range(1, 7)),
            tracks_by_album_id={},
        )
        no_anchors_tracks = tuple(
            album_track(index, f"Different {index}", ("Example Artist", "Collaborator"))
            for index in range(1, 7)
        )
        no_anchors_client = FakeAlbumLookupClient(
            candidates=(album_candidate(),),
            tracks_by_album_id={"album-1": no_anchors_tracks},
        )

        no_title_match = resolve_release_with_album(
            self.source_tracks,
            no_title_match_client,
            "access-token",
        )
        too_many = resolve_release_with_album(
            self.source_tracks,
            too_many_client,
            "access-token",
        )
        no_anchors = resolve_release_with_album(
            self.source_tracks,
            no_anchors_client,
            "access-token",
        )

        self.assertEqual(
            no_title_match.diagnostic,
            "Spotify returned no album candidate with a matching title",
        )
        self.assertEqual(
            too_many.diagnostic,
            "Spotify returned 6 album candidates with a matching title; the safe maximum is 5",
        )
        self.assertEqual(
            no_anchors.diagnostic,
            (
                "matching-title Spotify albums failed sequence validation: "
                "album-1 (Numbers and Colours): source/Spotify tracks 6/6; "
                "exact title-and-artist anchors 0; title anchors 0; "
                "artist-only source tracks 5; anchor order valid; unequal gaps 0; "
                "oversized gaps 1; positional artist mismatches 0; unusable Spotify tracks 0"
            ),
        )
        self.assertEqual(len(no_anchors.validation_diagnostics), 1)
        validation_diagnostic = no_anchors.validation_diagnostics[0]
        self.assertEqual(validation_diagnostic.album_id, "album-1")
        self.assertEqual(validation_diagnostic.source_track_count, 6)
        self.assertEqual(validation_diagnostic.spotify_track_count, 6)
        self.assertEqual(validation_diagnostic.exact_anchor_count, 0)
        self.assertEqual(validation_diagnostic.title_anchor_count, 0)
        self.assertEqual(validation_diagnostic.artist_only_source_track_count, 5)
        self.assertTrue(validation_diagnostic.anchor_order_is_valid)
        self.assertEqual(validation_diagnostic.unequal_gap_count, 0)
        self.assertEqual(validation_diagnostic.oversized_gap_count, 1)
        self.assertEqual(validation_diagnostic.positional_artist_mismatch_count, 0)
        self.assertEqual(validation_diagnostic.unusable_spotify_track_count, 0)

    def test_more_than_five_exact_album_candidates_falls_back_without_fetching(self) -> None:
        candidates = tuple(album_candidate(f"album-{index}") for index in range(1, 7))
        client = FakeAlbumLookupClient(candidates=candidates, tracks_by_album_id={})

        result = resolve_release_with_album(
            source_tracks=self.source_tracks,
            spotify_client=client,
            access_token="access-token",
            search_limit=10,
        )

        self.assertEqual(result.search_count, 1)
        self.assertEqual(result.decisions, ())
        self.assertEqual(client.album_track_requests, [])

    def test_five_exact_album_candidates_are_fetched_and_one_valid_edition_is_selected(self) -> None:
        candidates = tuple(album_candidate(f"album-{index}") for index in range(1, 6))
        invalid_tracks = tuple(
            album_track(index, f"Different {index}", ("Other Artist",))
            for index in range(1, 7)
        )
        client = FakeAlbumLookupClient(
            candidates=candidates,
            tracks_by_album_id={
                "album-1": self.spotify_tracks,
                **{
                    f"album-{index}": invalid_tracks
                    for index in range(2, 6)
                },
            },
        )

        result = resolve_release_with_album(
            source_tracks=self.source_tracks,
            spotify_client=client,
            access_token="access-token",
            search_limit=10,
        )

        self.assertEqual(len(result.decisions), 6)
        self.assertEqual(result.diagnostic, "")
        self.assertFalse(result.used_fallback_track_candidates)
        self.assertEqual(
            client.album_track_requests,
            [("access-token", f"album-{index}") for index in range(1, 6)],
        )

    def test_album_sequence_diagnostic_records_order_gap_artist_and_playability_failures(self) -> None:
        source_tracks = (
            source_track(1, "Anchor One"),
            source_track(2, "Middle"),
            source_track(3, "Anchor Three"),
            source_track(4, "Anchor Four"),
        )
        spotify_tracks = (
            album_track(1, "Anchor Three"),
            album_track(2, "Middle Different", ("Other Artist",)),
            album_track(3, "Anchor One"),
            album_track(4, "Anchor Four", is_playable=False),
            album_track(5, "Extra"),
        )

        diagnostic = analyze_album_sequence(
            source_tracks=source_tracks,
            album=album_candidate(),
            spotify_tracks=spotify_tracks,
        )

        self.assertEqual(diagnostic.exact_anchor_count, 2)
        self.assertEqual(diagnostic.title_anchor_count, 2)
        self.assertFalse(diagnostic.anchor_order_is_valid)
        self.assertEqual(diagnostic.unequal_gap_count, 0)
        self.assertEqual(diagnostic.oversized_gap_count, 0)
        self.assertEqual(diagnostic.positional_artist_mismatch_count, 0)
        self.assertEqual(diagnostic.unusable_spotify_track_count, 1)

    def test_duplicate_search_results_for_one_album_are_fetched_once(self) -> None:
        candidate = album_candidate()
        client = FakeAlbumLookupClient(
            candidates=(candidate, candidate),
            tracks_by_album_id={"album-1": self.spotify_tracks},
        )

        result = resolve_release_with_album(
            source_tracks=self.source_tracks,
            spotify_client=client,
            access_token="access-token",
            search_limit=10,
        )

        self.assertEqual(len(result.decisions), 6)
        self.assertEqual(client.album_track_requests, [("access-token", "album-1")])

    def test_release_lookup_uses_exact_album_id_from_track_candidates_when_album_search_misses(self) -> None:
        recovered_album_id = "recovered-album"
        client = FakeAlbumLookupClient(
            candidates=(album_candidate(album_name="Different Album"),),
            tracks_by_album_id={recovered_album_id: self.spotify_tracks},
        )
        track_candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:recovered-one",
                name="White",
                artists=("Example Artist", "Collaborator"),
                album_name="Numbers and Colours",
                album_id=recovered_album_id,
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:recovered-two",
                name="Grey",
                artists=("Example Artist", "Collaborator"),
                album_name="Numbers and Colours",
                album_id=recovered_album_id,
            ),
        )

        result = resolve_release_with_album(
            source_tracks=self.source_tracks,
            spotify_client=client,
            access_token="access-token",
            search_limit=10,
            fallback_track_candidates=track_candidates,
        )

        self.assertEqual(len(result.decisions), 6)
        self.assertEqual(result.diagnostic, "")
        self.assertEqual(
            client.album_track_requests,
            [("access-token", recovered_album_id)],
        )
        self.assertTrue(
            all(
                "album recovered from cached track-search candidates" in decision.reason
                for decision in result.decisions
            )
        )
        self.assertEqual(
            result.decisions[-1].match_strategy,
            TRACK_CANDIDATE_ALBUM_EXACT_MATCH_STRATEGY,
        )

    def test_release_lookup_ignores_track_candidate_album_id_when_album_name_differs(self) -> None:
        client = FakeAlbumLookupClient(
            candidates=(album_candidate(album_name="Different Album"),),
            tracks_by_album_id={},
        )
        track_candidate = SpotifyTrackCandidate(
            uri="spotify:track:wrong-album",
            name="Final Anchor",
            artists=("Example Artist",),
            album_name="Different Album",
            album_id="wrong-album",
        )

        result = resolve_release_with_album(
            source_tracks=self.source_tracks,
            spotify_client=client,
            access_token="access-token",
            fallback_track_candidates=(track_candidate,),
        )

        self.assertEqual(result.decisions, ())
        self.assertFalse(result.used_fallback_track_candidates)
        self.assertEqual(
            result.diagnostic,
            "Spotify returned no album candidate with a matching title",
        )
        self.assertEqual(client.album_track_requests, [])


class SpotifyCachedAlbumFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_tracks = (
            source_track(1, "Anchor One"),
            source_track(2, "Anchor Two", "Example Artist, Guest"),
            source_track(3, "Anchor Three"),
        )
        self.exact_tracks = (
            album_track(1, "Anchor One"),
            album_track(2, "Anchor Two", ("Example Artist", "Guest")),
            album_track(3, "Anchor Three"),
        )
        self.rejected_tracks = tuple(
            album_track(number, f"Wrong Track {number}", ("Other Artist",))
            for number in range(1, 4)
        )
        self.partial_tracks = (*self.exact_tracks[:2], self.rejected_tracks[2])

    def resolve(
        self,
        direct_ids: tuple[str, ...],
        tracks_by_id: dict[str, tuple[SpotifyAlbumTrack, ...]],
        hints: tuple[SpotifyTrackCandidate, ...],
        *,
        tracks: tuple[PlaylistTrack, ...] | None = None,
    ) -> tuple[SpotifyReleaseAlbumMatchResult, FakeAlbumLookupClient]:
        if tracks is None:
            tracks = self.source_tracks
        client = FakeAlbumLookupClient(
            candidates=tuple(album_candidate(album_id, tracks[0].album_name) for album_id in direct_ids),
            tracks_by_album_id=tracks_by_id,
        )
        result = resolve_release_with_album(
            tracks,
            client,
            "access-token",
            fallback_track_candidates=hints,
        )
        return result, client

    def hint(
        self, album_id: str, album_name: str = "Numbers and Colours",
    ) -> SpotifyTrackCandidate:
        return SpotifyTrackCandidate(
            uri=f"spotify:track:{album_id}-hint",
            name="Anchor One",
            artists=("Example Artist",),
            album_name=album_name,
            album_id=album_id,
        )

    def test_recovers_cached_album_after_rejected_direct_result(self) -> None:
        cached_hint = self.hint("recovered-album")
        result, client = self.resolve(
            ("direct-album",),
            {"direct-album": self.rejected_tracks, "recovered-album": self.exact_tracks},
            (
                self.hint("direct-album"),
                cached_hint,
                replace(cached_hint, uri="spotify:track:another-hint"),
            ),
        )

        self.assertEqual(result.search_count, 1)
        self.assertEqual(client.searches, [("access-token", 'album:"Numbers and Colours"', 10)])
        self.assertEqual(
            client.album_track_requests,
            [("access-token", "direct-album"), ("access-token", "recovered-album")],
        )
        self.assertEqual(tuple(decision.track for decision in result.decisions), self.source_tracks)
        self.assertEqual(
            [decision.spotify_uri for decision in result.decisions],
            ["spotify:track:1", "spotify:track:2", "spotify:track:3"],
        )
        self.assertEqual(
            [decision.match_strategy for decision in result.decisions],
            [TRACK_CANDIDATE_ALBUM_EXACT_MATCH_STRATEGY] * 3,
        )
        self.assertTrue(result.used_fallback_track_candidates)
        self.assertEqual(result.diagnostic, "")

    def test_valid_direct_results_take_precedence_over_cached_hints(self) -> None:
        cases = (
            ("complete", {"direct-album": self.exact_tracks}, 3),
            ("partial", {"direct-album": self.partial_tracks}, 2),
            ("ambiguous", {"direct-album": self.exact_tracks, "other-direct": self.partial_tracks}, 0),
        )
        for label, direct_tracks_by_id, expected_count in cases:
            with self.subTest(label=label):
                result, client = self.resolve(
                    tuple(direct_tracks_by_id),
                    {**direct_tracks_by_id, "recovered-album": self.exact_tracks},
                    (self.hint("recovered-album"),),
                )

                self.assertEqual(len(result.decisions), expected_count)
                self.assertFalse(result.used_fallback_track_candidates)
                self.assertEqual(
                    [album_id for _, album_id in client.album_track_requests],
                    list(direct_tracks_by_id),
                )
                if label == "ambiguous":
                    self.assertIn("2 Spotify album editions passed sequence validation", result.diagnostic)

    def test_records_assessment_when_cached_id_was_already_checked(self) -> None:
        result, client = self.resolve(
            ("direct-album",),
            {"direct-album": self.rejected_tracks},
            (self.hint("direct-album"),),
        )

        self.assertEqual(result.decisions, ())
        self.assertTrue(result.used_fallback_track_candidates)
        self.assertEqual(client.album_track_requests, [("access-token", "direct-album")])

    def test_competing_validated_cached_editions_block_recovery(self) -> None:
        for label, competing_tracks in (("complete", self.exact_tracks), ("partial", self.partial_tracks)):
            with self.subTest(second_edition=label):
                result, client = self.resolve(
                    ("direct-album",),
                    {
                        "direct-album": self.rejected_tracks,
                        "recovered-album": self.exact_tracks,
                        "other-cached": competing_tracks,
                    },
                    (self.hint("recovered-album"), self.hint("other-cached")),
                )

                self.assertEqual(result.decisions, ())
                self.assertIn("2 Spotify album editions passed sequence validation", result.diagnostic)
                self.assertEqual(
                    [album_id for _, album_id in client.album_track_requests],
                    ["direct-album", "recovered-album", "other-cached"],
                )

    def test_cached_album_requires_full_ordered_usable_exact_anchors(self) -> None:
        first, second, third = self.exact_tracks
        cases = (
            ("partial", self.partial_tracks),
            ("positional", (first, replace(second, name="Alternate Title"), third)),
            ("out of order", (second, first, third)),
            ("unplayable", (first, replace(second, is_playable=False), third)),
            ("missing track ID", (first, replace(second, uri="spotify:track:"), third)),
            ("conflicting track ID", (first, replace(second, uri=first.uri), third)),
            ("extra track", (*self.exact_tracks, album_track(4, "Extra Track"))),
        )
        for label, cached_tracks in cases:
            with self.subTest(label=label):
                result, client = self.resolve(
                    ("direct-album",),
                    {"direct-album": self.rejected_tracks, "recovered-album": cached_tracks},
                    (self.hint("recovered-album"),),
                )

                self.assertEqual(result.decisions, ())
                self.assertTrue(result.used_fallback_track_candidates)
                self.assertTrue(result.diagnostic)
                self.assertEqual(
                    [album_id for _, album_id in client.album_track_requests],
                    ["direct-album", "recovered-album"],
                )

    def test_cached_album_requires_strict_unicode_recording_identity(self) -> None:
        album_name = "Numbers and Colours 夜"
        first, second, third = self.exact_tracks
        source_tracks = (
            replace(self.source_tracks[0], album_name=album_name),
            replace(
                self.source_tracks[1],
                album_name=album_name,
                track_name="Anchor Two (Live) 夜",
                artist_name="Example Artist, Guest 夜",
            ),
            replace(self.source_tracks[2], album_name=album_name),
        )
        strict_second = replace(second, name="Anchor Two (Live) 夜", artists=("Example Artist", "Guest 夜"))
        cases = (
            ("album", "Numbers and Colours 日", strict_second),
            ("track", album_name, replace(strict_second, name="Anchor Two (Live) 日")),
            ("artist", album_name, replace(strict_second, artists=("Example Artist", "Guest 日"))),
            ("version punctuation", album_name, replace(strict_second, name="Anchor Two [Live] 夜")),
        )
        for label, cached_album_name, cached_second in cases:
            with self.subTest(label=label):
                result, client = self.resolve(
                    ("direct-album",),
                    {"direct-album": self.rejected_tracks, "recovered-album": (first, cached_second, third)},
                    (self.hint("recovered-album", cached_album_name),),
                    tracks=source_tracks,
                )

                self.assertEqual(result.decisions, ())
                self.assertTrue(result.used_fallback_track_candidates)
                self.assertEqual(result.validation_diagnostics[-1].exact_anchor_count, 3)
                self.assertTrue(result.diagnostic)
                self.assertEqual(client.album_track_requests[-1], ("access-token", "recovered-album"))

    def test_cached_album_requires_consistent_titles_for_the_same_id(self) -> None:
        source_album = "Numbers and Colours 夜"
        source_tracks = tuple(replace(track, album_name=source_album) for track in self.source_tracks)
        matching_hint = self.hint("recovered-album", source_album)
        conflicting_hint = replace(
            matching_hint, uri="spotify:track:other-hint", album_name="Numbers and Colours 日",
        )
        cases = (
            ("matching cached title first", (matching_hint, conflicting_hint), ()),
            ("conflicting cached title first", (conflicting_hint, matching_hint), ()),
            ("conflicting direct title", (matching_hint,), (album_candidate("recovered-album", "Different Album"),)),
        )
        for label, hints, additional_direct in cases:
            with self.subTest(label=label):
                client = FakeAlbumLookupClient(
                    (album_candidate("direct-album", source_album), *additional_direct),
                    {"direct-album": self.rejected_tracks, "recovered-album": self.exact_tracks},
                )
                result = resolve_release_with_album(
                    source_tracks, client, "access-token", fallback_track_candidates=hints,
                )

                self.assertEqual(result.decisions, ())
                self.assertEqual(
                    [album_id for _, album_id in client.album_track_requests],
                    ["direct-album", "recovered-album"],
                )
                self.assertTrue(result.used_fallback_track_candidates)
                self.assertTrue(result.diagnostic)


    def test_direct_and_cached_album_fetches_share_one_limit(self) -> None:
        direct_ids = ("direct-one", "direct-two")
        cached_ids = ("cached-one", "cached-two", "cached-three", "cached-four")
        tracks_by_id: dict[str, tuple[SpotifyAlbumTrack, ...]] = {
            album_id: self.rejected_tracks for album_id in (*direct_ids, *cached_ids)
        }
        tracks_by_id["cached-one"] = self.exact_tracks

        at_limit, at_limit_client = self.resolve(
            direct_ids, tracks_by_id, tuple(self.hint(album_id) for album_id in cached_ids[:3]),
        )
        over_limit, over_limit_client = self.resolve(
            direct_ids, tracks_by_id, tuple(self.hint(album_id) for album_id in cached_ids),
        )

        self.assertEqual(len(at_limit.decisions), 3)
        self.assertEqual(
            [album_id for _, album_id in at_limit_client.album_track_requests],
            [*direct_ids, *cached_ids[:3]],
        )
        self.assertEqual(over_limit.decisions, ())
        self.assertTrue(over_limit.used_fallback_track_candidates)
        self.assertIn("safe maximum is 5", over_limit.diagnostic)
        self.assertEqual([album_id for _, album_id in over_limit_client.album_track_requests], list(direct_ids))


if __name__ == "__main__":
    unittest.main()
