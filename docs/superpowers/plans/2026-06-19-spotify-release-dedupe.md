# Spotify Metadata Dedupe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for implementation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the Spotify publisher from adding the same artist/album/track combination to the same target playlist more than once, while allowing the same artist/track when it appears on a different album.

**Architecture:** Use a Spotify-visible publish identity key built from target playlist, normalized artist names, normalized album name, and normalized track name. Existing playlist items and newly matched candidates both expose this metadata, so append-mode duplicate checks do not need Discogs `release_id` or a migration ledger. Keep `Release Id` in reports as audit context only.

**Tech Stack:** Python standard library, existing Spotify publisher modules, `unittest`.

---

## Identity Rule

The duplicate key is:

```text
normalized target playlist | normalized artist names | normalized album | normalized track
```

Expected behavior:

- Same target playlist + artist + album + track: skip.
- Same target playlist + artist + track + different album: allow.
- Different target playlist: allow.
- Same Spotify URI with different album metadata: allow.
- `release_id` is not part of the duplicate key.

## Files

- Modify `scripts/publishers/spotify/publish_playlist.py`
  - Add identity-key helpers.
  - Build `existing_identity_keys` from Spotify playlist items.
  - Build `seen_source_identity_keys` from matched candidates in the current run.
  - Check duplicate status by identity key instead of Spotify URI.
- Modify `tests/spotify/test_publish_playlist.py`
  - Add regression tests for same metadata/different URI and same track/different album.
  - Update existing expectations where duplicate wording changes from URI to metadata identity.
- Modify `README.md`
  - Document append-mode duplicate behavior as artist/album/track based.

## Task 1: Write Failing Tests

- [ ] Add `test_append_skips_existing_track_identity_even_if_spotify_uri_changed`.

This creates an existing playlist item:

```python
SpotifyPlaylistItem(
    uri="spotify:track:old",
    name="Reprobation",
    artists=("Cauê",),
    album_name="Revelations",
)
```

Then the current CSV row for release `30887115` matches:

```python
SpotifyTrackCandidate(
    uri="spotify:track:new",
    name="Reprobation",
    artists=("Cauê",),
    album_name="Revelations",
)
```

Expected result:

```python
self.assertEqual(summary.already_present_count, 1)
self.assertEqual(summary.would_add_count, 0)
self.assertEqual(client.add_calls, [])
```

- [ ] Add `test_append_allows_same_artist_and_track_on_different_albums`.

Two rows have the same artist and track, but different albums:

```python
playlist_row("111", "First Album", "Shared Track", "Shared Artist")
playlist_row("222", "Second Album", "Shared Track", "Shared Artist")
```

Expected result:

```python
self.assertEqual([decision.status for decision in summary.decisions], ["would_add", "would_add"])
self.assertEqual(summary.duplicate_in_source_count, 0)
self.assertEqual(summary.would_add_count, 2)
```

- [ ] Add `test_append_allows_same_spotify_uri_on_different_albums`.

The two rows above both resolve to the same Spotify URI, but candidate album names differ. Expected result is still two `would_add` decisions.

- [ ] Run the targeted tests and verify they fail before production code changes:

```bash
python3 -m unittest \
  tests.spotify.test_publish_playlist.SpotifyPublishPlaylistTests.test_append_skips_existing_track_identity_even_if_spotify_uri_changed \
  tests.spotify.test_publish_playlist.SpotifyPublishPlaylistTests.test_append_allows_same_artist_and_track_on_different_albums \
  tests.spotify.test_publish_playlist.SpotifyPublishPlaylistTests.test_append_allows_same_spotify_uri_on_different_albums
```

## Task 2: Implement Metadata Identity Helpers

- [ ] Add helpers in `publish_playlist.py`:

```python
def spotify_playlist_item_identity_key(target_playlist_name: str, item: SpotifyPlaylistItem) -> str:
    return spotify_track_identity_key(
        target_playlist_name=target_playlist_name,
        artist_names=item.artists,
        album_name=item.album_name,
        track_name=item.name,
    )


def spotify_candidate_identity_key(target_playlist_name: str, candidate: SpotifyTrackCandidate) -> str:
    return spotify_track_identity_key(
        target_playlist_name=target_playlist_name,
        artist_names=candidate.artists,
        album_name=candidate.album_name,
        track_name=candidate.name,
    )


def spotify_track_identity_key(
    target_playlist_name: str,
    artist_names: Sequence[str],
    album_name: str,
    track_name: str,
) -> str:
    normalized_artists = " ".join(sorted(normalize_music_text(artist) for artist in artist_names if normalize_music_text(artist)))
    return "|".join(
        (
            normalize_music_text(target_playlist_name),
            normalized_artists,
            normalize_music_text(album_name),
            normalize_music_text(track_name),
        )
    )
```

- [ ] Build existing identities in append mode:

```python
existing_identity_keys = {
    spotify_playlist_item_identity_key(target_playlist_name, item)
    for item in existing_items
}
```

- [ ] Replace `seen_source_uris` with `seen_source_identity_keys`.

- [ ] In `build_publish_decision`, skip as `duplicate_in_source` when the candidate identity key has already been planned in this run.

- [ ] In append mode, skip as `already_present` when the candidate identity key is in `existing_identity_keys`.

## Task 3: Update Reports And Docs

- [ ] Update duplicate reasons:

```text
Spotify artist, album, and track already planned from an earlier local row
Spotify artist, album, and track already exist in playlist
```

- [ ] Update README append-mode docs to say duplicate checks use Spotify-visible artist, album, and track metadata, not `release_id`.

## Task 4: Verify

- [ ] Run targeted Spotify publisher tests:

```bash
python3 -m unittest tests.spotify.test_publish_playlist
```

- [ ] Run full unit tests:

```bash
python3 -m unittest discover -s tests
```

- [ ] Optional live-safe check:

```bash
python3 scripts/publishers/spotify/publish_playlist.py --playlists Ambient --publishing-dry-run --no-progress
```

Review the report before any real publish. The Revelations rows should be `already_present` if Spotify currently contains matching artist/album/track items.

## Self-Review

- This plan now matches the clarified rule: same artist and track may repeat across different albums.
- It does not require a migration ledger because Spotify exposes the metadata needed for existing-playlist de-dupe.
- Remaining fragility: Spotify album names can differ across single, EP, deluxe, remaster, or punctuation variants. The implementation should start conservative and not over-collapse albums.
