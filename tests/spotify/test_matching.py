import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.matching import (
    PlaylistTrack,
    SpotifyTrackCandidate,
    build_spotify_track_search_query,
    choose_best_track_match,
)


class SpotifyMatchingTests(unittest.TestCase):
    def test_builds_structured_search_query_from_track_artist_and_album(self):
        track = PlaylistTrack(
            playlist_name="Discogs - Breakbeat",
            release_id="111",
            album_name="Alpha Album",
            track_number="1",
            track_name="Alpha One",
            artist_name="Alpha Artist",
            spotify_search_query="Alpha Artist Alpha One Alpha Album",
        )

        query = build_spotify_track_search_query(track)

        self.assertEqual(query, 'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"')

    def test_falls_back_to_existing_search_query_when_structured_fields_are_missing(self):
        track = PlaylistTrack(
            playlist_name="Discogs - Breakbeat",
            release_id="111",
            album_name="",
            track_number="1",
            track_name="",
            artist_name="",
            spotify_search_query="Alpha Artist Alpha One Alpha Album",
        )

        query = build_spotify_track_search_query(track)

        self.assertEqual(query, "Alpha Artist Alpha One Alpha Album")

    def test_accepts_single_candidate_matching_track_artist_and_album(self):
        track = PlaylistTrack(
            playlist_name="Discogs - Breakbeat",
            release_id="111",
            album_name="Alpha Album",
            track_number="1",
            track_name="Alpha One",
            artist_name="Alpha Artist",
            spotify_search_query="Alpha Artist Alpha One Alpha Album",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:alpha",
            name="Alpha One",
            artists=("Alpha Artist",),
            album_name="Alpha Album",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:alpha")
        self.assertEqual(decision.reason, "track, artist, and album matched")

    def test_rejects_candidate_when_parenthetical_version_text_differs(self):
        track = PlaylistTrack(
            playlist_name="Discogs - Breakbeat",
            release_id="111",
            album_name="Alpha Album",
            track_number="1",
            track_name="Alpha One (Original Mix)",
            artist_name="Alpha Artist",
            spotify_search_query="Alpha Artist Alpha One Original Mix Alpha Album",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:alpha",
            name="Alpha One",
            artists=("Alpha Artist",),
            album_name="Alpha Album",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")

    def test_marks_multiple_matching_candidates_as_ambiguous(self):
        track = PlaylistTrack(
            playlist_name="Discogs - Breakbeat",
            release_id="111",
            album_name="Alpha Album",
            track_number="1",
            track_name="Alpha One",
            artist_name="Alpha Artist",
            spotify_search_query="Alpha Artist Alpha One Alpha Album",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:alpha-1",
                name="Alpha One",
                artists=("Alpha Artist",),
                album_name="Alpha Album",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:alpha-2",
                name="Alpha One",
                artists=("Alpha Artist",),
                album_name="Alpha Album",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(decision.reason, "2 candidates matched track, artist, and album")

    def test_rejects_candidates_that_do_not_match_track_artist_and_album(self):
        track = PlaylistTrack(
            playlist_name="Discogs - Breakbeat",
            release_id="111",
            album_name="Alpha Album",
            track_number="1",
            track_name="Alpha One",
            artist_name="Alpha Artist",
            spotify_search_query="Alpha Artist Alpha One Alpha Album",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:wrong",
            name="Alpha Two",
            artists=("Alpha Artist",),
            album_name="Alpha Album",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(decision.reason, "no candidates matched track, artist, and album")


if __name__ == "__main__":
    unittest.main()
