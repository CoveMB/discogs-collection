# Discogs collection tools

This repo contains small Python CLIs for working with Discogs collection export
CSVs. The main workflow is:

1. Enrich a Discogs export with explicit `Style` and `Genre` metadata from the
   Discogs release API.
2. Map those Discogs terms to local playlist labels.
3. Export full TuneMyMusic-style playlist master CSVs into per-playlist folders.
4. Create split CSVs from each playlist master, defaulting to 500 rows per file.
5. Optionally publish Spotify playlists from those playlist CSVs, or run an
   explicit dry-run preview first.

You can also create an on-the-fly publisher playlist from explicit Discogs
`release_id` values without adding those releases to the collection master.

The durable file is the enriched master CSV. By default that file is:

```text
collection/enriched-collection.csv
```

The scripts are local-first. They read and write CSV, JSON cache, and plain text
report files on disk. The enrichment, playlist exporter, and release-ID
playlist scripts can call Discogs. The Spotify publisher can call Spotify to
plan playlist changes, and it creates or updates Spotify playlists unless you
pass `--publishing-dry-run`. The Spotify dedupe command can remove exact track
URI duplicates from repo-managed Spotify publisher playlists, but only when you
pass `--apply`.

## Get a Discogs collection export

Start with a Discogs collection export CSV, not a Marketplace inventory export.
In Discogs, open your Collection page from your dashboard or the profile menu.
Use the collection settings/options area to choose `Export My Collection`.
Discogs redirects you to the Export page. Select `Collection` from the dropdown,
choose `Request Data Export`, then download the CSV after Discogs says the file
is available.

Discogs documents this flow in its Collection feature help page:
<https://support.discogs.com/hc/en-us/articles/360007331534-How-Does-The-Collection-Feature-Work>

`scripts/discogs_style_enricher.py` reads the `release_id` in each row, looks up
the matching Discogs release, and writes the explicit Discogs styles and genres
into `Style` and `Genre` columns.

The tool is built for repeat use. Keep one enriched CSV as your master file,
then merge each new Discogs collection export into that master. Export rows with
new `release_id` values are appended. Export rows whose `release_id` already
exists in the master refresh that master row's export fields in place. Export
rows with missing `release_id` values are skipped and listed in the report.
Already-filled style or genre values are preserved by default, and only missing
metadata is looked up.

## What the enricher changes

The script adds these columns when they are missing:

- `Style`
- `Genre`
- `Style Notes`
- `Genre Notes`
- `Updated At`

The enrichment columns are kept together. If the CSV already has `Style`, the
enrichment block is placed at that position. Otherwise the columns are added
after `Released` when that column is present. The output keeps `release_id` as
the first column when that column exists.

`Style` contains a comma-separated list of styles, for example:

```text
Soul, Funk, Electro, Deep House
```

`Genre` contains a comma-separated list of Discogs genres, for example:

```text
Electronic, Hip Hop
```

The audit columns explain what happened:

- `Style Notes` records style-specific context, such as a master release ID,
  failed lookup source, missing `release_id`, or `no explicit styles found`.
- `Genre Notes` records the same kind of context for genre lookups, including
  `no explicit genres found`.
- `Updated At` is a UTC timestamp written when the script fills a style or
  genre, or when it marks a row as blank or errored.

Lookup source and status are not written as CSV columns. They are kept in the
local JSON cache for audit/debug use.

If an older CSV still has `Style Source`, `Style Status`, or `Style Updated At`,
the current output drops those legacy columns. When `Updated At` is blank and
`Style Updated At` exists, the script copies that legacy timestamp into
`Updated At`.

Rows with existing `Style` or `Genre` values are left alone unless you pass
`--refresh-existing`. Preservation is field-specific, so a row with an existing
`Style` and a missing `Genre` still gets a genre lookup.

## Quick start

From the repository root, put one Discogs collection export CSV in `export`.
Then run:

```bash
python3 scripts/discogs_style_enricher.py
```

The script reads the only CSV in `export`, validates that it has the expected
Discogs export columns, and writes the enriched master file to:

```text
collection/enriched-collection.csv
```

If the master file does not exist yet, the script creates it from the export.
After a run with no lookup errors, the processed export is moved to `processed`.
If lookup errors occur, the export stays in `export` so you can rerun it.

When run in an interactive terminal, the script shows row progress on `stderr`
with a same-line progress bar, row count, and percentage. The final run summary
still prints at the end. Progress output is suppressed when `stderr` is not a
terminal, and you can turn it off with `--no-progress`.

You can still pass explicit paths when you want to run against a specific file:

```bash
python3 scripts/discogs_style_enricher.py \
  --export /path/to/new-discogs-export.csv \
  --master /path/to/master-with-styles.csv
```

## Recommended workflow

1. Export your collection from Discogs as CSV.
2. Put that CSV in `export`. Leave exactly one CSV in that folder.
3. Run `python3 scripts/discogs_style_enricher.py`.
4. Open the generated report and check rows listed as blank, not sure, or error.
5. Review any new Discogs style or genre terms listed in the report.
6. Keep `collection/enriched-collection.csv` as your master for the next run.
7. When you want playlist labels, run `python3 scripts/discogs_playlist_mapper.py`
   against the enriched master.
8. When you want TuneMyMusic import files, run
   `python3 scripts/discogs_playlist_exporter.py` against that mapped master.
9. Run `python3 scripts/discogs_playlist_splitter.py` to create split CSVs from
   each playlist master.
10. Upload the split CSVs to TuneMyMusic when you want to create the actual
   streaming-service playlist, or run the Spotify publisher described
   below to preview direct Spotify publishing.

This workflow avoids redoing work. The enrichment script keeps a local JSON
cache beside the output CSV, so later runs can reuse earlier release lookups.

The `export` folder may contain non-CSV files, but it must contain exactly one
CSV. If it contains zero CSV files or more than one CSV file, the script stops
before doing any lookups.

## One-command playlist workflow

To run enrichment, playlist mapping, TuneMyMusic export, and playlist splitting
one after the other, use:

```bash
python3 scripts/discogs_make_playlists.py
```

With no options, the command uses the same defaults as the individual scripts:

- enrich from `export` into `collection/enriched-collection.csv`
- map playlists in `collection/enriched-collection.csv`
- export playlist master CSVs into folders under `collection/playlists`
- write split CSVs under each playlist folder, using `config/workflow.json`
  when it exists and creating it with 500-row defaults when it does not
- read `config/publisher.json` for the final publisher step, creating a default
  config that skips publishing when the file does not exist

The command stops before the next step when a step exits with a nonzero status.
For example, if enrichment reports lookup errors, mapping, playlist export,
playlist splitting, and publishing do not run. If playlist splitting fails,
publishing does not run. Check the relevant report, then rerun after the issue
is resolved.

