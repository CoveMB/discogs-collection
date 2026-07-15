import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.matching import (
    UNMATCHED,
    PlaylistTrack,
    SpotifyTrackCandidate,
    TrackMatchDecision,
)
from publishers.spotify.publish_reports import (
    best_diagnostic_candidate,
    format_unmatched_track_details,
)


class PublishReportDiagnosticTests(unittest.TestCase):
    def test_best_diagnostic_candidate_uses_identity_evidence_not_api_order(self):
        track = playlist_track(
            track_name="Reson",
            artist_name="DJ Deep",
            album_name="Midnight Funk Association",
        )
        candidates = (
            spotify_candidate(
                uri="spotify:track:first",
                name="Resonate - Giorgio Gigli Remix",
                artists=("Giorgio Gigli",),
                album_name="Resonate Remixes",
            ),
            spotify_candidate(
                uri="spotify:track:eon",
                name="Eon",
                artists=("DJ Deep",),
                album_name="Midnight Funk Association",
            ),
            spotify_candidate(
                uri="spotify:track:resin",
                name="Resin",
                artists=("DJ Deep",),
                album_name="Midnight Funk Association",
            ),
        )

        result = best_diagnostic_candidate(track, candidates)

        self.assertEqual(result.uri, "spotify:track:resin")
        self.assertEqual(
            best_diagnostic_candidate(track, tuple(reversed(candidates))).uri,
            "spotify:track:resin",
        )

    def test_unmatched_diagnostic_explains_ranked_candidate_evidence(self):
        track = playlist_track(
            track_name="Reson",
            artist_name="DJ Deep",
            album_name="Midnight Funk Association",
        )
        decision = TrackMatchDecision(
            track=track,
            status=UNMATCHED,
            spotify_uri="",
            reason="no candidates matched track, artist, and album",
            review_candidates=(
                spotify_candidate(
                    uri="spotify:track:first",
                    name="Resonate - Giorgio Gigli Remix",
                    artists=("Giorgio Gigli",),
                    album_name="Resonate Remixes",
                ),
                spotify_candidate(
                    uri="spotify:track:resin",
                    name="Resin",
                    artists=("DJ Deep",),
                    album_name="Midnight Funk Association",
                ),
            ),
        )

        details = format_unmatched_track_details(decision)

        self.assertIn(
            "  Best diagnostic candidate: spotify:track:resin | DJ Deep | Resin | Midnight Funk Association",
            details,
        )
        self.assertIn(
            "  Diagnostic: title differs by 1 edit; artist set matches; album matches.",
            details,
        )
        self.assertIn("    Title edit distance: 1", details)
        self.assertIn(
            "    Artist: matches exactly (Discogs: DJ Deep; Spotify: DJ Deep)",
            details,
        )

    def test_unmatched_diagnostic_distinguishes_partial_artist_match(self):
        track = playlist_track(
            track_name="Alpha",
            artist_name="Alpha Artist, Guest Artist",
            album_name="Alpha Album",
        )
        decision = TrackMatchDecision(
            track=track,
            status=UNMATCHED,
            spotify_uri="",
            reason="no candidates matched track, artist, and album",
            review_candidates=(
                spotify_candidate(
                    uri="spotify:track:alpha",
                    name="Alpha",
                    artists=("Alpha Artist",),
                    album_name="Alpha Album",
                ),
            ),
        )

        details = format_unmatched_track_details(decision)

        self.assertIn(
            "  Diagnostic: title matches; at least one source artist matches; album matches.",
            details,
        )
        self.assertIn(
            "    Artist: partial match (Discogs: Alpha Artist, Guest Artist; Spotify: Alpha Artist)",
            details,
        )

    def test_unmatched_diagnostic_explains_when_no_candidates_were_returned(self):
        decision = TrackMatchDecision(
            track=playlist_track(),
            status=UNMATCHED,
            spotify_uri="",
            reason="Spotify returned no candidates",
        )

        details = format_unmatched_track_details(decision)

        self.assertIn("  Spotify returned 0 candidates.", details)
        self.assertIn(
            "  Diagnostic: no Spotify candidates were returned by any query.",
            details,
        )


def playlist_track(
    track_name: str = "Alpha One",
    artist_name: str = "Alpha Artist",
    album_name: str = "Alpha Album",
) -> PlaylistTrack:
    return PlaylistTrack(
        playlist_name="Discogs - House",
        release_id="111",
        album_name=album_name,
        track_number="1",
        track_name=track_name,
        artist_name=artist_name,
        spotify_search_query="",
    )


def spotify_candidate(
    uri: str,
    name: str,
    artists: tuple[str, ...],
    album_name: str,
) -> SpotifyTrackCandidate:
    return SpotifyTrackCandidate(
        uri=uri,
        name=name,
        artists=artists,
        album_name=album_name,
    )


if __name__ == "__main__":
    unittest.main()
