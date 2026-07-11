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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="36500992",
            album_name="The Poet And The Muse",
            track_number="7",
            track_name="Marcel’s Walk",
            artist_name="Mathys Lenne",
            spotify_search_query="Mathys Lenne Marcel’s Walk The Poet And The Muse",
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

    def test_builds_search_query_ladder_from_track_artist_album_and_raw_query(self):
        track = PlaylistTrack(
            playlist_name="Discogs - Breakbeat",
            release_id="111",
            album_name="Alpha EP",
            track_number="1",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="25308169",
            album_name="False Hope",
            track_number="7",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="37196016",
            album_name="A Thousand Faces",
            track_number="1",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="37672899",
            album_name="Carl Gari",
            track_number="2",
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
                track = PlaylistTrack(
                    playlist_name="Discogs - Test",
                    release_id="111",
                    album_name="Test Album",
                    track_number="1",
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
                track = PlaylistTrack(
                    playlist_name="Discogs - Deep Techno",
                    release_id="37672899",
                    album_name="Carl Gari",
                    track_number="2",
                    track_name=track_name,
                    artist_name="Carl Gari",
                    spotify_search_query=f"Carl Gari {track_name} Carl Gari",
                )

                queries = build_spotify_track_search_queries(track)

                self.assertNotIn('track:"Swim" artist:"Carl Gari"', queries)

    def test_search_query_ladder_does_not_strip_other_parenthetical_text(self):
        track = PlaylistTrack(
            playlist_name="Discogs - Breakbeat",
            release_id="111",
            album_name="Alpha Album",
            track_number="1",
            track_name="Alpha One (Club Mix)",
            artist_name="Alpha Artist",
            spotify_search_query="Alpha Artist Alpha One (Club Mix) Alpha Album",
        )

        queries = build_spotify_track_search_queries(track)

        self.assertNotIn('track:"Alpha One" artist:"Alpha Artist"', queries)
        self.assertNotIn("Alpha Artist Alpha One", queries)

    def test_search_query_ladder_strips_original_mix_case_and_spacing_variations(self):
        for suffix in (
            "(Original mix)",
            "(original mix)",
            "(ORIGINAL MIX)",
            "( Original   Mix )",
        ):
            with self.subTest(suffix=suffix):
                track = PlaylistTrack(
                    playlist_name="Discogs - Deep Techno",
                    release_id="37196016",
                    album_name="A Thousand Faces",
                    track_number="1",
                    track_name=f"Skull Shrine {suffix}",
                    artist_name="Feral",
                    spotify_search_query=f"Feral Skull Shrine {suffix} A Thousand Faces",
                )

                queries = build_spotify_track_search_queries(track)

                self.assertIn('track:"Skull Shrine" artist:"Feral"', queries)
                self.assertIn("Feral Skull Shrine", queries)

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

    def test_accepts_spotify_title_with_leading_in_when_artist_and_album_match(self):
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="29905411",
            album_name="Environment Control",
            track_number="3",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
            spotify_search_query="Polar Inertia Dark Territory Environment Control",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="29905411",
            album_name="Environment Control",
            track_number="3",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
            spotify_search_query="Polar Inertia Dark Territory Environment Control",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="29905411",
            album_name="",
            track_number="3",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
            spotify_search_query="Polar Inertia Dark Territory",
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
                track = PlaylistTrack(
                    playlist_name="Discogs - Deep Techno",
                    release_id="29905411",
                    album_name="Environment Control",
                    track_number="3",
                    track_name=source_title,
                    artist_name="Polar Inertia",
                    spotify_search_query=f"Polar Inertia {source_title} Environment Control",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="29905411",
            album_name="Environment Control",
            track_number="3",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
            spotify_search_query="Polar Inertia Dark Territory Environment Control",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="29905411",
            album_name="Environment Control",
            track_number="3",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
            spotify_search_query="Polar Inertia Dark Territory Environment Control",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="29905411",
            album_name="Environment Control",
            track_number="3",
            track_name="Dark Territory",
            artist_name="Polar Inertia",
            spotify_search_query="Polar Inertia Dark Territory Environment Control",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Breakbeat",
            release_id="111",
            album_name="Discogs EP",
            track_number="1",
            track_name="Alpha One",
            artist_name="Alpha Artist",
            spotify_search_query="Alpha Artist Alpha One Discogs EP",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Breakbeat",
            release_id="111",
            album_name="Discogs EP",
            track_number="1",
            track_name="Alpha One",
            artist_name="Alpha Artist",
            spotify_search_query="Alpha Artist Alpha One Discogs EP",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Breakbeat",
            release_id="111",
            album_name="Alpha Album",
            track_number="1",
            track_name="Alpha One",
            artist_name="Alpha Artist",
            spotify_search_query="Alpha Artist Alpha One Alpha Album",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="37672899",
            album_name="Carl Gari",
            track_number="2",
            track_name="Swim feat. Polygonia",
            artist_name="Carl Gari",
            spotify_search_query="Carl Gari Swim feat. Polygonia Carl Gari",
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
                track = PlaylistTrack(
                    playlist_name="Discogs - Deep Techno",
                    release_id="37672899",
                    album_name="Carl Gari",
                    track_number="2",
                    track_name=track_name,
                    artist_name="Carl Gari",
                    spotify_search_query=f"Carl Gari {track_name} Carl Gari",
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
                track = PlaylistTrack(
                    playlist_name="Discogs - Test",
                    release_id="111",
                    album_name="Test Album",
                    track_number="1",
                    track_name=track_name,
                    artist_name=source_artists,
                    spotify_search_query=f"{source_artists} {track_name} Test Album",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Test",
            release_id="111",
            album_name="Test Album",
            track_number="1",
            track_name="Where Are You Now (feat. Guest Six & Guest Seven)",
            artist_name="Guest Six, Guest Seven, Main Artist",
            spotify_search_query=(
                "Guest Six, Guest Seven, Main Artist Where Are You Now "
                "(feat. Guest Six & Guest Seven) Test Album"
            ),
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
        track = PlaylistTrack(
            playlist_name="Discogs - Test",
            release_id="111",
            album_name="Test Album",
            track_number="1",
            track_name="The Reason (feat. Guest Eight)",
            artist_name="Guest Eight, Main Artist",
            spotify_search_query=(
                "Guest Eight, Main Artist The Reason "
                "(feat. Guest Eight) Test Album"
            ),
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
        track = PlaylistTrack(
            playlist_name="Discogs - Test",
            release_id="111",
            album_name="Test Album",
            track_number="1",
            track_name="The Reason (feat. Guest Eight)",
            artist_name="Guest Eight",
            spotify_search_query=(
                "Guest Eight The Reason (feat. Guest Eight) Test Album"
            ),
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="37672899",
            album_name="Carl Gari",
            track_number="2",
            track_name="Swim feat. Polygonia",
            artist_name="Carl Gari",
            spotify_search_query="Carl Gari Swim feat. Polygonia Carl Gari",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="37672899",
            album_name="Carl Gari",
            track_number="2",
            track_name="Swim feat. Polygonia",
            artist_name="Carl Gari",
            spotify_search_query="Carl Gari Swim feat. Polygonia Carl Gari",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="37672899",
            album_name="Carl Gari",
            track_number="2",
            track_name="Swim feat. Polygonia",
            artist_name="Carl Gari",
            spotify_search_query="Carl Gari Swim feat. Polygonia Carl Gari",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="37672899",
            album_name="Carl Gari",
            track_number="2",
            track_name="Swim feat. Polygonia",
            artist_name="Carl Gari",
            spotify_search_query="Carl Gari Swim feat. Polygonia Carl Gari",
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

    def test_accepts_unannotated_candidate_for_source_original_mix_variations_when_album_differs(self):
        for suffix in (
            "(Original Mix)",
            "(Original mix)",
            "(original mix)",
            "(ORIGINAL MIX)",
            "( Original   Mix )",
        ):
            with self.subTest(suffix=suffix):
                track = PlaylistTrack(
                    playlist_name="Discogs - Deep Techno",
                    release_id="37196016",
                    album_name="A Thousand Faces",
                    track_number="1",
                    track_name=f"Skull Shrine {suffix}",
                    artist_name="Feral",
                    spotify_search_query=f"Feral Skull Shrine {suffix} A Thousand Faces",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="37196016",
            album_name="A Thousand Faces",
            track_number="1",
            track_name="Skull Shrine (Original Mix)",
            artist_name="Feral",
            spotify_search_query="Feral Skull Shrine (Original Mix) A Thousand Faces",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="37196016",
            album_name="A Thousand Faces",
            track_number="1",
            track_name="Skull Shrine (Original Mix)",
            artist_name="Feral",
            spotify_search_query="Feral Skull Shrine (Original Mix) A Thousand Faces",
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
        track = PlaylistTrack(
            playlist_name="Discogs - Deep Techno",
            release_id="37196016",
            album_name="A Thousand Faces",
            track_number="1",
            track_name="Skull Shrine (Original Mix)",
            artist_name="Feral",
            spotify_search_query="Feral Skull Shrine (Original Mix) A Thousand Faces",
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

    def test_rejects_unannotated_candidate_for_other_parenthetical_text(self):
        for source_title in ("Alpha One (Club Mix)", "Alpha One (Live)", "Poison Shyness (Anti-Social)"):
            with self.subTest(source_title=source_title):
                track = PlaylistTrack(
                    playlist_name="Discogs - Breakbeat",
                    release_id="111",
                    album_name="Alpha Album",
                    track_number="1",
                    track_name=source_title,
                    artist_name="Alpha Artist",
                    spotify_search_query=f"Alpha Artist {source_title} Alpha Album",
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