The child enrichment and exporter scripts still read `DISCOGS_TOKEN` from the
environment. The combined command does not have a separate token option.

Common path overrides are available when you want the same master file or config
used across the full workflow:

```bash
python3 scripts/discogs_make_playlists.py \
  --export export/latest-discogs-export.csv \
  --master collection/enriched-collection.csv \
  --config config/playlist-map.json \
  --workflow-config config/workflow.json \
  --publisher-config config/publisher.json \
  --playlist-output-dir collection/playlists \
  --split-report reports/playlist_splits.txt
```

The workflow script reads `default_publisher` from the publisher config when
you omit `--publisher`. The default config sets `default_publisher` to `none`,
so the final step prints a skip notice and does not call Spotify. To publish
from the combined workflow, pass `--publisher spotify` or set
`default_publisher` to `spotify`. Add `--publishing-dry-run` when you want the
resolved Spotify publisher to preview changes without creating or updating
playlists.

Pass `--skip-publish-playlist` to skip the final publisher for one run, even
when the publisher config defaults to Spotify. This is a convenience alias for
`--publisher none`.

When Spotify publishing is enabled, the combined workflow accepts
`--max-new-searches-per-run N` and passes it to the Spotify publisher. Use `0`
for an uncapped publisher run.

`config/publisher.json` defaults to:

```json
{
  "default_publisher": "none",
  "playlist_prefix": "Discogs - ",
  "playlist_suffix": ""
}
```

`default_publisher` must be `spotify` or `none` and controls the combined
workflow when you omit `--publisher`. The Spotify publisher uses
`playlist_prefix` and `playlist_suffix` when it resolves target playlist names.
Both values must be strings. With the default config, local `House` publishes to
Spotify playlist `Discogs - House` when Spotify publishing is enabled.

## Spotify publisher

`scripts/publishers/spotify/publish_playlist.py` reads the per-playlist master
CSVs under `collection/playlists`, matches rows to Spotify tracks, fetches the
current Spotify playlist state, and writes a local publish report. When you run
this script directly, it creates or updates Spotify playlists by default. Pass
`--publishing-dry-run` to preview the same decisions without writing to Spotify.
There is no `--apply` flag for publishing; omitting `--publishing-dry-run` is
the mode that can write changes.

Spotify settings come from `.env` by default. Copy `.env.example` to `.env` or
create `.env` with:

```text
SPOTIFY_CLIENT_ID="your-client-id"
SPOTIFY_REDIRECT_URI="http://127.0.0.1:8765/callback"
```

To create the Spotify app and fill `.env`:

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Create an app. Spotify asks for an app name, description, and developer terms
   confirmation.
3. Open the new app settings and add the same redirect URI you will put in
   `.env`, for example `http://127.0.0.1:8765/callback`.
4. Save the app settings, then copy the app's Client ID into
   `SPOTIFY_CLIENT_ID` in `.env`.
5. Leave `SPOTIFY_REDIRECT_URI` set to the exact redirect URI saved in the
   Spotify app settings.

Use a numeric loopback IP in the redirect URI, such as `127.0.0.1`; Spotify's
[redirect URI rules](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)
do not allow `localhost`, and this local callback also rejects `localhost`,
`0.0.0.0`, and non-local hosts. The script uses a local PKCE flow, so it does
not read or need a Spotify client secret. Do not add a client secret to `.env`
for this script.

The script writes the token cache to:

```text
config/cache/spotify-token.cache.json
```

The track match cache is separate from the generated playlist CSVs:

```text
collection/cache/spotify-track-matches.cache.json
```

The cache is JSON, ignored by git, and keyed by release ID, track number, artist,
album, and track name. The publisher reads it before searching Spotify. It stores
`matched`, `ambiguous`, and `unmatched` decisions so later runs can avoid repeat
searches. Search errors are not cached.

Append publishing also keeps a small publish-state cache:

```text
collection/cache/spotify-publish-state.cache.json
```

This cache records tracks the publisher has seen in, or successfully added to, a
target Spotify playlist. It is separate from the match cache because dry runs and
failed searches can populate match data without proving that a track has ever
belonged in the Spotify playlist.

Preview the publisher without writing to Spotify:

```bash
python3 scripts/publishers/spotify/publish_playlist.py \
  --playlist-output-dir collection/playlists \
  --publishing-dry-run
```

To process only specific playlists, pass one or more explicit selectors:

```bash
python3 scripts/publishers/spotify/publish_playlist.py \
  --playlists "House" collection/playlists/Techno
```

Each selector can be a playlist display name, playlist folder name, playlist
folder path, or playlist master CSV path. Omit `--playlists` to process every
playlist. The value `all` is not accepted for `--playlists`; omitting the flag is
the all-playlists path.

If the token cache is missing, expired, invalid, or missing the required Spotify
scopes, the script opens the Spotify login page and then continues the run. The
publisher requests playlist read/write access and `user-read-private`; it uses
the profile scope only to identify the current Spotify user before choosing a
playlist target. To force a fresh login, pass `--reauthorize`:

```bash
python3 scripts/publishers/spotify/publish_playlist.py --reauthorize
```

When run in an interactive terminal, the script shows Spotify planning progress
on `stderr` with the same-line progress bar used by the enrichment and playlist
export scripts. You can turn it off with `--no-progress`.

Spotify target playlist names come from `config/publisher.json`. The publisher
uses `playlist_prefix` and `playlist_suffix` around each local playlist folder
name. With the default config, local `House` publishes to Spotify playlist
`Discogs - House`. Existing targets must be owned private playlists. The
publisher ignores followed playlists with the same name and creates an owned
private playlist instead. Public and collaborative playlists with the target
name are rejected before any track writes.

The sync mode defaults to append:

```bash
python3 scripts/publishers/spotify/publish_playlist.py \
  --publisher-sync-mode append
```

In append mode, the publisher fetches the existing Spotify playlist first. It
uses Spotify-visible metadata to avoid duplicates: target playlist, artist,
album, and track. A row is reported as `already_present` when the same artist,
album, and track already exist in the target playlist, even if the current match
has a different Spotify URI. Duplicate artist, album, and track matches inside
the same source run are skipped and reported. The same artist and track can
still be added when it appears on a different album.

Append mode is source-aware. If a track is missing from Spotify but the
publish-state cache shows that the publisher already knew that track for the
target playlist, the script inserts it before the next current source-order
anchor. Manual Spotify-only tracks keep their relative order. If the missing
track is new to the publisher, the script appends it to the end. A track deleted
before the publish-state cache ever observed it cannot be distinguished from a
newly seen track, so the first source-aware run may still append that track.

