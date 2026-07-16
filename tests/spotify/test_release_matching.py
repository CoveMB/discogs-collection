import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.matching import PlaylistTrack
from publishers.spotify.release_matching import (
    ALBUM_EXACT_TRACK_MATCH_STRATEGY,
    ALBUM_POSITION_MATCH_STRATEGY,
    SpotifyAlbumCandidate,
    SpotifyAlbumTrack,
    build_spotify_album_search_query,
    choose_validated_album_match,
    match_release_tracks_to_album,
    resolve_release_with_album,
)


def source_track(
    track_number: int,
    track_name: str,
    artist_name: str = "Example Artist",
) -> PlaylistTrack:
    return PlaylistTrack(
        playlist_name="Techno",
        release_id="release-1",
        album_name="Numbers and Colours",
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


def album_candidate(album_id: str = "album-1") -> SpotifyAlbumCandidate:
    return SpotifyAlbumCandidate(
        album_id=album_id,
        uri=f"spotify:album:{album_id}",
        name="Numbers and Colours",
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
        self.assertEqual(client.searches, [("access-token", 'album:"Numbers and Colours"', 10)])
        self.assertEqual(client.album_track_requests, [("access-token", "album-1")])

    def test_more_than_three_exact_album_candidates_falls_back_without_fetching(self) -> None:
        candidates = tuple(album_candidate(f"album-{index}") for index in range(1, 5))
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


if __name__ == "__main__":
    unittest.main()
