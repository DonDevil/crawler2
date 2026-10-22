# Analytics layer (operator guide)

P13. Design: [../phases/p13-analytics/design.md](../phases/p13-analytics/design.md).
Status: implemented and tested on synthetic fixtures and a throwaway Scylla
keyspace; **no real-data export has been run yet** (exit.md). Offline only:
nothing in the crawler, extraction, media, matching, evidence or feedback
paths calls it, and it never writes to Scylla, Redis or MinIO.

## What it does

crawler2 keyspace → read-only token-range scan → validated, revisioned
Parquet export → DuckDB (ad-hoc SQL, standard campaign/source reports) →
offline graph and site/template clustering (unreviewed P7 bootstrap
candidates).

## Setup

DuckDB is a regular dependency (`make install`). Nothing to migrate. Settings
`CRAWLER2_ANALYTICS__*`:

| Setting | Default | Meaning |
|---|---|---|
| `ROOT` | `var/analytics` | every output; git-ignored under `var/` (also `--root`) |
| `SEGMENTS` | 64 | token-range segments per table (checkpoint/retry unit) |
| `MAX_RETRIES` / `RETRY_BACKOFF_S` | 3 / 2.0 | per-segment retries on storage unavailability |
| `DUCKDB_MEMORY_LIMIT` / `DUCKDB_THREADS` | `1GB` / 2 | build memory bound (spills to the run's temp dir) |

The keyspace and contact points are the usual `CRAWLER2_SCYLLA__*`; from the
host use the Scylla container IP (`docker inspect … crawler2-scylla-1`). On the
dev laptop run large exports as a memory-capped user unit (oomd):

```bash
systemd-run --user --unit=p13-export --collect -p MemoryMax=4G -p MemoryHigh=3G \
  -p MemorySwapMax=0 -p IOWeight=50 --working-directory="$PWD" \
  --setenv=CRAWLER2_SCYLLA__CONTACT_POINTS=<scylla-ip> \
  /bin/sh -c 'env/bin/crawler2-analytics export --keyspace crawler2 > var/p13-export.log 2>&1'
```

## Export

```bash
crawler2-analytics datasets                                   # catalog + excluded tables
crawler2-analytics export --keyspace crawler2 \
    --until 2026-10-06T00:00:00Z [--since 2026-10-01T00:00:00Z] \
    [--dataset page_observations --dataset urls] [--segments 64] [--verify-counts]
```

- Prints `outcome` (`published` | `unchanged`), `run_id`, `export_key`,
  `revision`, `path`, `content_id` and per-dataset status/rows/rejected.
- `--until` is the exclusive boundary (default: now — every run then gets its
  own key). For a repeatable export always pass `--until`.
- The cost is a full scan of every selected table, whatever the window.
- Exit 4 = failed, nothing published.

Layout: `<root>/exports/<export_key>/rev-NNNN/<dataset>/schema_version=1/date=YYYY-MM-DD/part-K.parquet`,
`_manifest.json` per revision (scope, boundary, counts, file SHA-256s),
`CURRENT` → newest revision, `<root>/runs/<run_id>.json` per run.

### Safe re-runs

- Same scope, same `--until`, unchanged source → `unchanged`, nothing written.
- Same scope, source changed (late writes, TTL expiry) → `rev-(n+1)`;
  earlier revisions stay untouched and readable.
- Different window or dataset set → a different export key.

### Validate

```bash
crawler2-analytics validate <export_key> [--revision N] [--quick]   # exit 2 if not ok
crawler2-analytics status                                           # exports, revisions, unpublished runs
```

### Recovery after a failed export

1. `crawler2-analytics status` lists the run under `unpublished_runs` with
   its error (`runs/<run_id>.json` has it too). Nothing was published.
2. Fix the cause (Scylla down, disk full, lock held …), then
   `crawler2-analytics export --resume <run_id>` — completed segments are
   reused; the scope comes from the run.
3. A validation failure (`key conflicts`, read-back mismatch) is a defect or
   a source anomaly: inspect `_staging/<run_id>/`, do not resume blindly.
4. `.publish.lock` left by a killed process: remove it only after checking
   no export of that key is running.
5. `crawler2-analytics gc [--older-than-hours 24] [--force] [--dry-run]`
   deletes failed (and with `--force` abandoned "running") staging runs.
   It never deletes a published revision; remove old revisions by hand
   (never the one `CURRENT` names).

## Query with DuckDB

```bash
crawler2-analytics sql <export_key> "SELECT site, count(*) FROM page_observations GROUP BY 1"
crawler2-analytics sql <export_key> --file crawler2/analytics/sql/site_overview.sql [--format json]
```

Views are named after the datasets and read exactly the manifest's files.
Shipped queries: `site_overview.sql`, `fetch_outcomes_by_day.sql`,
`matched_content_pages.sql`, `data_quality.sql`. Outside the CLI (e.g. the
`duckdb` shell), read files with `read_parquet('…/**/*.parquet',
hive_partitioning = false)` — the `date` column is stored in the files.

## Reports

```bash
crawler2-analytics report campaign <export_key> --campaign film-x [--format markdown] [--out f] [--verify] [--require-complete]
crawler2-analytics report source   <export_key> [--site example.com] [--format markdown]
```

Every report names the export revision and content id it used, defines each
metric, gives rates with numerator/denominator, and says `complete: false`
with reasons when a dataset is missing (exit 3 with `--require-complete`).
`report_digest` is identical for identical inputs.

## Graph

```bash
crawler2-analytics graph build <export_key> [--replace]
```

Writes `<root>/derived/<key>/rev-NNNN/graph-v1/{nodes,edges}.parquet` +
manifest. Rebuilding the same revision is `unchanged`. Edge semantics: design
§9 — co-occurrence only, never ownership.

## Clustering prototype

```bash
crawler2-analytics cluster run <export_key> [--min-pages 3] [--min-similarity 0.6] [--min-samples 3] [--max-sites 2000]
```

Writes `<root>/derived/<key>/rev-NNNN/cluster-v1-<params>/`: site features,
assignments (`assigned` | `noise` | `insufficient_data`), clusters, template
groups and `bootstrap-candidates.json` (status `unreviewed`). Nothing is
imported into P7; review is manual (design §10.1).

## Retention

Exports copy URLs and page titles out of Scylla: treat `<root>` like the
keyspace (ADR-005 open). Nothing is deleted automatically.

## Tests

`scripts/test-analytics.sh` (unit + real-Scylla integration, keyspace
`crawler2_it_p13_host`); unit only: `env/bin/pytest tests/unit/analytics`.
