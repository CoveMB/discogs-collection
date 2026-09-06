import sys
import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.client import (
    HttpResponse,
    SpotifyAlbumCandidate,
    SpotifyAlbumTrack,
    SpotifyPlaylist,
    SpotifyPlaylistItem,
    SpotifyApiError,
    SpotifyClient,
    SpotifyRateLimitDeferredError,
    SpotifyRateLimitRetriesExhaustedError,
    SpotifyRetryPolicy,
    SpotifyTrackCandidate,
)


class SpotifyClientTests(unittest.TestCase):
    def test_search_tracks_sends_structured_query_as_spotify_q_parameter(self):
        captured_requests = []

        def transport(request):
            captured_requests.append(request)
            return HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"tracks":{"items":[{"uri":"spotify:track:alpha","name":"Alpha One",'
                    '"artists":[{"name":"Alpha Artist"}],"album":{"name":"Alpha Album"}}]}}'
                ),
            )

        client = SpotifyClient(transport=transport)

        candidates = client.search_tracks(
            access_token="access-token",
            query='track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
            limit=10,
        )

        self.assertEqual(
            captured_requests[0].url,
            (
                "https://api.spotify.com/v1/search?"
                "q=track%3A%22Alpha+One%22+artist%3A%22Alpha+Artist%22+album%3A%22Alpha+Album%22"
                "&type=track&limit=10"
            ),
        )
        self.assertEqual(captured_requests[0].headers["Authorization"], "Bearer access-token")
        self.assertEqual(
            candidates,
            (
                SpotifyTrackCandidate(
                    uri="spotify:track:alpha",
                    name="Alpha One",
                    artists=("Alpha Artist",),
                    album_name="Alpha Album",
                ),
            ),
        )

    def test_search_albums_sends_album_query_and_parses_candidates(self):
        captured_requests = []

        def transport(request):
            captured_requests.append(request)
            return HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"albums":{"items":[{"id":"album-1","uri":"spotify:album:album-1",'
                    '"name":"Numbers and Colours","artists":[{"name":"Example Artist"}],'
                    '"total_tracks":6}]}}'
                ),
            )

        client = SpotifyClient(transport=transport)

        candidates = client.search_albums(
            access_token="access-token",
            query='album:"Numbers and Colours"',
            limit=10,
        )

        self.assertEqual(
            captured_requests[0].url,
            (
                "https://api.spotify.com/v1/search?"
                "q=album%3A%22Numbers+and+Colours%22&type=album&limit=10"
            ),
        )
        self.assertEqual(
            candidates,
            (
                SpotifyAlbumCandidate(
                    album_id="album-1",
                    uri="spotify:album:album-1",
                    name="Numbers and Colours",
                    artists=("Example Artist",),
                    total_tracks=6,
                ),
            ),
        )

    def test_get_album_tracks_reads_order_and_playability_across_pages(self):
        captured_requests = []
        responses = [
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"items":[{"uri":"spotify:track:one","name":"White",'
                    '"artists":[{"name":"Example Artist"}],"disc_number":1,'
                    '"track_number":1,"is_playable":true}],"next":"next-page"}'
                ),
            ),
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"items":[{"uri":"spotify:track:two","name":"Grey",'
                    '"artists":[{"name":"Example Artist"}],"disc_number":1,'
                    '"track_number":2,"is_playable":false}],"next":null}'
                ),
            ),
        ]

        def transport(request):
            captured_requests.append(request)
            return responses.pop(0)

        client = SpotifyClient(transport=transport)

        tracks = client.get_album_tracks(
            access_token="access-token",
            album_id="album/one",
        )

        self.assertEqual(
            tracks,
            (
                SpotifyAlbumTrack(
                    uri="spotify:track:one",
                    name="White",
                    artists=("Example Artist",),
                    disc_number=1,
                    track_number=1,
                    is_playable=True,
                ),
                SpotifyAlbumTrack(
                    uri="spotify:track:two",
                    name="Grey",
                    artists=("Example Artist",),
                    disc_number=1,
                    track_number=2,
                    is_playable=False,
                ),
            ),
        )
        self.assertIn("/albums/album%2Fone/tracks?", captured_requests[0].url)
        self.assertIn("offset=0", captured_requests[0].url)
        self.assertIn("offset=50", captured_requests[1].url)

    def test_lists_current_user_playlists_across_pages(self):
        captured_requests = []
        responses = [
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"items":[{"id":"playlist-1","name":"Discogs - House",'
                    '"external_urls":{"spotify":"https://open.spotify.com/playlist/playlist-1"}}],'
                    '"next":"next-page"}'
                ),
            ),
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"items":[{"id":"playlist-2","name":"Discogs - Techno",'
                    '"external_urls":{"spotify":"https://open.spotify.com/playlist/playlist-2"}}],'
                    '"next":null}'
                ),
            ),
        ]

        def transport(request):
            captured_requests.append(request)
            return responses.pop(0)

        client = SpotifyClient(transport=transport)

        playlists = client.list_current_user_playlists(access_token="access-token")

        self.assertEqual(
            playlists,
            (
                SpotifyPlaylist(
                    playlist_id="playlist-1",
                    name="Discogs - House",
                    url="https://open.spotify.com/playlist/playlist-1",
                ),
                SpotifyPlaylist(
                    playlist_id="playlist-2",
                    name="Discogs - Techno",
                    url="https://open.spotify.com/playlist/playlist-2",
                ),
            ),
        )
        self.assertEqual(captured_requests[0].method, "GET")
        self.assertIn("https://api.spotify.com/v1/me/playlists?", captured_requests[0].url)
        self.assertIn("offset=0", captured_requests[0].url)
        self.assertIn("offset=50", captured_requests[1].url)

    def test_reads_current_user_id_and_playlist_ownership_metadata(self):
        captured_requests = []
        responses = [
            HttpResponse(status=200, headers={}, body='{"id":"current-user"}'),
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"items":[{"id":"playlist-1","name":"Discogs - House",'
                    '"owner":{"id":"current-user"},"public":false,"collaborative":false,'
                    '"external_urls":{"spotify":"https://open.spotify.com/playlist/playlist-1"}},'
                    '{"id":"playlist-2","name":"Discogs - House",'
                    '"owner":{"id":"other-user"},"public":true,"collaborative":true,'
                    '"external_urls":{"spotify":"https://open.spotify.com/playlist/playlist-2"}}],'
                    '"next":null}'
                ),
            ),
        ]

        def transport(request):
            captured_requests.append(request)
            return responses.pop(0)

        client = SpotifyClient(transport=transport)

        current_user_id = client.get_current_user_id(access_token="access-token")
        playlists = client.list_current_user_playlists(access_token="access-token")

        self.assertEqual(current_user_id, "current-user")
        self.assertEqual(captured_requests[0].url, "https://api.spotify.com/v1/me")
        self.assertEqual(
            playlists,
            (
                SpotifyPlaylist(
                    playlist_id="playlist-1",
                    name="Discogs - House",
                    url="https://open.spotify.com/playlist/playlist-1",
                    owner_id="current-user",
                    public=False,
                    collaborative=False,
                ),
                SpotifyPlaylist(
                    playlist_id="playlist-2",
                    name="Discogs - House",
                    url="https://open.spotify.com/playlist/playlist-2",
                    owner_id="other-user",
                    public=True,
                    collaborative=True,
                ),
            ),
        )

    def test_get_playlist_items_reads_tracks_across_pages(self):
        captured_requests = []
        responses = [
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"items":[{"track":{"uri":"spotify:track:alpha","name":"Alpha One",'
                    '"artists":[{"name":"Alpha Artist"}],"album":{"name":"Alpha Album"}}}],'
                    '"next":"next-page"}'
                ),
            ),
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"items":[{"track":{"uri":"spotify:track:beta","name":"Beta One",'
                    '"artists":[{"name":"Beta Artist"}],"album":{"name":"Beta Album"}}}],'
                    '"next":null}'
                ),
            ),
        ]

        def transport(request):
            captured_requests.append(request)
            return responses.pop(0)

        client = SpotifyClient(transport=transport)

        items = client.get_playlist_items(access_token="access-token", playlist_id="playlist-1")

        self.assertEqual(
            items,
            (
                SpotifyPlaylistItem(
                    uri="spotify:track:alpha",
                    name="Alpha One",
                    artists=("Alpha Artist",),
                    album_name="Alpha Album",
                ),
                SpotifyPlaylistItem(
                    uri="spotify:track:beta",
                    name="Beta One",
                    artists=("Beta Artist",),
                    album_name="Beta Album",
                    position=50,
                ),
            ),
        )
        self.assertEqual(captured_requests[0].method, "GET")
        self.assertIn("/playlists/playlist-1/items?", captured_requests[0].url)
        self.assertIn("offset=0", captured_requests[0].url)
        self.assertIn("offset=50", captured_requests[1].url)

    def test_get_playlist_items_reads_added_at_and_zero_based_positions(self):
        captured_requests = []
        responses = [
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"items":[{"added_at":"2026-01-01T00:00:00Z",'
                    '"track":{"uri":"spotify:track:alpha","name":"Alpha One",'
                    '"artists":[{"name":"Alpha Artist"}],"album":{"name":"Alpha Album"}}}],'
                    '"next":"next-page"}'
                ),
            ),
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"items":[{"added_at":"2026-01-02T00:00:00Z",'
                    '"track":{"uri":"spotify:track:beta","name":"Beta One",'
                    '"artists":[{"name":"Beta Artist"}],"album":{"name":"Beta Album"}}}],'
                    '"next":null}'
                ),
            ),
        ]

        def transport(request):
            captured_requests.append(request)
            return responses.pop(0)

        client = SpotifyClient(transport=transport)

        items = client.get_playlist_items(access_token="access-token", playlist_id="playlist-1")

        self.assertEqual(
            items,
            (
                SpotifyPlaylistItem(
                    uri="spotify:track:alpha",
                    name="Alpha One",
                    artists=("Alpha Artist",),
                    album_name="Alpha Album",
                    added_at="2026-01-01T00:00:00Z",
                    position=0,
                ),
                SpotifyPlaylistItem(
                    uri="spotify:track:beta",
                    name="Beta One",
                    artists=("Beta Artist",),
                    album_name="Beta Album",
                    added_at="2026-01-02T00:00:00Z",
                    position=50,
                ),
            ),
        )
        self.assertIn("added_at", captured_requests[0].url)

    def test_get_playlist_items_reads_current_item_field(self):
        captured_requests = []
        responses = [
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"items":[{"item":{"type":"track","uri":"spotify:track:alpha","name":"Alpha One",'
                    '"artists":[{"name":"Alpha Artist"}],"album":{"name":"Alpha Album"}}}],'
                    '"next":null,"total":1}'
                ),
            ),
        ]

        def transport(request):
            captured_requests.append(request)
            return responses.pop(0)

        client = SpotifyClient(transport=transport)

        items = client.get_playlist_items(access_token="access-token", playlist_id="playlist-1")

        self.assertEqual(
            items,
            (
                SpotifyPlaylistItem(
                    uri="spotify:track:alpha",
                    name="Alpha One",
                    artists=("Alpha Artist",),
                    album_name="Alpha Album",
                ),
            ),
        )
        self.assertIn("item%28uri", captured_requests[0].url)

    def test_get_playlist_items_rejects_unparsed_nonempty_pages(self):
        responses = [
            HttpResponse(
                status=200,
                headers={},
                body='{"items":[{"item":{"type":"track","name":"Alpha One"}}],"next":null,"total":1}',
            ),
        ]

        def transport(request):
            return responses.pop(0)

        client = SpotifyClient(transport=transport)

        with self.assertRaisesRegex(SpotifyApiError, "could not parse any playlist tracks"):
            client.get_playlist_items(access_token="access-token", playlist_id="playlist-1")

    def test_create_playlist_posts_private_playlist_and_parses_result(self):
        captured_requests = []

        def transport(request):
            captured_requests.append(request)
            return HttpResponse(
                status=201,
                headers={},
                body=(
                    '{"id":"playlist-1","name":"Discogs - House",'
                    '"external_urls":{"spotify":"https://open.spotify.com/playlist/playlist-1"}}'
                ),
            )

        client = SpotifyClient(transport=transport)

        playlist = client.create_playlist(
            access_token="access-token",
            name="Discogs - House",
            public=False,
            description="Generated from Discogs collection",
        )

        self.assertEqual(
            playlist,
            SpotifyPlaylist(
                playlist_id="playlist-1",
                name="Discogs - House",
                url="https://open.spotify.com/playlist/playlist-1",
            ),
        )
        self.assertEqual(captured_requests[0].method, "POST")
        self.assertEqual(captured_requests[0].url, "https://api.spotify.com/v1/me/playlists")
        self.assertEqual(
            json.loads(captured_requests[0].body.decode("utf-8")),
            {
                "name": "Discogs - House",
                "public": False,
                "description": "Generated from Discogs collection",
            },
        )

    def test_add_playlist_items_posts_uri_batches_of_one_hundred(self):
        captured_requests = []

        def transport(request):
            captured_requests.append(request)
            return HttpResponse(status=201, headers={}, body="{}")

        client = SpotifyClient(transport=transport)
        uris = tuple(f"spotify:track:{index}" for index in range(101))

        result = client.add_playlist_items(
            access_token="access-token",
            playlist_id="playlist-1",
            uris=uris,
        )

        self.assertIsNone(result)
        self.assertEqual([request.method for request in captured_requests], ["POST", "POST"])
        self.assertEqual(captured_requests[0].url, "https://api.spotify.com/v1/playlists/playlist-1/items")
        self.assertEqual(len(json.loads(captured_requests[0].body.decode("utf-8"))["uris"]), 100)
        self.assertEqual(json.loads(captured_requests[1].body.decode("utf-8"))["uris"], ["spotify:track:100"])
        self.assertNotIn("position", json.loads(captured_requests[0].body.decode("utf-8")))
        self.assertNotIn("position", json.loads(captured_requests[1].body.decode("utf-8")))

    def test_add_playlist_items_posts_position_when_provided(self):
        captured_requests = []

        def transport(request):
            captured_requests.append(request)
            return HttpResponse(status=201, headers={}, body="{}")

        client = SpotifyClient(transport=transport)

        client.add_playlist_items(
            access_token="access-token",
            playlist_id="playlist-1",
            uris=("spotify:track:alpha", "spotify:track:beta"),
            position=2,
        )

        self.assertEqual(
            json.loads(captured_requests[0].body.decode("utf-8")),
            {
                "uris": ["spotify:track:alpha", "spotify:track:beta"],
                "position": 2,
            },
        )

    def test_replace_playlist_items_puts_first_uri_batch(self):
        captured_requests = []

        def transport(request):
            captured_requests.append(request)
            return HttpResponse(status=200, headers={}, body="{}")

        client = SpotifyClient(transport=transport)

        result = client.replace_playlist_items(
            access_token="access-token",
            playlist_id="playlist-1",
            uris=("spotify:track:alpha", "spotify:track:beta"),
        )

        self.assertIsNone(result)
        self.assertEqual(captured_requests[0].method, "PUT")
        self.assertEqual(captured_requests[0].url, "https://api.spotify.com/v1/playlists/playlist-1/items")
        self.assertEqual(
            json.loads(captured_requests[0].body.decode("utf-8"))["uris"],
            ["spotify:track:alpha", "spotify:track:beta"],
        )

    def test_add_playlist_items_raises_spotify_api_error_for_non_201(self):
        def transport(request):
            return HttpResponse(status=400, headers={}, body="fixed body")

        client = SpotifyClient(transport=transport)

        with self.assertRaises(SpotifyApiError) as raised:
            client.add_playlist_items(
                access_token="access-token",
                playlist_id="playlist-1",
                uris=("spotify:track:alpha",),
            )

        self.assertEqual(
            str(raised.exception),
            "Spotify playlist add items failed with status 400: fixed body",
        )

    def test_replace_playlist_items_raises_spotify_api_error_for_non_200(self):
        def transport(request):
            return HttpResponse(status=400, headers={}, body="fixed body")

        client = SpotifyClient(transport=transport)

        with self.assertRaises(SpotifyApiError) as raised:
            client.replace_playlist_items(
                access_token="access-token",
                playlist_id="playlist-1",
                uris=("spotify:track:alpha",),
            )

        self.assertEqual(
            str(raised.exception),
            "Spotify playlist replace items failed with status 400: fixed body",
        )

    def test_add_playlist_items_preserves_success_response_json_validation(self):
        def transport(request):
            return HttpResponse(status=201, headers={}, body="not json")

        client = SpotifyClient(transport=transport)

        with self.assertRaises(json.JSONDecodeError):
            client.add_playlist_items(
                access_token="access-token",
                playlist_id="playlist-1",
                uris=("spotify:track:alpha",),
            )

    def test_replace_playlist_items_preserves_success_response_json_validation(self):
        def transport(request):
            return HttpResponse(status=200, headers={}, body="not json")

        client = SpotifyClient(transport=transport)

        with self.assertRaises(json.JSONDecodeError):
            client.replace_playlist_items(
                access_token="access-token",
                playlist_id="playlist-1",
                uris=("spotify:track:alpha",),
            )

    def test_search_tracks_honors_retry_after_before_retrying_rate_limit(self):
        responses = [
            HttpResponse(status=429, headers={"Retry-After": "2"}, body='{"error":"rate limited"}'),
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"tracks":{"items":[{"uri":"spotify:track:alpha","name":"Alpha One",'
                    '"artists":[{"name":"Alpha Artist"}],"album":{"name":"Alpha Album"}}]}}'
                ),
            ),
        ]
        sleep_calls = []

        def transport(request):
            return responses.pop(0)

        client = SpotifyClient(transport=transport, sleep=sleep_calls.append)

        candidates = client.search_tracks(
            access_token="access-token",
            query='track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
        )

        self.assertEqual(sleep_calls, [2.0])
        self.assertEqual(candidates[0].uri, "spotify:track:alpha")

    def test_search_tracks_writes_safe_debug_logs_for_rate_limit_retries(self):
        responses = [
            HttpResponse(status=429, headers={"Retry-After": "2"}, body="Too many requests"),
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"tracks":{"items":[{"uri":"spotify:track:alpha","name":"Alpha One",'
                    '"artists":[{"name":"Alpha Artist"}],"album":{"name":"Alpha Album"}}]}}'
                ),
            ),
        ]
        debug_lines = []

        def transport(request):
            return responses.pop(0)

        client = SpotifyClient(
            transport=transport,
            sleep=lambda _seconds: None,
            debug_log=debug_lines.append,
        )

        candidates = client.search_tracks(
            access_token="access-token",
            query='track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
        )

        self.assertEqual(candidates[0].uri, "spotify:track:alpha")
        self.assertEqual(
            debug_lines,
            [
                "spotify_search_request attempt=1",
                "spotify_search_response attempt=1 status=429 retry_after=2",
                "spotify_rate_limit_retry attempt=1 max_retries=3 wait_seconds=2.0",
                "spotify_search_request attempt=2",
                "spotify_search_response attempt=2 status=200 retry_after=(none)",
            ],
        )
        self.assertNotIn("Alpha One", "\n".join(debug_lines))
        self.assertNotIn("access-token", "\n".join(debug_lines))

    def test_search_tracks_writes_transport_error_kind_for_status_zero(self):
        debug_lines = []

        def transport(request):
            return HttpResponse(
                status=0,
                headers={},
                body="<urlopen error [Errno 8] nodename nor servname provided>",
            )

        client = SpotifyClient(
            transport=transport,
            debug_log=debug_lines.append,
        )

        with self.assertRaisesRegex(SpotifyApiError, "Spotify search failed with status 0"):
            client.search_tracks(
                access_token="access-token",
                query='track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
            )

        self.assertEqual(
            debug_lines,
            [
                "spotify_search_request attempt=1",
                "spotify_search_response attempt=1 status=0 retry_after=(none) transport_error=dns_or_name_resolution",
            ],
        )
        self.assertNotIn("Alpha One", "\n".join(debug_lines))
        self.assertNotIn("access-token", "\n".join(debug_lines))

    def test_search_tracks_reads_retry_after_header_case_insensitively(self):
        responses = [
            HttpResponse(status=429, headers={"retry-after": "3"}, body="Too many requests"),
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"tracks":{"items":[{"uri":"spotify:track:alpha","name":"Alpha One",'
                    '"artists":[{"name":"Alpha Artist"}],"album":{"name":"Alpha Album"}}]}}'
                ),
            ),
        ]
        sleep_calls = []

        def transport(request):
            return responses.pop(0)

        client = SpotifyClient(transport=transport, sleep=sleep_calls.append)

        candidates = client.search_tracks(
            access_token="access-token",
            query='track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
        )

        self.assertEqual(sleep_calls, [3.0])
        self.assertEqual(candidates[0].uri, "spotify:track:alpha")

    def test_default_retry_policy_honors_large_retry_after_values(self):
        retry_policy = SpotifyRetryPolicy()

        self.assertEqual(retry_policy.rate_limit_wait_seconds({"Retry-After": "9999"}), 9999.0)

    def test_search_tracks_retries_repeated_rate_limits_until_success(self):
        responses = [
            HttpResponse(status=429, headers={"Retry-After": "2"}, body="Too many requests"),
            HttpResponse(status=429, headers={"Retry-After": "4"}, body="Too many requests"),
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"tracks":{"items":[{"uri":"spotify:track:alpha","name":"Alpha One",'
                    '"artists":[{"name":"Alpha Artist"}],"album":{"name":"Alpha Album"}}]}}'
                ),
            ),
        ]
        sleep_calls = []

        def transport(request):
            return responses.pop(0)

        client = SpotifyClient(
            transport=transport,
            sleep=sleep_calls.append,
            retry_policy=SpotifyRetryPolicy(max_rate_limit_retries=3),
        )

        candidates = client.search_tracks(
            access_token="access-token",
            query='track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
        )

        self.assertEqual(sleep_calls, [2.0, 4.0])
        self.assertEqual(candidates[0].uri, "spotify:track:alpha")

    def test_search_tracks_uses_fallback_wait_when_rate_limit_has_no_retry_after(self):
        responses = [
            HttpResponse(status=429, headers={}, body="Too many requests"),
            HttpResponse(
                status=200,
                headers={},
                body=(
                    '{"tracks":{"items":[{"uri":"spotify:track:alpha","name":"Alpha One",'
                    '"artists":[{"name":"Alpha Artist"}],"album":{"name":"Alpha Album"}}]}}'
                ),
            ),
        ]
        sleep_calls = []

        def transport(request):
            return responses.pop(0)

        client = SpotifyClient(
            transport=transport,
            sleep=sleep_calls.append,
            retry_policy=SpotifyRetryPolicy(max_rate_limit_retries=1, fallback_retry_after_seconds=30),
        )

        candidates = client.search_tracks(
            access_token="access-token",
            query='track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
        )

        self.assertEqual(sleep_calls, [30.0])
        self.assertEqual(candidates[0].uri, "spotify:track:alpha")

    def test_search_tracks_reports_rate_limit_after_retry_budget_is_exhausted(self):
        responses = [
            HttpResponse(status=429, headers={"Retry-After": "1"}, body="Too many requests"),
            HttpResponse(status=429, headers={"Retry-After": "1"}, body="Too many requests"),
            HttpResponse(status=429, headers={"Retry-After": "1"}, body="Too many requests"),
        ]
        sleep_calls = []

        def transport(request):
            return responses.pop(0)

        client = SpotifyClient(
            transport=transport,
            sleep=sleep_calls.append,
            retry_policy=SpotifyRetryPolicy(max_rate_limit_retries=2),
        )

        with self.assertRaisesRegex(SpotifyRateLimitRetriesExhaustedError, "Spotify rate limit retries were exhausted"):
            client.search_tracks(
                access_token="access-token",
                query='track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
            )

        self.assertEqual(sleep_calls, [1.0, 1.0])

    def test_search_tracks_fails_fast_when_retry_after_exceeds_max_wait(self):
        responses = [
            HttpResponse(status=429, headers={"Retry-After": "9999"}, body="Too many requests"),
        ]
        sleep_calls = []
        debug_lines = []

        def transport(request):
            return responses.pop(0)

        client = SpotifyClient(
            transport=transport,
            sleep=sleep_calls.append,
            retry_policy=SpotifyRetryPolicy(max_rate_limit_retries=1, max_rate_limit_wait_seconds=480),
            debug_log=debug_lines.append,
        )

        with self.assertRaises(SpotifyRateLimitDeferredError) as error:
            client.search_tracks(
                access_token="access-token",
                query='track:"Alpha One" artist:"Alpha Artist" album:"Alpha Album"',
            )

        self.assertEqual(
            str(error.exception),
            "Spotify rate limit Retry-After is 2 hours 46 minutes 39 seconds, exceeding max wait 8 minutes. "
            "Retry later. After the cooldown expires, run only scripts/publishers/spotify/publish_playlist.py.",
        )
        self.assertEqual(sleep_calls, [])
        self.assertEqual(
            debug_lines,
            [
                "spotify_search_request attempt=1",
                "spotify_search_response attempt=1 status=429 retry_after=9999",
                "spotify_rate_limit_deferred attempt=1 max_wait_seconds=480 retry_after_seconds=9999.0",
            ],
        )
        self.assertNotIn("Alpha One", "\n".join(debug_lines))
        self.assertNotIn("access-token", "\n".join(debug_lines))


if __name__ == "__main__":
    unittest.main()
