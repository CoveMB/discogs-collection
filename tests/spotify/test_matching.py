import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.matching import (
    PlaylistTrack,
    SpotifyTrackCandidate,
    build_spotify_track_search_queries,
    build_spotify_track_search_query,
    choose_best_track_match,
    normalize_music_text,
)


def source_track(
    *,
    track_name: str,
    artist_name: str,
    album_name: str,
    spotify_search_query: str | None = None,
    playlist_name: str = "Discogs - Test",
    release_id: str = "111",
    track_number: str = "1",
) -> PlaylistTrack:
    return PlaylistTrack(
        playlist_name=playlist_name,
        release_id=release_id,
        album_name=album_name,
        track_number=track_number,
        track_name=track_name,
        artist_name=artist_name,
        spotify_search_query=(
            spotify_search_query
            if spotify_search_query is not None
            else " ".join(value for value in (artist_name, track_name, album_name) if value)
        ),
    )


class SpotifyMatchingTests(unittest.TestCase):
    def test_normalizes_equivalent_unicode_punctuation_as_separators(self):
        equivalent_values = (
            ("Marcel’s Walk", "Marcel's Walk"),
            ("Marcelʼs Walk", "Marcel's Walk"),
            ("Hawaiʻi", "Hawai'i"),
            ("Signal—Path", "Signal-Path"),
            ("“Signal” Path", '"Signal" Path'),
            ("Signal…Path", "Signal...Path"),
        )

        for unicode_value, ascii_value in equivalent_values:
            with self.subTest(unicode_value=unicode_value, ascii_value=ascii_value):
                self.assertEqual(normalize_music_text(unicode_value), normalize_music_text(ascii_value))

    def test_normalization_does_not_ignore_missing_punctuation(self):
        self.assertNotEqual(normalize_music_text("Marcel’s Walk"), normalize_music_text("Marcels Walk"))

    def test_unicode_symbols_preserve_token_boundaries(self):
        self.assertEqual(normalize_music_text("Signal♥Path"), "signal path")
        self.assertNotEqual(normalize_music_text("Signal♥Path"), normalize_music_text("SignalPath"))

    def test_accepts_candidate_with_equivalent_typographic_apostrophe(self):
        track = source_track(
            album_name="The Poet And The Muse",
            track_name="Marcel’s Walk",
            artist_name="Mathys Lenne",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:marcels-walk",
            name="Marcel's Walk",
            artists=("Mathys Lenne",),
            album_name="The Poet And The Muse",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:marcels-walk")
        self.assertEqual(decision.reason, "track, artist, and album matched")

    def test_builds_structured_search_query_from_track_artist_and_album(self):
        track = source_track(
            album_name="Alpha Album",
            track_name="Alpha One",
            artist_name="Alpha Artist",
            spotify_search_query="Alpha Artist Alpha One Alpha Album",
        )

        query = build_spotify_track_search_query(track)

        self.assertEqual(query, 'track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"')

    def test_builds_search_query_ladder_from_track_artist_album_and_raw_query(self):
        track = source_track(
            album_name="Alpha EP",
            track_name="Alpha One",
            artist_name="Alpha Artist, Beta Artist",
            spotify_search_query="Alpha Artist Alpha One Alpha EP",
        )

        queries = build_spotify_track_search_queries(track)

        self.assertEqual(
            queries,
            (
                'track:"Alpha One" artist:"Alpha Artist, Beta Artist" album:"Alpha EP"',
                'track:"Alpha One" artist:"Alpha Artist, Beta Artist"',
                'track:"Alpha One" artist:"Alpha Artist"',
                'track:"Alpha One" artist:"Beta Artist"',
                "Alpha Artist, Beta Artist Alpha One",
                "Alpha Artist Alpha One Alpha EP",
            ),
        )

    def test_search_query_ladder_includes_plain_artist_and_title_before_raw_query(self):
        track = source_track(
            album_name="False Hope",
            track_name="Loosing Time",
            artist_name="Heap",
            spotify_search_query="Heap Loosing Time False Hope",
        )

        queries = build_spotify_track_search_queries(track)

        self.assertEqual(
            queries,
            (
                'track:"Loosing Time" artist:"Heap" album:"False Hope"',
                'track:"Loosing Time" artist:"Heap"',
                "Heap Loosing Time",
                "Heap Loosing Time False Hope",
            ),
        )

    def test_search_query_ladder_adds_original_mix_title_fallbacks_before_raw_query(self):
        track = source_track(
            album_name="A Thousand Faces",
            track_name="Skull Shrine (Original Mix)",
            artist_name="Feral",
            spotify_search_query="Feral Skull Shrine (Original Mix) A Thousand Faces",
        )

        queries = build_spotify_track_search_queries(track)

        self.assertEqual(
            queries,
            (
                'track:"Skull Shrine (Original Mix)" artist:"Feral" album:"A Thousand Faces"',
                'track:"Skull Shrine (Original Mix)" artist:"Feral"',
                "Feral Skull Shrine (Original Mix)",
                'track:"Skull Shrine" artist:"Feral" album:"A Thousand Faces"',
                'track:"Skull Shrine" artist:"Feral"',
                "Feral Skull Shrine",
                "Feral Skull Shrine (Original Mix) A Thousand Faces",
            ),
        )

    def test_search_query_ladder_adds_featured_credit_title_fallbacks_before_raw_query(self):
        track = source_track(
            album_name="Carl Gari",
            track_name="Swim feat. Polygonia",
            artist_name="Carl Gari",
            spotify_search_query="Carl Gari Swim feat. Polygonia Carl Gari",
        )

        queries = build_spotify_track_search_queries(track)

        self.assertEqual(
            queries,
            (
                'track:"Swim feat. Polygonia" artist:"Carl Gari" album:"Carl Gari"',
                'track:"Swim feat. Polygonia" artist:"Carl Gari"',
                "Carl Gari Swim feat. Polygonia",
                'track:"Swim" artist:"Carl Gari" album:"Carl Gari"',
                'track:"Swim" artist:"Carl Gari"',
                "Carl Gari Swim",
                "Carl Gari Swim feat. Polygonia Carl Gari",
            ),
        )

    def test_search_query_ladder_supports_observed_featured_credit_forms(self):
        cases = (
            ("Signal Path ft. Guest One", "Signal Path"),
            ("Night Drive (feat. Guest Two)", "Night Drive"),
            ("((( soft pressure ))) [feat. Guest Three]", "((( soft pressure )))"),
            (
                "Summer Exit (feat. Guest Four) (Interlude)",
                "Summer Exit (Interlude)",
            ),
            (
                "Magic Signal (Producer's 'Late Mix' feat. Guest Five)",
                "Magic Signal (Producer's 'Late Mix')",
            ),
        )
        for track_name, base_title in cases:
            with self.subTest(track_name=track_name):
                track = source_track(
                    album_name="Test Album",
                    track_name=track_name,
                    artist_name="Main Artist",
                    spotify_search_query=f"Main Artist {track_name} Test Album",
                )

                queries = build_spotify_track_search_queries(track)

                self.assertIn(f'track:"{base_title}" artist:"Main Artist"', queries)

    def test_search_query_ladder_does_not_strip_unsupported_featured_credit_forms(self):
        for track_name in (
            "Swim featuring Polygonia",
            "Featuring Phat Kat",
            "Swim feat.",
            "Swim (ft. Polygonia)",
            "Swim feat. Polygonia (Original Mix)",
        ):
            with self.subTest(track_name=track_name):
                track = source_track(
                    album_name="Carl Gari",
                    track_name=track_name,
                    artist_name="Carl Gari",
                    spotify_search_query=f"Carl Gari {track_name} Carl Gari",
                )

                queries = build_spotify_track_search_queries(track)

                self.assertNotIn('track:"Swim" artist:"Carl Gari"', queries)

    def test_search_query_ladder_does_not_strip_other_parenthetical_text(self):
        track = source_track(
            album_name="Alpha Album",
            track_name="Alpha One (Club Mix)",
            artist_name="Alpha Artist",
            spotify_search_query="Alpha Artist Alpha One (Club Mix) Alpha Album",
        )

        queries = build_spotify_track_search_queries(track)

        self.assertNotIn('track:"Alpha One" artist:"Alpha Artist"', queries)

    def test_search_query_ladder_adds_version_substitute_title_after_exact_queries(self):
        track = source_track(
            album_name="Colt EP",
            track_name="Airless (LIVE)",
            artist_name="Dense & Pika",
            spotify_search_query="Dense & Pika Airless (LIVE) Colt EP",
        )

        queries = build_spotify_track_search_queries(track)

        self.assertEqual(
            queries,
            (
                'track:"Airless (LIVE)" artist:"Dense & Pika" album:"Colt EP"',
                'track:"Airless (LIVE)" artist:"Dense & Pika"',
                "Dense & Pika Airless (LIVE)",
                'track:"Airless" artist:"Dense & Pika" album:"Colt EP"',
                'track:"Airless" artist:"Dense & Pika"',
                "Dense & Pika Airless",
                "Dense & Pika Airless (LIVE) Colt EP",
            ),
        )
        self.assertNotIn("Alpha Artist Alpha One", queries)

    def test_search_query_ladder_strips_original_mix_case_and_spacing_variations(self):
        for suffix in (
            "(Original mix)",
            "(original mix)",
            "(ORIGINAL MIX)",
            "( Original   Mix )",
        ):
            with self.subTest(suffix=suffix):
                track = source_track(
                    album_name="A Thousand Faces",
                    track_name=f"Skull Shrine {suffix}",
                    artist_name="Feral",
                    spotify_search_query=f"Feral Skull Shrine {suffix} A Thousand Faces",
                )

                queries = build_spotify_track_search_queries(track)

                self.assertIn('track:"Skull Shrine" artist:"Feral"', queries)
                self.assertIn("Feral Skull Shrine", queries)

    def test_falls_back_to_existing_search_query_when_structured_fields_are_missing(self):
        track = source_track(
            album_name="",
            track_name="",
            artist_name="",
            spotify_search_query="Alpha Artist Alpha One Alpha Album",
        )

        query = build_spotify_track_search_query(track)

        self.assertEqual(query, "Alpha Artist Alpha One Alpha Album")

    def test_accepts_single_candidate_matching_track_artist_and_album(self):
        track = source_track(
            album_name="Alpha Album",
            track_name="Alpha One",
            artist_name="Alpha Artist",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:alpha",
            name="Alpha One",
            artists=("Alpha Artist",),
            album_name="Alpha Album",
        )
        search_queries = ('track:"Alpha One" artist:"Alpha Artist"',)

        decision = choose_best_track_match(track, (candidate,), search_queries)

        self.assertIs(decision.track, track)
        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:alpha")
        self.assertEqual(decision.reason, "track, artist, and album matched")
        self.assertIs(decision.candidate, candidate)
        self.assertEqual(decision.review_candidates, (candidate,))
        self.assertEqual(decision.search_queries, search_queries)
        self.assertEqual(decision.match_strategy, "")

    def test_accepts_spotify_title_with_leading_in_when_artist_and_album_match(self):
        track = source_track(
            album_name="Environment Control",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:dark-territory",
            name="In Dark Territory",
            artists=("Polar Inertia",),
            album_name="Environment Control",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:dark-territory")
        self.assertEqual(
            decision.reason,
            "track title differed only by Spotify's leading 'in'; artist and album matched",
        )

    def test_leading_in_title_variant_requires_matching_artist_and_album(self):
        track = source_track(
            album_name="Environment Control",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:wrong-artist",
                name="In Dark Territory",
                artists=("Other Artist",),
                album_name="Environment Control",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:wrong-album",
                name="In Dark Territory",
                artists=("Polar Inertia",),
                album_name="Other Album",
            ),
        )

        for candidate in candidates:
            with self.subTest(candidate_uri=candidate.uri):
                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "unmatched")
                self.assertEqual(decision.spotify_uri, "")

    def test_leading_in_title_variant_requires_a_nonempty_album(self):
        track = source_track(
            album_name="",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:dark-territory",
            name="In Dark Territory",
            artists=("Polar Inertia",),
            album_name="",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")

    def test_leading_in_title_variant_rejects_broader_title_changes(self):
        cases = (
            ("Dark Territory", "Beyond Dark Territory"),
            ("Dark Territory", "In Dark Territory Remix"),
            ("Dark Territory", "Dark In Territory"),
            ("Love", "In Love"),
            ("In Dark Territory", "Dark Territory"),
        )

        for source_title, spotify_title in cases:
            with self.subTest(source_title=source_title, spotify_title=spotify_title):
                track = source_track(
                    album_name="Environment Control",
                    track_name=source_title,
                    artist_name="Polar Inertia",
                )
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:candidate",
                    name=spotify_title,
                    artists=("Polar Inertia",),
                    album_name="Environment Control",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "unmatched")
                self.assertEqual(decision.spotify_uri, "")

    def test_exact_title_match_precedes_leading_in_title_variant(self):
        track = source_track(
            album_name="Environment Control",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:leading-in",
                name="In Dark Territory",
                artists=("Polar Inertia",),
                album_name="Environment Control",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:exact",
                name="Dark Territory",
                artists=("Polar Inertia",),
                album_name="Environment Control",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:exact")
        self.assertEqual(decision.reason, "track, artist, and album matched")

    def test_marks_multiple_leading_in_title_variants_as_ambiguous(self):
        track = source_track(
            album_name="Environment Control",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
        )
        candidates = tuple(
            SpotifyTrackCandidate(
                uri=f"spotify:track:dark-territory-{candidate_number}",
                name="In Dark Territory",
                artists=("Polar Inertia",),
                album_name="Environment Control",
            )
            for candidate_number in (1, 2)
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(
            decision.reason,
            "2 candidates had Spotify's leading 'in' title variant and matched artist and album",
        )

    def test_can_defer_leading_in_title_variant_until_search_ladder_finishes(self):
        track = source_track(
            album_name="Environment Control",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:dark-territory",
            name="In Dark Territory",
            artists=("Polar Inertia",),
            album_name="Environment Control",
        )

        decision = choose_best_track_match(
            track,
            (candidate,),
            allow_leading_in_title_variant=False,
        )

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")

    def test_accepts_unique_candidate_matching_track_and_artist_when_album_differs(self):
        track = source_track(
            album_name="Discogs EP",
            track_name="Alpha One",
            artist_name="Alpha Artist",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:alpha",
            name="Alpha One",
            artists=("Alpha Artist",),
            album_name="Spotify Single",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:alpha")
        self.assertEqual(decision.reason, "track and artist matched; album differed")

    def test_marks_multiple_track_and_artist_matches_as_ambiguous_when_album_differs(self):
        track = source_track(
            album_name="Discogs EP",
            track_name="Alpha One",
            artist_name="Alpha Artist",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:alpha-single",
                name="Alpha One",
                artists=("Alpha Artist",),
                album_name="Spotify Single",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:alpha-compilation",
                name="Alpha One",
                artists=("Alpha Artist",),
                album_name="Compilation",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(decision.reason, "2 candidates matched track and artist")

    def test_deduplicates_candidates_by_spotify_uri_before_matching(self):
        track = source_track(
            album_name="Alpha Album",
            track_name="Alpha One",
            artist_name="Alpha Artist",
        )
        duplicate_candidate = SpotifyTrackCandidate(
            uri="spotify:track:alpha",
            name="Alpha One",
            artists=("Alpha Artist",),
            album_name="Alpha Album",
        )

        decision = choose_best_track_match(track, (duplicate_candidate, duplicate_candidate))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:alpha")

    def test_accepts_featured_credit_relocated_to_spotify_artists(self):
        track = source_track(
            album_name="Carl Gari",
            track_name="Swim feat. Polygonia",
            artist_name="Carl Gari",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:swim",
            name="Swim",
            artists=("Carl Gari", "Polygonia"),
            album_name="Carl Gari",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:swim")
        self.assertEqual(
            decision.reason,
            "track matched after moving source featured credit to Spotify artists; "
            "source artist, featured artist, and album matched",
        )

    def test_accepts_featured_credit_marker_case_and_spacing_variations(self):
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:swim",
            name="Swim",
            artists=("Carl Gari", "Polygonia"),
            album_name="Carl Gari",
        )

        for track_name in ("Swim FEAT. Polygonia", "Swim   Feat.   Polygonia"):
            with self.subTest(track_name=track_name):
                track = source_track(
                    album_name="Carl Gari",
                    track_name=track_name,
                    artist_name="Carl Gari",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "matched")
                self.assertEqual(decision.spotify_uri, "spotify:track:swim")

    def test_accepts_observed_featured_credit_forms(self):
        cases = (
            (
                "Signal Path ft. Guest One",
                "Signal Path",
                "Main One, Main Two",
                ("Main One", "Main Two", "Guest One"),
            ),
            (
                "Night Drive (feat. Guest Two)",
                "Night Drive",
                "Main Group & Partner",
                ("Main Group & Partner", "Guest Two"),
            ),
            (
                "((( soft pressure ))) [feat. Guest Three]",
                "((( soft pressure )))",
                "Main Artist",
                ("Main Artist", "Guest Three"),
            ),
            (
                "Summer Exit (feat. Guest Four) (Interlude)",
                "Summer Exit (Interlude)",
                "Guest Four, Main Artist",
                ("Main Artist", "Guest Four"),
            ),
            (
                "Magic Signal (Producer's 'Late Mix' feat. Guest Five)",
                "Magic Signal (Producer's 'Late Mix')",
                "Main Artist",
                ("Main Artist", "Guest Five"),
            ),
        )
        for track_name, spotify_title, source_artists, spotify_artists in cases:
            with self.subTest(track_name=track_name):
                track = source_track(
                    album_name="Test Album",
                    track_name=track_name,
                    artist_name=source_artists,
                )
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:test",
                    name=spotify_title,
                    artists=spotify_artists,
                    album_name="Test Album",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "matched")
                self.assertEqual(decision.spotify_uri, "spotify:track:test")

    def test_accepts_multiple_featured_artists_credited_separately(self):
        track = source_track(
            album_name="Test Album",
            track_name="Where Are You Now (feat. Guest Six & Guest Seven)",
            artist_name="Guest Six, Guest Seven, Main Artist",
        )
        for spotify_artists in (
            ("Main Artist", "Guest Six", "Guest Seven"),
            ("Main Artist", "Guest Six & Guest Seven"),
        ):
            with self.subTest(spotify_artists=spotify_artists):
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:test",
                    name="Where Are You Now",
                    artists=spotify_artists,
                    album_name="Test Album",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "matched")
                self.assertEqual(decision.spotify_uri, "spotify:track:test")

        partial_candidate = SpotifyTrackCandidate(
            uri="spotify:track:partial",
            name="Where Are You Now",
            artists=("Main Artist", "Guest Six"),
            album_name="Test Album",
        )

        partial_decision = choose_best_track_match(track, (partial_candidate,))

        self.assertEqual(partial_decision.status, "unmatched")
        self.assertEqual(partial_decision.spotify_uri, "")

    def test_rejects_featured_credit_candidate_without_non_featured_source_artist(self):
        track = source_track(
            album_name="Test Album",
            track_name="The Reason (feat. Guest Eight)",
            artist_name="Guest Eight, Main Artist",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:test",
            name="The Reason",
            artists=("Guest Eight",),
            album_name="Test Album",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")

    def test_rejects_featured_credit_when_source_has_no_non_featured_artist(self):
        track = source_track(
            album_name="Test Album",
            track_name="The Reason (feat. Guest Eight)",
            artist_name="Guest Eight",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:test",
            name="The Reason",
            artists=("Guest Eight",),
            album_name="Test Album",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")

    def test_accepts_relocated_featured_credit_when_album_differs(self):
        track = source_track(
            album_name="Carl Gari",
            track_name="Swim feat. Polygonia",
            artist_name="Carl Gari",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:swim",
            name="Swim",
            artists=("Carl Gari", "Polygonia"),
            album_name="Swim",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:swim")
        self.assertEqual(
            decision.reason,
            "track matched after moving source featured credit to Spotify artists; "
            "source and featured artists matched; album differed",
        )

    def test_rejects_relocated_featured_credit_without_both_spotify_artists(self):
        track = source_track(
            album_name="Carl Gari",
            track_name="Swim feat. Polygonia",
            artist_name="Carl Gari",
        )

        for artists in (("Carl Gari",), ("Carl Gari", "Different Artist"), ("Polygonia",)):
            with self.subTest(artists=artists):
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:swim",
                    name="Swim",
                    artists=artists,
                    album_name="Carl Gari",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "unmatched")
                self.assertEqual(decision.spotify_uri, "")

    def test_prefers_exact_featured_title_over_relocated_credit_candidate(self):
        track = source_track(
            album_name="Carl Gari",
            track_name="Swim feat. Polygonia",
            artist_name="Carl Gari",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:swim",
                name="Swim",
                artists=("Carl Gari", "Polygonia"),
                album_name="Carl Gari",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:swim-featured-title",
                name="Swim feat. Polygonia",
                artists=("Carl Gari",),
                album_name="Carl Gari",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:swim-featured-title")
        self.assertEqual(decision.reason, "track, artist, and album matched")

    def test_marks_multiple_relocated_featured_credit_candidates_as_ambiguous(self):
        track = source_track(
            album_name="Carl Gari",
            track_name="Swim feat. Polygonia",
            artist_name="Carl Gari",
        )
        candidates = tuple(
            SpotifyTrackCandidate(
                uri=f"spotify:track:swim-{index}",
                name="Swim",
                artists=("Carl Gari", "Polygonia"),
                album_name="Carl Gari",
            )
            for index in range(2)
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(
            decision.reason,
            "2 candidates matched after moving source featured credit to Spotify artists and matching album",
        )

    def test_accepts_featured_credit_present_only_in_spotify_title(self):
        track = source_track(
            album_name="Test Album",
            track_name="Signal Path",
            artist_name="Main Artist",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:signal-path",
            name="Signal Path feat. Guest One",
            artists=("Main Artist", "Guest One"),
            album_name="Test Album",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:signal-path")
        self.assertEqual(
            decision.reason,
            "track matched after removing Spotify featured credit from track title; "
            "source artist, featured artist, and album matched",
        )

    def test_accepts_supported_featured_credit_forms_present_only_on_spotify(self):
        cases = (
            ("Signal Path", "Signal Path ft. Guest One"),
            ("Night Drive", "Night Drive (feat. Guest One)"),
            ("Soft Pressure", "Soft Pressure [feat. Guest One]"),
            ("Summer Exit (Interlude)", "Summer Exit (feat. Guest One) (Interlude)"),
            (
                "Magic Signal (Producer's 'Late Mix')",
                "Magic Signal (Producer's 'Late Mix' feat. Guest One)",
            ),
        )
        for source_title, spotify_title in cases:
            with self.subTest(spotify_title=spotify_title):
                track = source_track(
                    album_name="Test Album",
                    track_name=source_title,
                    artist_name="Main Artist",
                )
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:test",
                    name=spotify_title,
                    artists=("Main Artist", "Guest One"),
                    album_name="Test Album",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "matched")
                self.assertEqual(decision.spotify_uri, "spotify:track:test")

    def test_rejects_unsupported_featured_credit_forms_present_only_on_spotify(self):
        track = source_track(
            album_name="Test Album",
            track_name="Signal Path",
            artist_name="Main Artist",
        )
        for spotify_title in (
            "Signal Path featuring Guest One",
            "Signal Path feat.",
            "Signal Path (ft. Guest One)",
            "Signal Path feat. Guest One (Original Mix)",
        ):
            with self.subTest(spotify_title=spotify_title):
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:test",
                    name=spotify_title,
                    artists=("Main Artist", "Guest One"),
                    album_name="Test Album",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "unmatched")
                self.assertEqual(decision.spotify_uri, "")

    def test_one_sided_featured_credit_fallback_does_not_reconcile_credits_on_both_titles(self):
        track = source_track(
            album_name="Test Album",
            track_name="Signal Path feat. Guest One",
            artist_name="Main Artist",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:test",
            name="Signal Path ft. Guest One",
            artists=("Main Artist", "Guest One"),
            album_name="Test Album",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")

    def test_accepts_spotify_only_multiple_featured_artists_when_all_are_credited(self):
        track = source_track(
            album_name="Test Album",
            track_name="Where Are You Now",
            artist_name="Main Artist",
        )
        for spotify_artists in (
            ("Main Artist", "Guest One", "Guest Two"),
            ("Main Artist", "Guest One & Guest Two"),
        ):
            with self.subTest(spotify_artists=spotify_artists):
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:test",
                    name="Where Are You Now (feat. Guest One & Guest Two)",
                    artists=spotify_artists,
                    album_name="Test Album",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "matched")
                self.assertEqual(decision.spotify_uri, "spotify:track:test")

    def test_rejects_spotify_only_featured_credit_without_required_artists(self):
        track = source_track(
            album_name="Test Album",
            track_name="Signal Path",
            artist_name="Main Artist",
        )

        for artists in (
            ("Main Artist",),
            ("Main Artist", "Different Guest"),
            ("Guest One",),
        ):
            with self.subTest(artists=artists):
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:signal-path",
                    name="Signal Path feat. Guest One",
                    artists=artists,
                    album_name="Test Album",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "unmatched")
                self.assertEqual(decision.spotify_uri, "")

    def test_rejects_partial_spotify_only_multiple_featured_artist_credit(self):
        track = source_track(
            album_name="Test Album",
            track_name="Where Are You Now",
            artist_name="Main Artist",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:partial",
            name="Where Are You Now (feat. Guest One & Guest Two)",
            artists=("Main Artist", "Guest One"),
            album_name="Test Album",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")

    def test_accepts_spotify_only_featured_credit_when_album_differs(self):
        track = source_track(
            album_name="Source Album",
            track_name="Signal Path",
            artist_name="Main Artist",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:signal-path",
            name="Signal Path feat. Guest One",
            artists=("Main Artist", "Guest One"),
            album_name="Spotify Album",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:signal-path")
        self.assertEqual(
            decision.reason,
            "track matched after removing Spotify featured credit from track title; "
            "source and featured artists matched; album differed",
        )

    def test_prefers_exact_title_over_spotify_only_featured_credit_candidate(self):
        track = source_track(
            album_name="Test Album",
            track_name="Signal Path",
            artist_name="Main Artist",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:featured",
                name="Signal Path feat. Guest One",
                artists=("Main Artist", "Guest One"),
                album_name="Test Album",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:exact",
                name="Signal Path",
                artists=("Main Artist",),
                album_name="Test Album",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:exact")
        self.assertEqual(decision.reason, "track, artist, and album matched")

    def test_marks_multiple_spotify_only_featured_credit_candidates_as_ambiguous(self):
        track = source_track(
            album_name="Test Album",
            track_name="Signal Path",
            artist_name="Main Artist",
        )
        candidates = tuple(
            SpotifyTrackCandidate(
                uri=f"spotify:track:signal-path-{index}",
                name="Signal Path feat. Guest One",
                artists=("Main Artist", "Guest One"),
                album_name="Test Album",
            )
            for index in range(2)
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(
            decision.reason,
            "2 candidates matched after removing Spotify featured credit from track title and matching album",
        )

    def test_accepts_unannotated_candidate_for_source_original_mix_variations_when_album_differs(self):
        for suffix in (
            "(Original Mix)",
            "(Original mix)",
            "(original mix)",
            "(ORIGINAL MIX)",
            "( Original   Mix )",
        ):
            with self.subTest(suffix=suffix):
                track = source_track(
                    album_name="A Thousand Faces",
                    track_name=f"Skull Shrine {suffix}",
                    artist_name="Feral",
                )
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:skull-shrine",
                    name="Skull Shrine",
                    artists=("Feral",),
                    album_name="A Thousand Faces EP",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "matched")
                self.assertEqual(decision.spotify_uri, "spotify:track:skull-shrine")
                self.assertEqual(
                    decision.reason,
                    "track matched after removing source Original Mix annotation; artist matched; album differed",
                )

    def test_prefers_original_mix_fallback_candidate_with_matching_album(self):
        track = source_track(
            album_name="A Thousand Faces",
            track_name="Skull Shrine (Original Mix)",
            artist_name="Feral",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:skull-shrine-single",
                name="Skull Shrine",
                artists=("Feral",),
                album_name="Spotify Single",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:skull-shrine-album",
                name="Skull Shrine",
                artists=("Feral",),
                album_name="A Thousand Faces",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:skull-shrine-album")
        self.assertEqual(
            decision.reason,
            "track matched after removing source Original Mix annotation; artist and album matched",
        )

    def test_prefers_exact_annotated_title_over_original_mix_fallback(self):
        track = source_track(
            album_name="A Thousand Faces",
            track_name="Skull Shrine (Original Mix)",
            artist_name="Feral",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:skull-shrine",
                name="Skull Shrine",
                artists=("Feral",),
                album_name="A Thousand Faces",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:skull-shrine-original-mix",
                name="Skull Shrine (Original Mix)",
                artists=("Feral",),
                album_name="A Thousand Faces",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:skull-shrine-original-mix")
        self.assertEqual(decision.reason, "track, artist, and album matched")

    def test_marks_multiple_original_mix_fallback_candidates_as_ambiguous(self):
        track = source_track(
            album_name="A Thousand Faces",
            track_name="Skull Shrine (Original Mix)",
            artist_name="Feral",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:skull-shrine-single",
                name="Skull Shrine",
                artists=("Feral",),
                album_name="Spotify Single",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:skull-shrine-compilation",
                name="Skull Shrine",
                artists=("Feral",),
                album_name="Spotify Compilation",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(
            decision.reason,
            "2 candidates matched after removing source Original Mix annotation and matching artist",
        )

    def test_accepts_spotify_original_annotations_for_unannotated_source_title(self):
        for candidate_title in (
            "Skull Shrine - Original",
            "Skull Shrine - Original Mix",
            "Skull Shrine (Original Mix)",
            "Skull Shrine [ORIGINAL MIX]",
            "Skull Shrine - Orginal Mix",
        ):
            with self.subTest(candidate_title=candidate_title):
                track = source_track(
                    album_name="A Thousand Faces",
                    track_name="Skull Shrine",
                    artist_name="Feral",
                )
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:skull-shrine",
                    name=candidate_title,
                    artists=("Feral",),
                    album_name="A Thousand Faces",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "matched")
                self.assertEqual(decision.spotify_uri, "spotify:track:skull-shrine")
                self.assertEqual(
                    decision.reason,
                    "track matched after removing Spotify Original annotation; artist set and album matched",
                )
                self.assertEqual(decision.match_strategy, "spotify_original_annotation")

    def test_spotify_original_annotation_requires_exact_artist_set_and_album(self):
        track = source_track(
            album_name="A Thousand Faces",
            track_name="Skull Shrine",
            artist_name="Feral",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:wrong-artist",
                name="Skull Shrine - Original Mix",
                artists=("Other Artist",),
                album_name="A Thousand Faces",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:extra-artist",
                name="Skull Shrine - Original Mix",
                artists=("Feral", "Guest Artist"),
                album_name="A Thousand Faces",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:wrong-album",
                name="Skull Shrine - Original Mix",
                artists=("Feral",),
                album_name="Different Album",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")

    def test_prefers_exact_title_over_spotify_original_annotation_fallback(self):
        track = source_track(
            album_name="A Thousand Faces",
            track_name="Skull Shrine",
            artist_name="Feral",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:annotated",
                name="Skull Shrine - Original Mix",
                artists=("Feral",),
                album_name="A Thousand Faces",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:exact",
                name="Skull Shrine",
                artists=("Feral",),
                album_name="A Thousand Faces",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.spotify_uri, "spotify:track:exact")
        self.assertEqual(decision.reason, "track, artist, and album matched")

    def test_marks_multiple_spotify_original_annotation_candidates_as_ambiguous(self):
        track = source_track(
            album_name="A Thousand Faces",
            track_name="Skull Shrine",
            artist_name="Feral",
        )
        candidates = tuple(
            SpotifyTrackCandidate(
                uri=f"spotify:track:annotated-{index}",
                name="Skull Shrine - Original Mix",
                artists=("Feral",),
                album_name="A Thousand Faces",
            )
            for index in range(2)
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(
            decision.reason,
            "2 candidates matched after removing Spotify Original annotation",
        )
        self.assertEqual(decision.match_strategy, "")

    def test_rejects_other_spotify_version_annotations_for_unannotated_source(self):
        track = source_track(
            album_name="A Thousand Faces",
            track_name="Skull Shrine",
            artist_name="Feral",
        )
        for candidate_title in (
            "Skull Shrine - Remix",
            "Skull Shrine - Original Edit",
            "Skull Shrine - Extended Mix",
        ):
            with self.subTest(candidate_title=candidate_title):
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:other-version",
                    name=candidate_title,
                    artists=("Feral",),
                    album_name="A Thousand Faces",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "unmatched")
                self.assertEqual(decision.spotify_uri, "")

    def test_rejects_unannotated_candidate_for_other_parenthetical_text(self):
        for source_title in ("Alpha One (Club Mix)", "Poison Shyness (Anti-Social)"):
            with self.subTest(source_title=source_title):
                track = source_track(
                    album_name="Alpha Album",
                    track_name=source_title,
                    artist_name="Alpha Artist",
                )
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:alpha",
                    name=source_title.rsplit(" (", 1)[0],
                    artists=("Alpha Artist",),
                    album_name="Alpha Album",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "unmatched")
                self.assertEqual(decision.spotify_uri, "")

    def test_accepts_case_only_version_label_change_as_an_exact_match(self):
        track = source_track(
            album_name="Colt EP",
            track_name="Airless (LIVE)",
            artist_name="Dense & Pika",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:airless-live",
            name="Airless (Live)",
            artists=("Dense & Pika",),
            album_name="Colt EP",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:airless-live")
        self.assertEqual(decision.reason, "track, artist, and album matched")

    def test_accepts_unique_version_substitutes_after_ordinary_matching_fails(self):
        cases = (
            (
                "First Contact",
                "First Contact - Remastered",
                ("Outline",),
                "remastered",
            ),
            (
                "Airless (Live)",
                "Airless",
                ("Dense & Pika",),
                "live",
            ),
            (
                "Feel Da Rain (D’Pac Dub) (Edit)",
                "Feel Da Rain - D'Pac Dub",
                ("Dionne", "D'Pac"),
                "edit",
            ),
        )
        for source_title, spotify_title, spotify_artists, version_label in cases:
            with self.subTest(source_title=source_title, spotify_title=spotify_title):
                source_artist = spotify_artists[0]
                track = source_track(
                    album_name="Discogs Album",
                    track_name=source_title,
                    artist_name=source_artist,
                )
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:substitute",
                    name=spotify_title,
                    artists=spotify_artists,
                    album_name="Spotify Album",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "matched")
                self.assertEqual(decision.spotify_uri, "spotify:track:substitute")
                self.assertEqual(
                    decision.reason,
                    f"track matched using the {version_label} version substitute after no ordinary title match",
                )
                self.assertEqual(decision.match_strategy, "version_substitute")

    def test_exact_title_precedes_version_substitute(self):
        track = source_track(
            album_name="Colt EP",
            track_name="Airless (Live)",
            artist_name="Dense & Pika",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:substitute",
                name="Airless",
                artists=("Dense & Pika",),
                album_name="Colt EP",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:exact",
                name="Airless (Live)",
                artists=("Dense & Pika",),
                album_name="Colt EP",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:exact")
        self.assertEqual(decision.reason, "track, artist, and album matched")

    def test_can_defer_version_substitute_until_search_ladder_finishes(self):
        track = source_track(
            album_name="First Contact",
            track_name="First Contact",
            artist_name="Outline",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:remastered",
            name="First Contact - Remastered",
            artists=("Outline",),
            album_name="First Contact (Remastered)",
        )

        decision = choose_best_track_match(
            track,
            (candidate,),
            allow_version_substitute=False,
        )

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")

    def test_marks_multiple_version_substitutes_as_ambiguous(self):
        track = source_track(
            album_name="First Contact",
            track_name="First Contact",
            artist_name="Outline",
        )
        candidates = tuple(
            SpotifyTrackCandidate(
                uri=f"spotify:track:remastered-{index}",
                name="First Contact - Remastered",
                artists=("Outline",),
                album_name=album_name,
            )
            for index, album_name in enumerate(("First Contact (Remastered)", "Blueprint25"), start=1)
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(decision.reason, "2 candidates matched as version substitutes")
        self.assertEqual(decision.review_candidates, candidates)

    def test_does_not_substitute_one_explicit_version_for_another(self):
        track = source_track(
            album_name="Alpha Album",
            track_name="Alpha One (Live)",
            artist_name="Alpha Artist",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:remastered",
            name="Alpha One - Remastered",
            artists=("Alpha Artist",),
            album_name="Alpha Album",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")

    def test_accepts_letter_number_spacing_with_matching_album_and_source_artist_subset(self):
        track = source_track(
            album_name="Chroma000",
            track_name="Chroma002 L.A.V.A",
            artist_name="B.D.B",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:chroma-002",
            name="CHROMA 002 L.A.V.A",
            artists=("B.D.B", "BICEP", "Benjamin Damage"),
            album_name="CHROMA 000",
        )

        decision = choose_best_track_match(track, (candidate,))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:chroma-002")
        self.assertEqual(
            decision.reason,
            "track and album matched after normalizing letter-number spacing",
        )
        self.assertEqual(decision.match_strategy, "alphanumeric_spacing")

    def test_letter_number_spacing_fallback_requires_matching_album_and_all_source_artists(self):
        track = source_track(
            album_name="Chroma000",
            track_name="Chroma002 L.A.V.A",
            artist_name="B.D.B, BICEP",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:wrong-album",
                name="CHROMA 002 L.A.V.A",
                artists=("B.D.B", "BICEP"),
                album_name="Different Album",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:missing-artist",
                name="CHROMA 002 L.A.V.A",
                artists=("B.D.B",),
                album_name="CHROMA 000",
            ),
        )

        for candidate in candidates:
            with self.subTest(candidate_uri=candidate.uri):
                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "unmatched")
                self.assertEqual(decision.spotify_uri, "")

    def test_marks_multiple_letter_number_spacing_candidates_as_ambiguous(self):
        track = source_track(
            album_name="Chroma000",
            track_name="Chroma002 L.A.V.A",
            artist_name="B.D.B",
        )
        candidates = tuple(
            SpotifyTrackCandidate(
                uri=f"spotify:track:chroma-002-{index}",
                name="CHROMA 002 L.A.V.A",
                artists=("B.D.B",),
                album_name="CHROMA 000",
            )
            for index in range(1, 3)
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(
            decision.reason,
            "2 candidates matched after normalizing letter-number spacing",
        )
        self.assertEqual(decision.review_candidates, candidates)

    def test_marks_multiple_matching_candidates_as_ambiguous(self):
        track = source_track(
            album_name="Alpha Album",
            track_name="Alpha One",
            artist_name="Alpha Artist",
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
        search_queries = ("first query", "second query")

        decision = choose_best_track_match(track, candidates, search_queries)

        self.assertIs(decision.track, track)
        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(decision.reason, "2 candidates matched track, artist, and album")
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.review_candidates, candidates)
        self.assertEqual(decision.search_queries, search_queries)
        self.assertEqual(decision.match_strategy, "")

    def test_accepts_tightly_constrained_title_typos(self):
        cases = (
            ("Loosing Time", "Losing Time", 1),
            ("Alpha Siganl", "Alpha Signal", 1),
            ("Signals From An Unconncet Dimension", "Signals From An Unconnected Dimension", 3),
            ("Reson", "Resin", 1),
        )
        for source_title, spotify_title, expected_distance in cases:
            with self.subTest(source_title=source_title, spotify_title=spotify_title):
                track = source_track(
                    album_name="Test Album",
                    track_name=source_title,
                    artist_name="Main Artist",
                )
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:test",
                    name=spotify_title,
                    artists=("Main Artist",),
                    album_name="Test Album",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "matched")
                self.assertEqual(decision.spotify_uri, "spotify:track:test")
                self.assertEqual(
                    decision.reason,
                    "track title matched by constrained typo fallback "
                    f"(distance {expected_distance}); artist set and album matched",
                )

    def test_constrained_typo_fallback_requires_exact_artist_set_and_album(self):
        track = source_track(
            album_name="Test Album",
            track_name="Reson",
            artist_name="Main Artist, Second Artist",
        )
        rejected_candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:missing-artist",
                name="Resin",
                artists=("Main Artist",),
                album_name="Test Album",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:extra-artist",
                name="Resin",
                artists=("Main Artist", "Second Artist", "Guest Artist"),
                album_name="Test Album",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:wrong-album",
                name="Resin",
                artists=("Second Artist", "Main Artist"),
                album_name="Other Album",
            ),
        )

        for candidate in rejected_candidates:
            with self.subTest(uri=candidate.uri):
                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "unmatched")
                self.assertEqual(decision.spotify_uri, "")

        reordered_artist_candidate = SpotifyTrackCandidate(
            uri="spotify:track:reordered-artists",
            name="Resin",
            artists=("Second Artist", "Main Artist"),
            album_name="Test Album",
        )

        decision = choose_best_track_match(track, (reordered_artist_candidate,))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:reordered-artists")

    def test_constrained_typo_fallback_rejects_broad_title_changes(self):
        cases = (
            ("Heat", "Beat"),
            ("1", "White"),
            ("Reson", "Rasin"),
            ("Alpha Signal", "Alpha Sign"),
            ("Alpha Signal", "Signal Alpha"),
            ("Alpha Signal Original Mix", "Alpha Signal Original Edit"),
            ("Alpha Signal", "Alpha Signel Extended"),
        )
        for source_title, spotify_title in cases:
            with self.subTest(source_title=source_title, spotify_title=spotify_title):
                track = source_track(
                    album_name="Test Album",
                    track_name=source_title,
                    artist_name="Main Artist",
                )
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:test",
                    name=spotify_title,
                    artists=("Main Artist",),
                    album_name="Test Album",
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "unmatched")
                self.assertEqual(decision.spotify_uri, "")

    def test_marks_multiple_constrained_typo_candidates_as_ambiguous(self):
        track = source_track(
            album_name="Test Album",
            track_name="Reson",
            artist_name="Main Artist",
        )
        candidates = (
            SpotifyTrackCandidate(
                uri="spotify:track:resin",
                name="Resin",
                artists=("Main Artist",),
                album_name="Test Album",
            ),
            SpotifyTrackCandidate(
                uri="spotify:track:reron",
                name="Reron",
                artists=("Main Artist",),
                album_name="Test Album",
            ),
        )

        decision = choose_best_track_match(track, candidates)

        self.assertEqual(decision.status, "ambiguous")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(decision.reason, "2 candidates matched by constrained typo fallback")
        self.assertEqual(decision.review_candidates, candidates)

    def test_constrained_typo_fallback_requires_nonempty_artist_and_album(self):
        cases = (
            ("", "Test Album", ("Main Artist",)),
            ("", "Test Album", ()),
            ("Main Artist", "", ("Main Artist",)),
            ("Main Artist", "Test Album", ()),
        )
        for source_artist, source_album, candidate_artists in cases:
            with self.subTest(source_artist=source_artist, source_album=source_album):
                track = source_track(
                    album_name=source_album,
                    track_name="Loosing Time",
                    artist_name=source_artist,
                )
                candidate = SpotifyTrackCandidate(
                    uri="spotify:track:losing-time",
                    name="Losing Time",
                    artists=candidate_artists,
                    album_name=source_album,
                )

                decision = choose_best_track_match(track, (candidate,))

                self.assertEqual(decision.status, "unmatched")
                self.assertEqual(decision.spotify_uri, "")

    def test_deduplicates_constrained_typo_candidates_by_spotify_uri(self):
        track = source_track(
            album_name="Test Album",
            track_name="Loosing Time",
            artist_name="Main Artist",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:losing-time",
            name="Losing Time",
            artists=("Main Artist",),
            album_name="Test Album",
        )

        decision = choose_best_track_match(track, (candidate, candidate))

        self.assertEqual(decision.status, "matched")
        self.assertEqual(decision.spotify_uri, "spotify:track:losing-time")

    def test_can_disable_constrained_typo_fallback_while_search_ladder_continues(self):
        track = source_track(
            album_name="Test Album",
            track_name="Loosing Time",
            artist_name="Main Artist",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:losing-time",
            name="Losing Time",
            artists=("Main Artist",),
            album_name="Test Album",
        )

        decision = choose_best_track_match(
            track,
            (candidate,),
            allow_constrained_typo_fallback=False,
        )

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")

    def test_rejects_candidates_that_do_not_match_track_artist_and_album(self):
        track = source_track(
            album_name="Alpha Album",
            track_name="Alpha One",
            artist_name="Alpha Artist",
        )
        candidate = SpotifyTrackCandidate(
            uri="spotify:track:wrong",
            name="Alpha Two",
            artists=("Alpha Artist",),
            album_name="Alpha Album",
        )
        search_queries = ("first query", "second query")

        decision = choose_best_track_match(track, (candidate,), search_queries)

        self.assertEqual(decision.status, "unmatched")
        self.assertEqual(decision.spotify_uri, "")
        self.assertEqual(decision.reason, "no candidates matched track, artist, and album")
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.review_candidates, (candidate,))
        self.assertEqual(decision.search_queries, search_queries)


if __name__ == "__main__":
    unittest.main()
