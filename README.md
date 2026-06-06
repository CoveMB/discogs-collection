# Discogs collection tools

This repo contains small Python CLIs for working with Discogs collection export
CSVs. The main workflow is:

1. Enrich a Discogs export with explicit `Style` and `Genre` metadata from the
   Discogs release API.
2. Map those Discogs terms to local playlist labels.
3. Export one TuneMyMusic-style CSV per playlist from Discogs tracklists.

The durable file is the enriched master CSV. By default that file is:

```text
collection/enriched-collection.csv
```

The scripts are local-first. They read and write CSV, JSON cache, and plain text
report files on disk. The enrichment and playlist exporter scripts can call
Discogs. They do not call Spotify, TuneMyMusic, or any other music service.

`scripts/discogs_style_enricher.py` reads the `release_id` in each row, looks up
the matching Discogs release, and writes the explicit Discogs styles and genres
into `Style` and `Genre` columns.

The tool is built for repeat use. Keep one enriched CSV as your master file,
then merge each new Discogs collection export into that master. Existing rows
and already-filled style or genre values are preserved by default. New rows are
appended, and only missing metadata is looked up.

## What the enricher changes

The script adds these columns when they are missing:

- `Style`
- `Genre`
- `Style Notes`
- `Genre Notes`
- `Updated At`

The enrichment columns are kept together. If the CSV already has `Style`, the
enrichment block is placed at that position. Otherwise the columns are added
after `Released` when that column is present.

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

This workflow avoids redoing work. The enrichment script keeps a local JSON
cache beside the output CSV, so later runs can reuse earlier release lookups.

The `export` folder may contain non-CSV files, but it must contain exactly one
CSV. If it contains zero CSV files or more than one CSV file, the script stops
before doing any lookups.

## One-command playlist workflow

To run enrichment, playlist mapping, and TuneMyMusic export one after the other,
use:

```bash
python3 scripts/discogs_make_playlists.py
```

With no options, the command uses the same defaults as the three individual
scripts:

- enrich from `export` into `collection/enriched-collection.csv`
- map playlists in `collection/enriched-collection.csv`
- export playlist CSVs into `collection/playlists`

The command stops before the next step when a step exits with a nonzero status.
For example, if enrichment reports lookup errors, mapping and playlist export do
not run. Check the enrichment report, then rerun after the issue is resolved.

The child enrichment and exporter scripts still read `DISCOGS_TOKEN` from the
environment. The combined command does not have a separate token option.

Common path overrides are available when you want the same master file or config
used across the full workflow:

```bash
python3 scripts/discogs_make_playlists.py \
  --export export/latest-discogs-export.csv \
  --master collection/enriched-collection.csv \
  --config config/playlist-map.json \
  --playlist-output-dir collection/playlists
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
reports/<output-name>_<YYYY-MM-DD_HH-MM-SS>_playlist_report.txt
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

The mapper preserves existing rows, row order, and custom columns. If the CSV
already has `Playlists`, that column is updated in place. Otherwise `Playlists`
is inserted after `Genre` when present, after `Style` when only `Style` is
present, or at the end as a fallback.

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
  "playlist_prefix": "Discogs - ",
  "excluded_terms": ["Electronic", "Electro"],
  "playlists": {
    "Bossanova": ["Bossa Nova", "Bossanova"],
    "Breakbeat": ["Breakbeat", "Breaks"],
    "House": ["House", "Deep House", "Acid House"]
  }
}
```

`playlist_prefix` is prepended to each output playlist name. With the config
above, `House` becomes `Discogs - House`.

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
CSV and writes one TuneMyMusic-style CSV per playlist. It does not call Spotify,
does not need Spotify credentials, and does not write to any external service.

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

Each playlist in the `Playlists` column gets its own file:

```text
collection/playlists/<playlist-name>.csv
```

Playlist file names come from the playlist labels. Characters that are not safe
in file names are replaced with `_`, repeated whitespace is collapsed, an empty
label becomes `playlist.csv`, and case-insensitive name collisions get a suffix
such as ` (2)`.

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
reports/playlists_<YYYY-MM-DD_HH-MM-SS>_playlist_export_report.txt
```

The report contains summary counts, output file paths, playlist CSV row counts,
release changes by playlist, and review notes for uncertain rows. The terminal
summary prints the same release changes.

The exporter rewrites playlist CSVs from the current mapped master each time it
runs. Before rewriting a playlist CSV, it compares the new rows with the
previous TuneMyMusic CSV in the output directory. Release changes appear once per
release, even when a release exports multiple track rows. For rows without
`Release Id`, the comparison falls back to artist and album names.

If an old playlist CSV is no longer generated, the report lists its releases as
removed from the current export, but leaves the file in place. If a previous CSV
cannot be read, or if it is missing the current TuneMyMusic columns, the exporter
skips the release-change comparison for that file and writes a review note.

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

## How rows are merged

The script reads both files:

- the selected export is the new Discogs collection export.
- `--master` is the existing enriched CSV. It defaults to
  `collection/enriched-collection.csv` and is created if missing.

Rows already present in the master are preserved. Rows from the new export are
appended only when their original export fields do not already match a master
row. Enrichment columns are ignored for this matching step, so existing
`Style`, `Genre`, notes, or timestamp values do not cause duplicates.

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
- `reports/<output-name>_<YYYY-MM-DD_HH-MM-SS>_report.txt`

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

- `discogs_style_enricher.py` returns `0` when the run finishes without lookup
  errors.
- `discogs_style_enricher.py` returns `2` when the run finishes, but at least
  one lookup ended with an error status.
- `discogs_style_enricher.py` returns `1` for handled file, directory, processed
  export collision, and validation errors.
- `discogs_playlist_mapper.py` and `discogs_playlist_exporter.py` return `0` on
  success and `1` for handled file, config, cache, and input validation errors.
- `discogs_make_playlists.py` returns `0` when all three steps return `0`.
  Otherwise it returns the first nonzero step exit code and skips later steps.

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
    Enriched master CSV used by all three steps. When set, the mapper reads
    from and writes to this path, and the exporter reads from this path.

--config PATH
    Playlist map JSON passed to the mapper.

--playlist-output-dir PATH
    Directory for per-playlist TuneMyMusic CSV files.

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

--refresh-existing
    Ask the enricher to replace existing Style and Genre values.

--no-seen-terms
    Disable seen Discogs terms tracking in the enricher.

--no-progress
    Disable progress output in enrichment and playlist export.

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
    Text report path. Defaults to reports/<output-name>_<timestamp>_report.txt.

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
    reports/<output-name>_<timestamp>_playlist_report.txt.
```

TuneMyMusic playlist exporter options:

```text
--input PATH
    Playlist-mapped enriched master CSV. Defaults to
    collection/enriched-collection.csv.

--output-dir PATH
    Directory for per-playlist CSVs. Defaults to collection/playlists.

--report PATH
    Text report path. Defaults to
    reports/playlists_<timestamp>_playlist_export_report.txt.

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

Extra columns are allowed. Duplicate headers are rejected. Rows without a
`release_id` value are left blank with `Style Notes` and `Genre Notes` set to
`missing release_id`.

## Tests

Run the unit tests with:

```bash
python3 -m unittest discover -s tests
```

## Pre-commit hook

The repo has a tracked hook at `.githooks/pre-commit`. This checkout is
configured to use it with:

```bash
git config core.hooksPath .githooks
```

Before each commit, the hook checks staged project text files for trailing
whitespace and missing final newlines, runs Python syntax linting with
`compileall`, and runs the unit tests. It skips private workflow data folders
such as `collection`, `config`, `export`, `processed`, and `reports`.