Append publishing is incremental. The publisher reads the playlist CSVs in
source order and checks the local Spotify match cache before searching Spotify.
For uncached rows, it searches up to 500 new tracks per run by default. Cache
hits do not count against that limit. As matched tracks are found, the publisher
writes source-aware append batches of up to 100 new URIs, saves the match cache
and publish-state cache, and rewrites the report. When the search budget is
reached, the run exits successfully with a partial run status. Later runs start
from the CSVs again, reuse cached matches, fetch the current Spotify playlist
contents, and skip tracks that are already present.

To change the per-run search budget:

```bash
python3 scripts/publishers/spotify/publish_playlist.py \
  --max-new-searches-per-run 50
```

Use `--max-new-searches-per-run 0` for an uncapped run. If Spotify throttles
before the default 500 new searches, rerun the publisher after the cooldown and
use a lower value such as `50` or `25`.

Replace mode plans the final playlist from the matched local CSV rows in source
order:

```bash
python3 scripts/publishers/spotify/publish_playlist.py \
  --publisher-sync-mode replace
```

In dry-run replace mode, the report shows the playlist that would be written
without modifying Spotify. In the default publishing mode, replace mode stops
before writing if any row is ambiguous, unmatched, or errored. Spotify accepts up
to 100 URIs in one replace request, so the publisher replaces the first batch
and appends the remaining batches. If a later batch fails after the first
replace request succeeds, the report marks that playlist as partially replaced
so you know the remote playlist may need manual repair.

When checking Spotify, the terminal prints one line per target playlist:

```text
Playlist Discogs - House does not exist, creating
Playlist Discogs - House already exists with 42 songs, updating
```

In dry runs, the missing-playlist message says `would create` instead of
`creating`. The same messages appear in the report.

To write changes to Spotify, omit `--publishing-dry-run`:

```bash
python3 scripts/publishers/spotify/publish_playlist.py \
  --publisher-sync-mode append
```

For each uncached row, the script builds a structured Spotify search query from
`Track Name`, `Artist Name`, and `Album Name`. It falls back to the existing
`Spotify Search Query` column only when the structured fields are blank. A track
is marked `matched` only when one Spotify result matches track, artist, and
album after normalization. Multiple matching candidates are marked `ambiguous`,
and rows with no matching candidate are marked `unmatched`.

The Spotify match cache stores `matched`, `ambiguous`, and `unmatched` decisions.
Later runs reuse those decisions instead of searching Spotify again. Search
errors are not cached, since they usually mean Spotify or the network failed
temporarily. To recheck every row and replace cached decisions with fresh Spotify
results without writing playlist changes to Spotify, run:

```bash
python3 scripts/publishers/spotify/publish_playlist.py \
  --publishing-dry-run \
  --refresh-match-cache
```

If you omit `--publishing-dry-run`, the publisher also creates or updates
Spotify playlists according to the selected sync mode.

The report includes run status, summary counts, playlist checks, per-row publish
decisions, review sections, and the final planned playlist state. The final-state
section lists position, status, artist, track, album, and Spotify URI for each
playlist. In append mode it starts with the current Spotify playlist order and
then adds the planned new tracks. In replace mode it shows the replacement
playlist. Ambiguous, unmatched, and error rows are not added to the final planned
state.

If Spotify rate-limits a request, the client waits and retries. It honors
`Retry-After` exactly when Spotify sends it, and uses a 60-second wait when the
header is missing or invalid. If Spotify keeps returning `429` after three
retries, or sends a `Retry-After` longer than the allowed wait, the publisher
treats that as a rate-limit stop instead of a row-level search error. It saves
the match cache, writes an aborted partial report for rows already planned,
prints that report path, and exits with a nonzero status. In append mode,
completed append checkpoints stay written; uncheckpointed pending rows are not
written after the rate-limit stop. After the cooldown, run the publisher again
without `--refresh-match-cache` so it can reuse the saved decisions. The
completed report is rebuilt from the playlist CSVs and the match cache, so it
includes cached decisions from earlier attempts and new results from the final
run.

## Dedupe Spotify publisher playlists

`scripts/dedupe_playlists.py` checks provider playlists for duplicate tracks and
plans removals. The first supported provider is Spotify.

The script only considers Spotify playlists that are owned by the current user,
private, non-collaborative, and named with the publisher prefix or suffix from
`config/publisher.json`. With the default config, that means playlists named
like `Discogs - House`. Followed playlists, public playlists, collaborative
playlists, playlists with unknown privacy status, and playlists that do not
match the configured publisher naming are skipped. If both `playlist_prefix` and
`playlist_suffix` are empty, the script stops before calling Spotify because it
cannot identify repo-managed playlists safely.

Preview duplicate removals:

```bash
python3 scripts/dedupe_playlists.py --provider spotify
```

Preview duplicate removals for selected playlists:

```bash
python3 scripts/dedupe_playlists.py --provider spotify --playlists House Techno
```

Apply duplicate removals:

```bash
python3 scripts/dedupe_playlists.py --provider spotify --apply
```

Apply duplicate removals for selected playlists:

```bash
python3 scripts/dedupe_playlists.py --provider spotify --playlists House Techno --apply
```

`--playlists` accepts one or more selectors. Each selector can be the local
playlist label, such as `House`, the Spotify target name, such as
`Discogs - House`, or the Spotify playlist ID. Each selected playlist must still
pass the owned, private, non-collaborative, publisher-managed checks. If any
selector is blank, `all`, ambiguous, missing, or matches only skipped playlists,
the script stops before fetching playlist tracks.

The singular `--playlist` selector is not supported. Use `--playlists` even when
you want to process one playlist.

Duplicates are exact Spotify track URI matches inside the same playlist. The
same track URI in two different playlists is not a duplicate. For each duplicate
URI, the script keeps the item with the earliest Spotify `added_at` timestamp.
If timestamps are missing, invalid, or tied, it keeps the earlier playlist
position.

The report defaults to:

```text
reports/YYYY-MM-DD_HH-MM-SS_dedupe.txt
```

The report lists fetched playlists, eligible playlists, skipped playlists,
planned duplicate removals, and applied removals. It does not update collection
CSVs, playlist master CSVs, split CSVs, or Spotify match caches.

## On-the-fly release playlists

Use `scripts/discogs_release_playlist.py` when you already have Discogs
`release_id` values and want a playlist without importing those releases into
the collection master. The script fetches Discogs tracklists and writes a
TuneMyMusic-style playlist master. It does not read or write
`collection/enriched-collection.csv`, does not run style or genre mapping, and
does not run the playlist splitter.

Preview a Spotify playlist from release IDs:

```bash
python3 scripts/discogs_release_playlist.py \
  --name "Friday Picks" \
  --publisher spotify \
  --publishing-dry-run \
  123456 789012
```

The script writes a TuneMyMusic-style master CSV under the on-the-fly playlist
folder:

```text
collection/playlists/on-the-fly/Friday Picks/Friday Picks.csv
```

