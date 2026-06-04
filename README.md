# Discogs metadata enricher

`scripts/discogs_style_enricher.py` enriches a Discogs collection export CSV with
explicit release style and genre data from Discogs. It reads the `release_id` in
each row, looks up the matching Discogs release, and writes the styles and
genres into `Style` and `Genre` columns.

The tool is built for repeat use. Keep one enriched CSV as your master file,
then merge each new Discogs collection export into that master. Existing rows
and already-filled style or genre values are preserved by default. New rows are
appended, and only missing metadata is looked up.

## What the tool changes

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
5. Keep `collection/enriched-collection.csv` as your master for the next run.

This workflow avoids redoing work. The script keeps a local JSON cache beside the
output CSV, so later runs can reuse earlier release lookups.

The `export` folder may contain non-CSV files, but it must contain exactly one
CSV. If it contains zero CSV files or more than one CSV file, the script stops
before doing any lookups.

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

Unless you pass custom paths, the script writes `processing.cache.json` beside
the output CSV and writes the report in `reports`:

- `processing.cache.json`
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

The report summarizes the run:

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
- release IDs left blank or not sure, with artist, title, style notes, and genre
  notes when present

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

The script prints a run summary at the end.

- Exit code `0` means the run finished without lookup errors.
- Exit code `2` means the run finished, but at least one lookup ended with
  an error status.

Rows with blank lookup status are not treated as command failures. They mean the
script could not find explicit style or genre data and chose not to guess.

## Full command reference

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
    Lookup cache JSON path. Defaults to processing.cache.json beside the output CSV.

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
