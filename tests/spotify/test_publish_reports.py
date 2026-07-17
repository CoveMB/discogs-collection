import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.matching import (
    ERROR,
    UNMATCHED,
    PlaylistTrack,
    SpotifyTrackCandidate,
)
from publishers.spotify.publish_reports import (
    best_diagnostic_candidate,
    format_publish_review_details,
    rank_diagnostic_candidates,
)
from publishers.spotify.publish_types import MATCH_SOURCE_SEARCH, PlaylistPublishDecision


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
        decision = unmatched_publish_decision(
            track,
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

        details = format_publish_review_details(decision)

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

    def test_unmatched_diagnostic_lists_the_next_two_ranked_candidates(self):
        track = playlist_track(
            track_name="Reson",
            artist_name="DJ Deep",
            album_name="Midnight Funk Association",
        )
        candidates = (
            spotify_candidate(
                uri="spotify:track:wrong",
                name="Resonate",
                artists=("Other Artist",),
                album_name="Other Album",
            ),
            spotify_candidate(
                uri="spotify:track:third",
                name="Reason",
                artists=("DJ Deep",),
                album_name="Other Album",
            ),
            spotify_candidate(
                uri="spotify:track:best",
                name="Resin",
                artists=("DJ Deep",),
                album_name="Midnight Funk Association",
            ),
            spotify_candidate(
                uri="spotify:track:second",
                name="Resonance",
                artists=("DJ Deep",),
                album_name="Midnight Funk Association",
            ),
        )
        decision = unmatched_publish_decision(track, review_candidates=candidates)

        ranked = rank_diagnostic_candidates(track, candidates)
        details = format_publish_review_details(decision)

        self.assertEqual(tuple(candidate.uri for candidate in ranked[:3]), (
            "spotify:track:best",
            "spotify:track:second",
            "spotify:track:third",
        ))
        self.assertIn("  Other diagnostic candidates:", details)
        self.assertIn(
            "    - spotify:track:second | DJ Deep | Resonance | Midnight Funk Association",
            details,
        )
        self.assertIn(
            "    - spotify:track:third | DJ Deep | Reason | Other Album",
            details,
        )
        self.assertNotIn("spotify:track:wrong | Other Artist", "\n".join(details))

    def test_unmatched_diagnostic_distinguishes_partial_artist_match(self):
        track = playlist_track(
            track_name="Alpha",
            artist_name="Alpha Artist, Guest Artist",
            album_name="Alpha Album",
        )
        decision = unmatched_publish_decision(
            track,
            review_candidates=(
                spotify_candidate(
                    uri="spotify:track:alpha",
                    name="Alpha",
                    artists=("Alpha Artist",),
                    album_name="Alpha Album",
                ),
            ),
        )

        details = format_publish_review_details(decision)

        self.assertIn(
            "  Diagnostic: title matches; at least one source artist matches; album matches.",
            details,
        )
        self.assertIn(
            "    Artist: partial match (Discogs: Alpha Artist, Guest Artist; Spotify: Alpha Artist)",
            details,
        )

    def test_unmatched_diagnostic_explains_when_no_candidates_were_returned(self):
        decision = unmatched_publish_decision(
            playlist_track(),
            reason="Spotify returned no candidates",
        )

        details = format_publish_review_details(decision)

        self.assertIn("  Spotify returned 0 candidates.", details)
        self.assertIn(
            "  Diagnostic: no Spotify candidates were returned by any query.",
            details,
        )

    def test_publish_error_detail_keeps_multiline_error_readable(self):
        track = playlist_track()
        decision = PlaylistPublishDecision(
            playlist_name=track.playlist_name,
            target_playlist_name=track.playlist_name,
            track=track,
            status=ERROR,
            spotify_uri="",
            reason='Spotify search failed with status 429: {\n  "error": "Too many requests"\n}',
            match_source=MATCH_SOURCE_SEARCH,
        )

        details = format_publish_review_details(decision)

        self.assertIn(
            '  Why: Spotify search failed with status 429: { "error": "Too many requests" }',
            details,
        )
        self.assertNotIn('Why: Spotify search failed with status 429: {\n', "\n".join(details))


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


def unmatched_publish_decision(
    track: PlaylistTrack,
    review_candidates: tuple[SpotifyTrackCandidate, ...] = (),
    reason: str = "no candidates matched track, artist, and album",
) -> PlaylistPublishDecision:
    return PlaylistPublishDecision(
        playlist_name=track.playlist_name,
        target_playlist_name=track.playlist_name,
        track=track,
        status=UNMATCHED,
        spotify_uri="",
        reason=reason,
        match_source=MATCH_SOURCE_SEARCH,
        review_candidates=review_candidates,
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