Reusing the same playlist name rewrites that on-the-fly master CSV. Duplicate
release IDs are deduped in first-seen order and listed in the release playlist
report. You can pass release IDs as arguments, with `--release-ids-file`, or
with both. Files may use whitespace or commas between IDs.

If the playlist name contains characters that are unsafe in file names, the
folder and CSV file names are sanitized. The publisher name is still the exact
`--name` value. For example, `--name "Friday/Picks"` writes
`collection/playlists/on-the-fly/Friday_Picks/Friday_Picks.csv`, while Spotify
uses `Friday/Picks` as the target playlist name.

Names that would resolve outside the on-the-fly output folder are rejected. If
two different names sanitize to the same folder, the script stops instead of
overwriting the earlier playlist master. Reusing the same exact name still
updates that playlist's master CSV. The playlist folder stores a small
`.release-playlist.json` file to track the exact name.

The default publisher still comes from `config/publisher.json`, and `--publisher`
overrides it. If the resolved publisher is `none`, the script only writes the
CSV and report. If the resolved publisher is `spotify`, it publishes the
generated master CSV by exact path. On-the-fly release playlists do not use
`playlist_prefix` or `playlist_suffix`.

When this script publishes to Spotify, `--max-new-searches-per-run` uses the
same uncached search budget as the main Spotify publisher. The default is 500
uncached searches per run. Use `0` for an uncapped publisher run.

The release-ID workflow reuses the same local caches as the collection workflow:

```text
collection/cache/playlist-tracks.cache.json
collection/cache/spotify-track-matches.cache.json
```

These caches store lookup and match data only. The script does not use the
style and genre enrichment cache, and it does not store playlist labels or
collection row order. If a release from an on-the-fly playlist later appears in
a Discogs collection export, the main workflow adds it according to the export
and the existing master merge. The earlier on-the-fly lookup does not move it to
the front.

Normal runs of `scripts/discogs_make_playlists.py` use `collection/playlists`
and skip the nested on-the-fly playlist masters. If you want to publish or split
only those on-the-fly masters, point the command directly at
`collection/playlists/on-the-fly`.

You can also read release IDs from a file:

```bash
python3 scripts/discogs_release_playlist.py \
  --name "Friday Picks" \
  --release-ids-file release-ids.txt
```

## Playlist mapper

`scripts/discogs_playlist_mapper.py` is a separate local step. It reads an
enriched master CSV, reads a curated playlist map JSON, and adds or updates a
`Playlists` column. It does not call Spotify and it does not change raw
Discogs `Style` or `Genre` values.

Run it after enrichment:

```bash
python3 scripts/discogs_playlist_mapper.py
```

By default it uses:

```text
--input collection/enriched-collection.csv
--output collection/enriched-collection.csv
--config config/playlist-map.json
```

The mapper writes back to the input master by default. Pass `--output` when you
want to review the mapped CSV before replacing the master.

The mapper also writes a report by default:

```text
reports/YYYY-MM-DD_HH-MM-SS_discogs_playlist_mapper.txt
```

The report groups the run into summary, file path, and playlist association
sections. Each association shows the `release_id`, artist, title, and playlist
names written for that row. Rows with no mapped playlist are listed as `None`.
After a successful run, the mapper prints the same playlist association section
to the console after the output paths and row counts.

If the config file does not exist, the mapper creates an empty config with the
expected fields, prints what each field is for, and waits for you to press Enter
after filling it in. If stdin is not available, the mapper creates the config
and stops so you can fill it in before running again.

The mapper preserves existing rows, row order, and custom columns. It writes
`release_id` as the first column when that column exists. If the CSV already has
`Playlists`, that column is updated in place. Otherwise `Playlists` is inserted
after `Genre` when present, after `Style` when only `Style` is present, or at
the end as a fallback.

To inspect the playlist config without mapping any rows, run:

```bash
python3 scripts/discogs_playlist_config_printer.py
```

The printer uses `config/playlist-map.json` by default. If the file is missing,
it creates a blank config and prints it. It does not read or write the collection
CSV.

The config shape is:

```json
{
  "excluded_terms": ["Electronic", "Electro"],
  "playlists": {
    "Bossanova": ["Bossa Nova", "Bossanova"],
    "Breakbeat": ["Breakbeat", "Breaks"],
    "House": ["House", "Deep House", "Acid House"]
  }
}
```

Local playlist names come directly from the canonical labels under `playlists`.
With the config above, `House` stays `House`. Publisher-specific prefixes and
suffixes belong in `config/publisher.json`. Do not put `playlist_prefix` in
`config/playlist-map.json`.

`excluded_terms` are cleanup rules for raw Discogs terms that should never create
playlist labels. They are exact term rules with case-insensitive matching.

`playlists` maps each canonical playlist label to the raw Discogs style or genre
aliases that should create that playlist. The canonical label casing is kept in
the output.

Mapping rules:

- `Style` and `Genre` are parsed as comma-separated term lists.
- Matching is case-insensitive and trims whitespace.
- `Style` is checked first.
- If `Style` creates one or more playlists, `Genre` is ignored for that row.
- If `Style` creates no playlist, `Genre` is used as the fallback.
- One row can receive multiple playlists.
- The same raw Discogs term can appear under multiple playlist labels; matching
  rows receive each label.
- Duplicate source terms do not duplicate playlist names.
- Playlist order follows the order of `playlists` in the config.
- Rows with no matching term get a blank `Playlists` value.

The mapper validates the config before writing output. It rejects malformed JSON,
missing or invalid `playlists`, invalid `excluded_terms`, empty playlist labels,
empty aliases, non-string aliases, and aliases that appear in both
`excluded_terms` and `playlists`.

## TuneMyMusic playlist exporter

`scripts/discogs_playlist_exporter.py` reads a playlist-mapped enriched master
CSV and writes one TuneMyMusic-style master CSV per playlist. It does not call
Spotify, does not need Spotify credentials, and does not write to any external
service.

Run it after the playlist mapper:

```bash
python3 scripts/discogs_playlist_exporter.py
```

By default it uses:

```text
--input collection/enriched-collection.csv
--output-dir collection/playlists
--cache collection/cache/playlist-tracks.cache.json
```

Each playlist in the `Playlists` column gets its own folder and master CSV:

```text
collection/playlists/<playlist-name>/<playlist-name>.csv
collection/playlists/Techno/Techno.csv
```

Playlist folder and master file names come from the playlist labels. Characters
that are not safe in file names are replaced with `_`, repeated whitespace is
collapsed, an empty label becomes `playlist`, and case-insensitive folder name
collisions get a suffix such as ` (2)`.

The exporter writes a small TuneMyMusic import CSV:

```text
Release Id, Album Name, Track Number, Track Name, Artist Name,
Spotify Search Query
```

