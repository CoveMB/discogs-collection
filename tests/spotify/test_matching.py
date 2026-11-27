import sys
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.matching import (
    AMBIGUOUS,
    ERROR,
    MATCHED,
    UNMATCHED,
    PlaylistTrack,
    SpotifyTrackCandidate,
    TrackMatchDecision,
    build_base_title_version_review_query,
    build_spotify_track_search_queries,
    build_spotify_track_search_query,
    choose_best_track_match,
    normalize_music_text,
    resolve_spotify_edition_tie,
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
    def test_other_aligned_edition_cannot_disappear_when_the_uri_has_conflicting_metadata(self):
        track = source_track(track_name="Rivière", artist_name="North", album_name="Evening")
        first = SpotifyTrackCandidate(
            "spotify:track:first", "Riviere", ("North",), "Evening", "edition-a", "USABC2400001", True,
        )
        second = replace(first, uri="spotify:track:second", album_id="edition-b")
        contradicted = replace(second, name="Another Song", isrc="USABC2400002")
        result = choose_best_track_match(track, (first, contradicted), aligned_album_positions=(first, second))
        self.assertEqual((result.status, result.spotify_uri), (AMBIGUOUS, ""))

    def test_composer_prefix_requires_full_label_credits_album_and_unique_playable_candidate(self):
        track = source_track(track_name="Op. 12 / II. Allegro", artist_name="City Players", album_name="Works (Volume I)")
        candidate = SpotifyTrackCandidate(
            "spotify:track:work", "A. Reed: Op. 12/II. Allegro", ("Ada Reed", "City Players"),
            "Works / Volume I", "edition", "USABC2400001", True,
        )
        self.assertEqual(choose_best_track_match(track, (candidate,)).spotify_uri, candidate.uri)
        reversed_label = replace(candidate, name="Reed, A: Op. 12/II. Allegro")
        self.assertEqual(choose_best_track_match(track, (reversed_label,)).spotify_uri, candidate.uri)
        for other in (
            replace(candidate, name="T. Reed: Op. 12/II. Allegro"),
            replace(candidate, name="Reed, T: Op. 12/II. Allegro"),
            replace(candidate, name="Reed: Op. 12/III. Allegro"),
            replace(candidate, name="Reed: Op. 12/II. Allegro (Arranged)"),
            replace(candidate, artists=("Ada Reed",)),
            replace(candidate, album_name="Compilation"),
            replace(candidate, is_playable=None),
        ):
            with self.subTest(other=other):
                self.assertNotEqual(choose_best_track_match(track, (other,)).status, MATCHED)
        rival = replace(candidate, uri="spotify:track:rival", name="Unknown: Op. 12/II. Allegro")
        self.assertEqual(choose_best_track_match(track, (candidate, rival)).status, AMBIGUOUS)

    def test_featuring_requires_all_separately_credited_performers(self):
        track = source_track(track_name="Evening", artist_name="North, Sol", album_name="Source")
        copy = SpotifyTrackCandidate(
            "spotify:track:copy", "Evening featuring Sol", ("Sol", "North"), "Other", "edition", is_playable=True,
        )
        self.assertEqual(choose_best_track_match(track, (copy,)).spotify_uri, copy.uri)
        for other in (replace(copy, artists=("North",)), replace(copy, name="Evening featuring Sol featuring Sol")):
            self.assertNotEqual(choose_best_track_match(track, (other,)).status, MATCHED)

    def test_full_title_conjunction_with_exact_credits_and_playability(self):
        track = source_track(track_name="Night and Day", artist_name="North", album_name="Source")
        copy = SpotifyTrackCandidate(
            "spotify:track:copy", "Night & Day", ("North",), "Other", "edition", is_playable=True,
        )
        self.assertEqual(choose_best_track_match(track, (copy,)).spotify_uri, copy.uri)
        for other in (replace(copy, artists=("North", "South")), replace(copy, name="Night&Day"),
                      replace(copy, name="Night & Day (Live)"), replace(copy, is_playable=False)):
            self.assertNotEqual(choose_best_track_match(track, (other,)).status, MATCHED)

    def test_spelling_candidates_must_be_explainable_before_requiring_source_proof(self):
        for left, right, candidate in (
            ("Mavi Işık", "Mavi İşik", True), ("Crème", "Creme", True), ("Άνοιξη", "Ανοιξη", True),
            ("Алёна", "Алена", True), ("عَلَم", "علم", True), ("שָׁלוֹם", "שׁלום", True),
            ("año 東京", "ano 東京", True), ("عَلَم", "عِلْم", False), ("أمل", "امل", False),
            ("שׁם", "שׂם", False), ("año 東京", "ano 京都", False), ("が", "か", False),
            ("Côte", "Côté", True), ("Côte", "Cóte", False), ("Mira Α", "Mira А", False),
        ):
            with self.subTest(left=left, right=right):
                track = source_track(track_name=left, artist_name="North", album_name="Evening")
                hit = SpotifyTrackCandidate(
                    "spotify:track:copy", right, ("North",), "Evening", "edition", is_playable=True,
                )
                result = choose_best_track_match(track, (hit,))
                self.assertEqual(result.status, AMBIGUOUS if candidate else UNMATCHED)

    def test_exact_matching_preserves_letters_and_marks_in_all_scripts(self):
        artist = "Example Artist"
        for source, spotify, matches in (
            ("東京", "京都", False),
            ("Signal 東", "Signal 西", False),
            ("Север", "север", True),
            ("Κύμα", "Κυ\u0301μα", True),
            ("か", "が", False),
            ("Café", "Cafe", False),
            ("Maße", "Masse", False),
            ("♫", "★", False),
        ):
            with self.subTest(source=source, spotify=spotify):
                track = source_track(track_name=source, artist_name=artist, album_name="Evening")
                candidate = SpotifyTrackCandidate("spotify:track:copy", spotify, (artist,), "Evening")
                self.assertEqual(choose_best_track_match(track, (candidate,)).status == MATCHED, matches)

    def test_repeated_performer_credit_requires_consistent_playable_copies(self):
        track = source_track(track_name="Rivière (Live)", artist_name="North, Sol", album_name="Evening")
        preferred = SpotifyTrackCandidate(
            "spotify:track:one", "Rivière (Live) (with Sol)", ("Sol", "North"),
            "Evening (with Sol)", "album-one", "USABC2400001", True,
        )
        other = replace(preferred, uri="spotify:track:two", album_name="Sampler", album_id="album-two")
        for candidates, winner in (
            ((replace(other, isrc=""),), other.uri),
            ((preferred, other), preferred.uri), ((other, preferred, other), preferred.uri),
        ):
            with self.subTest(candidates=candidates):
                decision = choose_best_track_match(track, candidates)
                self.assertEqual((decision.status, decision.spotify_uri), (MATCHED, winner))
                self.assertEqual(decision.match_strategy, "repeated_performer_credit")
                self.assertIn("(with Sol)", decision.reason)
        for rival in (
            replace(other, isrc="USABC2400002"), replace(other, isrc=""), replace(other, is_playable=None),
            replace(other, album_id=preferred.album_id), replace(other, album_name="Evening"),
            replace(other, album_id=""), replace(preferred, name=""), replace(preferred, is_playable=False),
            replace(preferred, has_complete_recording_metadata=False), replace(preferred, name="Other"),
        ):
            with self.subTest(rival=rival):
                self.assertEqual(choose_best_track_match(track, (preferred, rival)).status, AMBIGUOUS)
        self.assertEqual(choose_best_track_match(replace(track, album_name="Third"), (preferred, other)).status, AMBIGUOUS)

    def test_repeated_performer_credit_preserves_work_version_and_artist_identity(self):
        track = source_track(track_name="Café", artist_name="North, Sol", album_name="Source")
        candidate = SpotifyTrackCandidate("spotify:track:one", "Cafe\u0301 ( WITH  SOL )", ("Sol", "North"), "Other", "id", "", True)
        self.assertEqual(choose_best_track_match(track, (candidate,)).status, MATCHED)
        self.assertEqual(choose_best_track_match(track, (replace(candidate, name="Café(with Sol)"),)).status, MATCHED)
        self.assertEqual(choose_best_track_match(track, (replace(candidate, version_title_review_only=True),)).status, UNMATCHED)
        for title in ("Cafe (with Sol)", "Café Live (with Sol)", "Café (Mixed) (with Sol)", "Café with Sol",
                      "Café (with (Sol))", "Café (with Sol", "Café (with S)", "Café (with Sol) (with Sol)"):
            with self.subTest(title=title):
                self.assertEqual(choose_best_track_match(track, (replace(candidate, name=title),)).status, UNMATCHED)
        for exported, artists in (
            ("North, Sol", ("North, Sol",)), ("North, Sol", ("North",)),
            ("North, Sol", ("North", "Sol", "Guest")), ("North, Sol, Sol", ("North", "Sol", "Sol")),
            ("North, Wind & Fire, Sol", ("North", "Wind & Fire", "Sol")), ("North,,Sol", ("North", "Sol")),
        ):
            with self.subTest(exported=exported, artists=artists):
                decision = choose_best_track_match(replace(track, artist_name=exported), (replace(candidate, artists=artists),))
                self.assertEqual(decision.status, UNMATCHED)

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

    def test_base_title_review_query_requires_version_and_candidate_evidence(self):
        track = source_track(track_name="Signal (2019 Remaster)", artist_name="Harbour Lines", album_name="Signals")
        candidate = SpotifyTrackCandidate("spotify:track:other", "Signal - 2020 Remaster", ("Harbour Lines",), "Other")
        decision = TrackMatchDecision(track, UNMATCHED, "", "ordinary miss", review_candidates=(candidate,))
        expected_query = 'track:"Signal" artist:"Harbour Lines"'
        self.assertEqual(build_base_title_version_review_query(decision), expected_query)

        for version_title in (
            "Signal [Demo]", "Signal – Acoustic Version", "Signal — Vocal Rework",
            "Signal (Remaster (2019))", "Signal [Rework (Club)]",
        ):
            with self.subTest(version_title=version_title):
                self.assertEqual(
                    build_base_title_version_review_query(replace(decision, track=replace(track, track_name=version_title))),
                    expected_query,
                )
        for ineligible_title in (
            "Signal (Part Two)", "Signal [Deliverance]", "Signal (2019 Remaster]", "Signal-Remaster",
            "Signal - 2019 Remaster - Part Two", "(2019 Remaster)",
        ):
            with self.subTest(ineligible_title=ineligible_title):
                self.assertIsNone(
                    build_base_title_version_review_query(replace(decision, track=replace(track, track_name=ineligible_title)))
                )
        self.assertEqual(
            build_base_title_version_review_query(replace(
                decision,
                track=replace(track, track_name="Signal - Part Two - 2019 Remaster"),
                review_candidates=(replace(candidate, name="Signal - Part Two"),),
            )),
            'track:"Signal - Part Two" artist:"Harbour Lines"',
        )
        self.assertIsNone(build_base_title_version_review_query(replace(decision, track=replace(track, artist_name=" "))))
        self.assertIsNone(build_base_title_version_review_query(replace(decision, review_candidates=())))
        for extra_artist in ("Guest", "星"):
            with self.subTest(extra_artist=extra_artist):
                self.assertIsNone(build_base_title_version_review_query(replace(
                    decision, review_candidates=(replace(candidate, artists=("Harbour Lines", extra_artist)),),
                )))
        self.assertIsNone(build_base_title_version_review_query(replace(
            decision,
            track=replace(track, track_name="Signal 東 (2019 Remaster)"),
            review_candidates=(replace(candidate, name="Signal 西 - 2020 Remaster"),),
        )))
        self.assertIsNone(build_base_title_version_review_query(replace(
            decision,
            track=replace(track, track_name="Signal! (2019 Remaster)"),
            review_candidates=(replace(candidate, name="Signal? - 2020 Remaster"),),
        )))
        self.assertEqual(build_base_title_version_review_query(replace(
            decision,
            track=replace(track, track_name="Café Signal (2019 Remaster)", artist_name="Harbour Lines, 星"),
            review_candidates=(replace(candidate, name="CAFE\u0301   Signal", artists=("星", "harbour  lines")),),
        )), 'track:"Café Signal" artist:"Harbour Lines, 星"')
        self.assertEqual(build_base_title_version_review_query(replace(
            decision,
            track=replace(track, artist_name="Earth, Wind\t&\tFire"),
            review_candidates=(replace(candidate, artists=("Earth, Wind & Fire",)),),
        )), 'track:"Signal" artist:"Earth, Wind & Fire"')
        self.assertIsNone(build_base_title_version_review_query(replace(
            decision, search_queries=('  track:"Signal"\tartist:"Harbour   Lines" ',),
        )))
        for status in (MATCHED, AMBIGUOUS, ERROR):
            with self.subTest(status=status):
                self.assertIsNone(build_base_title_version_review_query(replace(decision, status=status)))

        for query_length in (250, 251):
            base_title = "東" * (query_length - len('track:"" artist:"Harbour Lines"'))
            query = f'track:"{base_title}" artist:"Harbour Lines"'
            with self.subTest(query_length=query_length):
                self.assertEqual(build_base_title_version_review_query(replace(
                    decision,
                    track=replace(track, track_name=f"{base_title} (2019 Remaster)"),
                    review_candidates=(replace(candidate, name=base_title),),
                )), query if query_length == 250 else None)

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

    def test_same_recording_edition_tie_selects_playable_copy_stably(self):
        track = source_track(
            track_name="Áfter Light 現場", artist_name="Harbour Lines, Nami", album_name="Tidal Signals",
        )
        first = SpotifyTrackCandidate(
            uri="spotify:track:alpha",
            name="A\u0301fter  Light 現場",
            artists=("Nami", "Harbour Lines"),
            album_name="Tidal Signals",
            album_id="edition-a",
            isrc="USAAA2600001",
        )
        second = replace(
            first, uri="spotify:track:zulu", album_id="edition-b",
            name="áfter light 現場", artists=("harbour lines", "nami"),
        )
        search_queries = ('track:"Áfter Light 現場"',)
        cases = (
            ("known playable", (first, replace(second, is_playable=True)), second.uri, True),
            ("known unavailable", (replace(first, is_playable=False), second), second.uri, None),
            ("unknowns", (first, second), first.uri, None),
            ("reversed unknowns", (second, first), first.uri, None),
            ("later unavailable", (first, second, replace(first, is_playable=False)), second.uri, None),
            ("later playable", (first, second, replace(second, is_playable=True)), second.uri, True),
        )
        for label, observations, expected_uri, expected_playable in cases:
            with self.subTest(label=label):
                ambiguity = choose_best_track_match(track, observations, search_queries)
                resolved = resolve_spotify_edition_tie(ambiguity, observations)

                self.assertEqual(ambiguity.status, "ambiguous")
                self.assertEqual(resolved.status, "matched")
                self.assertEqual(resolved.match_strategy, "same_recording_edition")
                self.assertEqual(resolved.spotify_uri, expected_uri)
                self.assertIsNotNone(resolved.candidate)
                assert resolved.candidate is not None
                self.assertIs(resolved.candidate.is_playable, expected_playable)
                self.assertEqual(resolved.search_queries, search_queries)
                self.assertEqual(
                    [candidate.uri for candidate in resolved.review_candidates],
                    [candidate.uri for candidate in ambiguity.review_candidates],
                )

    def test_same_recording_edition_tie_retains_conflicting_evidence(self):
        track = source_track(
            track_name="After Light (Live) 夜", artist_name="Harbour Lines, Nami",
            album_name="Tidal Signals 海",
        )
        first = SpotifyTrackCandidate(
            uri="spotify:track:alpha",
            name=track.track_name,
            artists=("Harbour Lines", "Nami"),
            album_name=track.album_name,
            album_id="edition-a",
            isrc="USAAA2600001",
        )
        second = replace(first, uri="spotify:track:zulu", album_id="edition-b")
        cases = (
            ("different ISRC", (first, replace(second, isrc="USAAA2600002"))),
            ("missing ISRC", (first, replace(second, isrc=""))),
            ("lookalike ISRC", (first, replace(second, isrc="UſAAA2600001"))),
            ("different album tier", (
                replace(first, album_name="Another Album"), replace(second, album_name="Another Album"),
            )),
            ("missing album ID", (first, replace(second, album_id=""))),
            ("shared album ID", (first, replace(second, album_id="edition-a"))),
            ("partial artist credit", (first, replace(second, artists=("Harbour Lines",)))),
            ("changed version punctuation", (first, replace(second, name="After Light [Live] 夜"))),
            ("changed Unicode title for the same URI", (first, second, replace(second, name="After Light (Live) 日"))),
            ("changed Unicode album for the same URI", (first, second, replace(second, album_name="Tidal Signals 山"))),
            ("all unavailable", (replace(first, is_playable=False), replace(second, is_playable=False))),
            ("changed observed ID", (first, second, replace(first, isrc="USAAA2600002"))),
            ("changed observed album", (first, second, replace(first, album_id="edition-c"))),
            ("changed observed credit", (first, second, replace(first, artists=("Harbour Lines",)))),
            ("conflicting playability", (
                replace(first, is_playable=True), second, replace(first, is_playable=False),
            )),
            ("episode URI", (replace(first, uri="spotify:episode:alpha"), second)),
            ("empty track URI suffix", (replace(first, uri="spotify:track:"), second)),
        )
        for label, observations in cases:
            with self.subTest(label=label):
                ambiguity = choose_best_track_match(track, observations)

                self.assertEqual(ambiguity.status, "ambiguous")
                self.assertIs(resolve_spotify_edition_tie(ambiguity, observations), ambiguity)

        ordinary = choose_best_track_match(track, (first,))
        self.assertEqual(ordinary.status, "matched")
        self.assertIs(resolve_spotify_edition_tie(ordinary, (first, second)), ordinary)

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