Despite the `Spotify Search Query` column name, the script only builds local
search text from artist, track, and album names. It does not call Spotify.
`Release Id` keeps each playlist row linked back to the enriched master CSV.

For each release row with a playlist, the exporter uses `release_id` to fetch the
Discogs release tracklist, caches the parsed tracklist, and writes one output row
per track. If a release belongs to more than one playlist, its tracks are written
to each matching playlist CSV. `Track Number` is the track order inside the
release export, starting at `1`. It is not the raw Discogs track position.

Discogs tracklists can contain headings, indexes, and nested `sub_tracks`. The
exporter flattens usable `sub_tracks`, skips non-track headings, and uses a
track-level artist when Discogs supplies one. Otherwise it falls back to the
release artist.

Rows with no playlist are skipped. Rows with a playlist but no `release_id`, a
failed lookup, or an empty Discogs tracklist are still exported as one
release-level fallback row, using the release title as the track name. The report
lists those fallback rows so you can review them before importing. Rows without
`release_id` are not sent to Discogs.

When run in an interactive terminal, the exporter shows row progress on `stderr`
with a same-line progress bar, row count, and percentage. Progress counts input
rows as the exporter scans them, including rows skipped because they have no
playlist. You can turn it off with `--no-progress`.

The report defaults to:

```text
reports/YYYY-MM-DD_HH-MM-SS_discogs_playlist_exporter.txt
```

The report contains summary counts, output file paths, playlist CSV row counts,
release changes by playlist, and review notes for uncertain rows. The terminal
summary prints the same release changes.

The exporter rewrites playlist master CSVs from the current mapped master each
time it runs. Before rewriting a playlist master, it compares the new rows with
the previous TuneMyMusic CSV in that playlist folder. Release changes appear
once per release, even when a release exports multiple track rows. For rows
without `Release Id`, the comparison falls back to artist and album names.

If an old playlist folder is no longer generated, the report lists its master
CSV releases as removed from the current export, but leaves the folder and file
in place. If a previous CSV cannot be read, or if it is missing the current
TuneMyMusic columns, the exporter skips the release-change comparison for that
file and writes a review note.

The exporter uses the same Discogs token environment variable as the enrichment
script:

```bash
export DISCOGS_TOKEN="your-token"
```

It can run without a token, but a token usually gives better Discogs API rate
limits. Cached releases are not fetched again.

The playlist tracklist cache defaults to:

```text
collection/cache/playlist-tracks.cache.json
```

It stores parsed tracklist data by `release_id`, including empty tracklists and
notes. It does not store raw Discogs API payloads. If the cache file has an
unsupported schema, the exporter stops and asks you to delete the old cache or
choose a new `--cache` path.

## Playlist splitter

`scripts/discogs_playlist_splitter.py` reads the playlist master CSVs under
`collection/playlists` and writes split CSVs in each playlist folder. Each split
uses the same TuneMyMusic columns as the playlist master.

Run it after the playlist exporter:

```bash
python3 scripts/discogs_playlist_splitter.py
```

For a playlist master like:

```text
collection/playlists/Techno/Techno.csv
```

the splitter writes files like:

```text
collection/playlists/Techno/splits/1-500.csv
collection/playlists/Techno/splits/501-932.csv
```

The file names use actual row numbers from the playlist master. By default,
existing split CSVs are treated as frozen. The splitter preserves them, writes
new split files for new release rows after the highest existing range, and warns
when an existing split's contents no longer match the current master rows for
that range. It does not rewrite mismatched frozen splits unless you ask for
regeneration.

Release rows are kept together when they fit within the configured row limit. By
default that limit is 500 rows. If one release has more rows than the configured
limit, that release can be split across files and the report includes a warning.

The splitter reads `config/workflow.json` by default and creates it with these
settings if it does not exist:

```json
{
  "max_rows_per_split": 500,
  "keep_release_tracks_together": true,
  "create_new_split_files_for_new_releases": true
}
```

`max_rows_per_split` sets the default split row limit. `--max-rows` overrides it
for one run.

When `keep_release_tracks_together` is `true`, the splitter keeps rows from the
same `Release Id` in the same split file when they fit within the row limit. Set
it to `false` to fill each split file up to the row limit, even if that splits a
release across files.

When `create_new_split_files_for_new_releases` is `true`, stable update mode
keeps existing split files unchanged and writes new split files after the highest
existing range. Set it to `false` to append new rows into the latest split file
until it reaches the row limit, then create later split files as needed. In that
mode, the latest split must still match the current master rows for its range. If
it does not, the splitter fails instead of rewriting possible manual edits or
stale data.

Unknown keys in `workflow.json` are rejected so spelling mistakes do not get
ignored.

To regenerate split files, pass a target:

```bash
python3 scripts/discogs_playlist_splitter.py --regenerate all
python3 scripts/discogs_playlist_splitter.py --regenerate Techno
python3 scripts/discogs_playlist_splitter.py --regenerate collection/playlists/Techno
python3 scripts/discogs_playlist_splitter.py --regenerate collection/playlists/Techno/Techno.csv
```

`all` regenerates every playlist folder with a master CSV. A named target can
match the folder name or the display playlist name after safe file-name cleanup.
A path target must point inside the playlist output directory and can point to a
playlist folder or its master CSV.

## Creating playlists with TuneMyMusic

The exporter and splitter only write local CSV files. To create the actual
playlist in Spotify, Apple Music, YouTube Music, TIDAL, or another destination
service, use TuneMyMusic's web transfer flow:

```text
https://www.tunemymusic.com/transfer
```

TuneMyMusic's [public transfer docs](https://www.tunemymusic.com/features/transfer)
list file upload as a supported source, with CSV among the supported file
formats. They also say transfers match tracks from playlist data such as title,
artist, album, and unique identifiers when present. The generated playlist CSV
includes `Track Name`, `Artist Name`, and `Album Name`, plus the local
`Release Id` audit column.

Use this flow for each split CSV:

1. Open `https://www.tunemymusic.com/transfer`.
2. Choose `Upload file` as the source.
3. Upload one file from a playlist `splits` folder, for example
   `collection/playlists/House/splits/1-500.csv`.
4. If TuneMyMusic shows a track review step, check that the track names and
   artist names look right before continuing.
5. Choose the destination music service and authorize TuneMyMusic for the
   account where the playlist should be created.
6. Choose whether to create a new playlist or add tracks to an existing one. For
   a new playlist, use the playlist folder name or the playlist label from the
   `Playlists` column.
7. Start the transfer.
8. At the end, review TuneMyMusic's missing-track list. Download its missing
   tracks CSV when available, then compare it with this tool's playlist export
   report to decide which tracks need manual cleanup.

Upload one split CSV at a time. For playlists with multiple split files, create
the playlist from the first split, then add the later splits to the same
destination playlist. TuneMyMusic controls destination-service authorization,
file-size limits, free-plan limits, matching behavior, and missing-track
reporting, so check its [FAQ](https://www.tunemymusic.com/help?faq=1) or plan
page if a large playlist is rejected or the site asks you to upgrade.

## How rows are merged

The script reads both files:

- the selected export is the new Discogs collection export.
- `--master` is the existing enriched CSV. It defaults to
  `collection/enriched-collection.csv` and is created if missing.

Rows are matched by `release_id`. Rows from the new export with an existing
`release_id` refresh that master row's export fields in place. Rows with a new
`release_id` are appended. Rows from the new export with a missing `release_id`
are skipped and listed in the report. Duplicate `release_id` values within the
same export are also skipped and reported after the first one is used.

Enrichment columns are not replaced during the merge, so existing `Style`,
`Genre`, notes, and timestamp values are preserved unless later enrichment work
changes them under the normal refresh rules.

Custom columns that already exist in the master are preserved. New export fields
are also kept in the output.

CSV and JSON outputs are written through a temporary file in the destination
folder and then replaced into place. That keeps normal successful writes from
partially overwriting the master file.

## How metadata is looked up

For each row that needs a style or genre, the script uses this order:

1. Discogs release API:

   ```text
   https://api.discogs.com/releases/<release_id>
   ```

2. Discogs master API, only when the release API returns a `master_id` and at
   least one requested field is still missing:

   ```text
   https://api.discogs.com/masters/<master_id>
   ```

The release endpoint is fetched once per uncached `release_id`. If the master
endpoint is needed, it is also fetched once and shared by style and genre
resolution.

If neither API response includes explicit style or genre data, the script leaves
the missing field blank and adds the release to the report.

The tool does not guess styles or genres from artist, title, label, format,
notes, or other collection fields.

## Authentication and rate limits

The script can run without a Discogs token, but a token usually gives better API
rate limits. You can set it in the environment:

```bash
export DISCOGS_TOKEN="your-token"
```

or pass it directly:

```bash
python3 scripts/discogs_style_enricher.py \
  --export /path/to/new-discogs-export.csv \
  --discogs-token "your-token"
```

By default, uncached lookups use Discogs response headers to stay under the
reported rate limit. Before the first response, the script starts from Discogs'
published-style limits: roughly 25 requests per minute without a token and 60
requests per minute with a token. It keeps a small safety margin so a run is
less likely to hit `429` responses.

Set a larger minimum delay when you want to be more conservative:

```bash
--request-interval-seconds 1.5
```

Uncached release lookups can run in parallel. The default is three workers:

```bash
--max-workers 3
```

Parallel lookup does not change CSV row order. The script fetches metadata in
worker threads, then applies the results back to rows in master CSV order before
writing the output file.

The default HTTP timeout is `30` seconds per request:

```bash
--timeout-seconds 45
```

The script retries failed HTTP requests up to three times. It waits longer after
server errors. For Discogs `429` rate limit responses, it uses `Retry-After`
when Discogs sends it and otherwise waits 65 seconds. Client errors that are not
rate limits stop retrying for that URL.

## Cache and report files

Unless you pass custom paths, the script writes the lookup cache under a
`cache` folder beside the output CSV and writes the report in `reports`.
With the default master path, the lookup cache is `collection/cache/processing.cache.json`.

- `cache/processing.cache.json` under the output CSV folder
- `reports/YYYY-MM-DD_HH-MM-SS_discogs_style_enricher.txt`

The cache stores release metadata lookup results by `release_id`, including
blanks and errors. This makes later runs faster and avoids asking Discogs for the
same release over and over. Cached error entries are retried on later runs.
Cached blank entries are reused because they record that Discogs did not provide
explicit metadata.

Each cache entry keeps style and genre values, lookup source, lookup status,
notes, master ID, and lookup time. The source is one of the lookup paths such as
`api_release` or `api_master`. The status is `filled`, `blank`, or `error`.
The cache uses a schema-versioned JSON structure and does not store raw Discogs
API payloads. Old flat style-only caches are not migrated; use the new default
cache path or delete the old cache if you explicitly point `--cache` at it.

The report is a plain text file with summary, file path, seen-terms, and manual
review sections. It summarizes the run:

- input export row count
- master row count before the run
- output row count
- appended row count
- filled style count
- filled genre count
- preserved style count
- preserved genre count
- blank or not sure count
- lookup error count
- output and cache paths
- refreshed existing release rows
- skipped export rows with missing or duplicate `release_id`
- release IDs left blank or not sure, with artist, title, which metadata fields
  are missing, and style or genre notes when present

## Seen Discogs terms

The enrichment script also keeps a local seen-terms sidecar file:

```text
collection/cache/collected.cache.json
```

This file records raw Discogs style and genre terms that have already appeared in
your enriched master. It is a term registry, not playlist config. It does not
store release IDs, collection history, playlist labels, or rules.

The default schema is:

```json
{
  "schema_version": 1,
  "record_type": "discogs_seen_terms",
  "styles": ["Deep House", "Disco", "House"],
  "genres": ["Electronic", "Funk / Soul"]
}
```

After each enrichment run, the script collects comma-separated terms from the
current output rows, compares them with the seen-terms file, writes newly seen
styles and genres into the report, then updates the file with the union of old
and current terms. Stored terms are sorted alphabetically so diffs stay stable.

On the first run, if the seen-terms file does not exist, the script creates it
from the current output terms, reports that the snapshot was initialized, and
lists every current term as new. Later runs report only terms that were not in
that saved snapshot.

If the seen-terms file exists but is malformed, the script fails clearly before
writing the enriched CSV or updating the sidecar. It does not reset the file.

Report examples:

```text
New Discogs terms since last seen-terms snapshot
------------------------------------------------
Styles:
- Acid Jazz
- Breaks

Genres:
- None
```

or, on first initialization:

```text
Seen terms snapshot
-------------------
- Initialized: collection/cache/collected.cache.json
- Styles tracked: 42
- Genres tracked: 8

New Discogs terms since last seen-terms snapshot
------------------------------------------------
Styles:
- Acid Jazz
- Breaks

Genres:
- Electronic
```

Use `--seen-terms PATH` to store this sidecar somewhere else. Use
`--no-seen-terms` for a run that should not read or update the sidecar.

New terms in the enrichment report are review prompts. Add a term to
`config/playlist-map.json` only when you want that raw Discogs term to create a
playlist label. Leaving a term only in `collection/cache/collected.cache.json`
just prevents repeated alerts for a term you have already reviewed.

## Refreshing existing metadata

By default, existing `Style` and `Genre` values are treated as user-approved data
and are not looked up again.

Use `--refresh-existing` when you want the script to replace existing style and
genre values:

```bash
python3 scripts/discogs_style_enricher.py --refresh-existing
```

This still uses the lookup cache. If the cache contains non-error results, those
cached results are reused.

## Exit codes

Each script prints a run summary at the end.

Handled runtime errors print a single `Error: ...` line to `stderr` and return
`1`. This includes file, directory, CSV, JSON, config, cache, validation, and
supported Spotify API failures for the scripts that can hit those cases.

- `discogs_style_enricher.py` returns `0` when the run finishes without lookup
  errors.
- `discogs_style_enricher.py` returns `2` when the run finishes, but at least
  one lookup ended with an error status.
- `discogs_style_enricher.py` returns `1` for handled file, directory, processed
  export collision, and validation errors.
- `discogs_playlist_mapper.py`, `discogs_playlist_exporter.py`, and
  `discogs_playlist_splitter.py` return `0` on success and `1` for handled file,
  config, cache, and input validation errors.
- `discogs_make_playlists.py` returns `0` when all configured workflow steps
  return `0`. Otherwise it returns the first nonzero step exit code and skips
  later steps.
- `dedupe_playlists.py` returns `0` when planning or apply finishes, and `1` for
  handled file, config, Spotify API, and safety validation errors.

Rows with blank lookup status are not treated as command failures. They mean the
script could not find explicit style or genre data and chose not to guess.
Argument parsing errors use Python `argparse` behavior, which prints usage text
and exits before the script's run summary.

## Full command reference

Combined workflow options:

```text
--export PATH
    Specific Discogs collection export CSV passed to the enricher.

--input-dir PATH
    Folder containing one Discogs export CSV passed to the enricher.

--processed-dir PATH
    Folder where default-folder exports are moved after enrichment.

--master PATH
    Enriched master CSV used by the enrichment, mapping, and export steps. When
    set, the mapper reads from and writes to this path, and the exporter reads
    from this path.

--config PATH
    Playlist map JSON passed to the mapper.

--workflow-config PATH
    Workflow JSON config passed to the splitter. Defaults to
    config/workflow.json when omitted by the splitter.

--playlist-output-dir PATH
    Directory for per-playlist folders, playlist master CSVs, and split CSVs.

--enrichment-cache PATH
    Discogs style and genre lookup cache JSON passed to the enricher as
    --cache.

--tracklist-cache PATH
    Discogs tracklist lookup cache JSON passed to the exporter as --cache.

--enrichment-report PATH
    Enrichment report path.

--mapping-report PATH
    Playlist mapping report path.

--playlist-report PATH
    Playlist export report path.

--split-report PATH
    Playlist split report path.

--regenerate-splits TARGET
    Playlist folder/display name/path to regenerate, or all, passed to the
    splitter.

--publisher-config PATH
    Publisher JSON config. Defaults to config/publisher.json.

--publisher spotify|none
    Publisher override for the workflow. If omitted, the workflow uses
    default_publisher from the publisher config. The default config resolves to
    none, so the final workflow step prints a skip notice. When resolved to
    spotify, the final workflow step runs the Spotify publisher after split CSVs
    are written.

--skip-publish-playlist
    Skip the final playlist publisher for this run. This is equivalent to
    --publisher none and cannot be combined with --publisher.

--publishing-dry-run
    Preview playlist publishing without creating or updating Spotify playlists.
    This only affects runs where the resolved publisher is spotify. When omitted,
    the resolved Spotify publisher can write changes.

--max-new-searches-per-run COUNT
    Maximum uncached Spotify searches per publisher run. Defaults to the
    Spotify publisher's 500-search cap. Use 0 for an uncapped publisher run.

--max-rows COUNT
    Maximum rows per split CSV, overriding workflow config.

--refresh-existing
    Ask the enricher to replace existing Style and Genre values.

--no-seen-terms
    Disable seen Discogs terms tracking in the enricher.

--no-progress
    Disable progress output in enrichment, playlist export, and Spotify
    publishing.

--timeout-seconds SECONDS
    HTTP timeout per Discogs request for enrichment and playlist export.

--request-interval-seconds SECONDS
    Minimum delay between Discogs requests for enrichment and playlist export.

--max-workers COUNT
    Maximum concurrent uncached enrichment lookups.
```

Style enricher options:

```text
--export PATH
    Specific Discogs collection export CSV. If omitted, the script reads exactly
    one CSV from --input-dir. Explicit --export runs do not move the export to
    --processed-dir.

--input-dir PATH
    Folder containing one new Discogs export CSV. Defaults to export.

--processed-dir PATH
    Folder where default-folder exports are moved after successful runs.
    Defaults to processed.

--master PATH
    Existing enriched master CSV. Created if missing. Defaults to
    collection/enriched-collection.csv.

--output PATH
    Output enriched master CSV. Defaults to --master.

--report PATH
    Text report path. Defaults to
    reports/YYYY-MM-DD_HH-MM-SS_discogs_style_enricher.txt.

--cache PATH
    Lookup cache JSON path. Defaults to cache/processing.cache.json under the
    output CSV folder.

--seen-terms PATH
    Seen Discogs terms JSON path. Defaults to collection/cache/collected.cache.json.

--no-seen-terms
    Disable seen Discogs terms tracking for this run.

--discogs-token TOKEN
    Optional Discogs personal access token. Defaults to DISCOGS_TOKEN.

--user-agent TEXT
    User-Agent sent to Discogs.

--timeout-seconds SECONDS
    HTTP timeout per request. Defaults to 30.

--request-interval-seconds SECONDS
    Minimum delay between Discogs requests. Defaults to header-aware throttling
    with no extra fixed delay.

--max-workers COUNT
    Maximum concurrent uncached Discogs lookups. Defaults to 3.

--refresh-existing
    Replace existing Style and Genre values instead of preserving them.

--no-progress
    Disable the interactive terminal progress bar.
```

Playlist mapper options:

```text
--input PATH
    Enriched Discogs master CSV. Defaults to collection/enriched-collection.csv.

--output PATH
    Output CSV. Defaults to --input.

--config PATH
    Playlist map JSON. Defaults to config/playlist-map.json.

--report PATH
    Text report path. Defaults to
    reports/YYYY-MM-DD_HH-MM-SS_discogs_playlist_mapper.txt.
```

TuneMyMusic playlist exporter options:

```text
--input PATH
    Playlist-mapped enriched master CSV. Defaults to
    collection/enriched-collection.csv.

--output-dir PATH
    Directory for per-playlist folders and master CSVs. Defaults to
    collection/playlists.

--report PATH
    Text report path. Defaults to
    reports/YYYY-MM-DD_HH-MM-SS_discogs_playlist_exporter.txt.

--cache PATH
    Discogs tracklist cache JSON. Defaults to
    collection/cache/playlist-tracks.cache.json.

--discogs-token TOKEN
    Optional Discogs personal access token. Defaults to DISCOGS_TOKEN.

--user-agent TEXT
    User-Agent sent to Discogs.

--timeout-seconds SECONDS
    HTTP timeout per request. Defaults to 30.

--request-interval-seconds SECONDS
    Minimum delay between Discogs requests. Defaults to header-aware throttling
    with no extra fixed delay.

--no-progress
    Disable the interactive terminal progress bar.
```

Release playlist options:

```text
release_ids
    Discogs release IDs to export. Values must be positive integers. You can
    also pass --release-ids-file.

--name TEXT
    Required playlist name. The same name updates the same on-the-fly master CSV.
    The file path may be sanitized, but the publisher target keeps this exact
    name.

--release-ids-file PATH
    Text file containing release IDs separated by whitespace or commas. IDs from
    the file are appended after IDs passed as arguments, then deduped in order.

--output-dir PATH
    Directory for on-the-fly playlist folders. Defaults to
    collection/playlists/on-the-fly.

--report PATH
    Release playlist report path. Defaults to
    reports/YYYY-MM-DD_HH-MM-SS_discogs_release_playlist.txt.

--tracklist-cache PATH
    Discogs tracklist cache JSON. Defaults to
    collection/cache/playlist-tracks.cache.json.

--publisher-config PATH
    Publisher JSON config. Defaults to config/publisher.json.

--publisher spotify|none
    Publisher override. If omitted, the script reads default_publisher from the
    publisher config. Use none to write the CSV and report without publishing.

--publisher-report PATH
    Spotify publisher report path. Defaults to
    reports/YYYY-MM-DD_HH-MM-SS_publish_playlist.txt.

--publishing-dry-run
    Preview Spotify playlist changes without creating or updating playlists.

--publisher-sync-mode append|replace
    Spotify sync mode. Defaults to append. This only affects Spotify publishing.

--refresh-match-cache
    Recheck every generated playlist row with Spotify and update the local track
    match cache. Use with --publishing-dry-run to refresh local decisions without
    writing playlist changes to Spotify.

--match-cache PATH
    Spotify track match cache path. Defaults to
    collection/cache/spotify-track-matches.cache.json.

--publish-state-cache PATH
    Spotify publish-state cache path. Defaults to
    collection/cache/spotify-publish-state.cache.json.

--env-file PATH
    Local env file containing Spotify app settings. Defaults to .env.

--token-cache PATH
    Spotify token cache path. Defaults to config/cache/spotify-token.cache.json.

--reauthorize
    Force a fresh Spotify login before running the publisher.

--search-limit COUNT
    Spotify search result limit per track. Defaults to 10.

--max-new-searches-per-run COUNT
    Maximum uncached Spotify searches per publisher run. Defaults to 500. Use 0
    for an uncapped run. Lower this value if Spotify throttles before the
    default cap.

--discogs-token TOKEN
    Optional Discogs personal access token. Defaults to DISCOGS_TOKEN.

--user-agent TEXT
    User-Agent sent to Discogs.

--timeout-seconds SECONDS
    HTTP timeout per Discogs request. Defaults to 30.

--request-interval-seconds SECONDS
    Minimum delay between Discogs requests. Defaults to header-aware throttling
    with no extra fixed delay.

--debug-log PATH
    Write sanitized release playlist debug logs to this path.

--no-progress
    Disable the interactive terminal progress bar.
```

Playlist dedupe options:

```text
--provider spotify
    Provider to dedupe. Defaults to spotify.

--apply
    Remove planned duplicate tracks from eligible provider playlists. Omit this
    flag for a dry run.

--playlists VALUE [VALUE ...]
    Dedupe one or more eligible playlists. Each value can be the local playlist
    label, Spotify target name, or Spotify playlist ID. Omit this flag to process
    every eligible playlist. Use --playlists for one selector too; --playlist is
    not supported.

--report PATH
    Dedupe report path. Defaults to
    reports/YYYY-MM-DD_HH-MM-SS_dedupe.txt.

--publisher-config PATH
    Publisher JSON config. Defaults to config/publisher.json.

--env-file PATH
    Local env file containing Spotify app settings. Defaults to .env.

--token-cache PATH
    Spotify token cache path. Defaults to config/cache/spotify-token.cache.json.

--reauthorize
    Force a fresh Spotify login before running dedupe.

--debug-log PATH
    Write sanitized playlist dedupe debug logs to this path.

--no-progress
    Disable the interactive terminal progress bar.
```

Playlist splitter options:

```text
--output-dir PATH
    Directory containing playlist folders. Defaults to collection/playlists.

--report PATH
    Text report path. Defaults to
    reports/YYYY-MM-DD_HH-MM-SS_discogs_playlist_splitter.txt.

--regenerate [TARGET]
    Regenerate split CSVs for a playlist folder/display name/path or all. If
    the option is present without a target, it regenerates all playlists. If
    omitted, the splitter uses stable update mode for all playlists.

--workflow-config PATH
    Workflow JSON config. Defaults to config/workflow.json, created with default
    values if missing.

--max-rows COUNT
    Maximum rows per split CSV. Overrides workflow config for one run.
```

Playlist config printer options:

```text
--config PATH
    Playlist map JSON. Defaults to config/playlist-map.json.
```

## Requirements

The tool uses only the Python standard library. Run it with Python 3.

The input CSV must use the standard Discogs collection export header. The script
requires these columns:

```text
Catalog#, Artist, Title, Label, Format, Rating, Released, release_id,
CollectionFolder, Date Added, Collection Media Condition,
Collection Sleeve Condition, Collection Notes
```

Extra columns are allowed. Duplicate headers are rejected. During an incoming
export merge, rows without a `release_id` value are skipped and listed in the
report. In an existing master row, missing `release_id` still prevents Discogs
metadata lookup for that row; missing style or genre fields are left blank and
marked with `missing release_id` notes.

## Tests

Install the locked development tooling after cloning the repository or when
`package-lock.json` changes:

```bash
npm ci --ignore-scripts
```

Run the Pylance-compatible type check and the unit tests with:

```bash
npm run typecheck
python3 -m unittest discover -s tests
```

Pylance and the command-line check both read `pyrightconfig.json`. The Pyright
version and its development dependencies are locked in `package-lock.json`, so
local checks and CI use the same checker. Pyright is a development tool; the
collection scripts still use only the Python standard library at runtime.

## Pre-commit hook

The repo has a tracked hook at `.githooks/pre-commit`. This checkout is
configured to use it with:

```bash
git config core.hooksPath .githooks
```

Before each commit, the hook checks staged project text files for trailing
whitespace and missing final newlines, runs Python syntax and type checks, and
runs the unit tests. Run `npm ci --ignore-scripts` first if the hook reports that
Pyright is not installed. The hook skips private workflow data folders such as
`collection`, `config`, `export`, `processed`, and `reports`.
