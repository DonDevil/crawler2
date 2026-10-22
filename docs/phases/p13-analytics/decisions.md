# P13 decisions

Date: 2026-10-22. Engineering decisions taken without owner input (all
reversible), rejected shortcuts, and the owner decisions that remain open.

## 1. Engineering decisions

| # | Decision | Why / alternative |
|---|---|---|
| D-1 | **DuckDB is the only new dependency** (`duckdb>=1.5.6,<1.6`); it writes Parquet itself | pyarrow/pandas/polars would add ~100 MB for nothing DuckDB lacks; no Spark/Dask/graph DB/broker (single-host volumes, plan says DuckDB) |
| D-2 | Read Scylla with **full token-range scans** in N segments at LOCAL_QUORUM; time windows filter client-side | tables are keyed by id, not time; per-day indexes are TTL'd (30 d) derived copies and would silently miss older rows; no snapshot exists to use instead |
| D-3 | Partition by **UTC date** of each dataset's own event time; **domain is a sorted column, not a directory** | M1 has thousands of single-page external sites: `site=` directories would create thousands of tiny files; row-group statistics on the sorted `site` column give the pruning. Plan's "date/domain" is met as date partitions + domain clustering |
| D-4 | Outputs go to a configurable filesystem root (`CRAWLER2_ANALYTICS__ROOT`, default `var/analytics`), **not MinIO** | MinIO holds immutable evidence bytes behind content addressing; analytics revisions need atomic directory publication and DuckDB local reads; a shared filesystem path can be configured for multi-host |
| D-5 | Stage rows as gzip NDJSON per segment, build in a disk-backed DuckDB file | bounded memory, resumable per segment, explicit types at load (`read_json(columns=…)`) |
| D-6 | **Revisioned publication** under one export key; unchanged content ⇒ `unchanged`, changed ⇒ next immutable revision; `CURRENT` pointer | idempotent re-export without overwriting; changed sources never merged with old rows; reports can name the exact revision |
| D-7 | Exact duplicate rows are collapsed and counted; a key with differing content **fails the run** | a scan cannot legitimately return conflicting rows for one primary key; never pick one silently |
| D-8 | Malformed source rows are **rejected and counted** (with samples); the run still publishes | one corrupt document must not block analytics; it is visible in every manifest and report `data_quality` |
| D-9 | Rows with a null time column are kept in `date=unknown` | no silent loss; they cannot be placed in a window, so they are disclosed instead |
| D-10 | Order-independent XOR-of-MD5 content digest (DuckDB) + SHA-256 per file | cheap change detection over millions of rows without sorting; integrity from file hashes |
| D-11 | `page_links` / `page_media_refs` / `evidence_sources` keyed by (parent id, ordinal in the document) | faithful to the source document even when it repeats a target; W9 (`links_by_page_version`) not scanned a second time |
| D-12 | IDs exported in contract wire form | traceable to events and logs, self-describing |
| D-13 | Reports: JSON authoritative, Markdown rendering; no notebooks | the repo has no notebooks; versioned SQL files (`crawler2/analytics/sql/`) and the `sql` command cover exploration without a Jupyter dependency |
| D-14 | Missing scoping dataset ⇒ report fails; other missing datasets ⇒ `complete: false` with null sections | explicit, never silently partial |
| D-15 | Graph as two Parquet tables (nodes, edges) with sample provenance keys (≤ 5) and the derivation documented | lightweight, DuckDB-queryable; the full supporting set is re-derivable from the revision with the documented query |
| D-16 | Clustering: Jaccard over token features + deterministic DBSCAN, implemented in ~60 lines | no scikit-learn; noise support (nothing forced into a cluster); deterministic without a seed; volumes fit O(n²) with a hard bound |
| D-17 | Clusters are content-addressed and written as **unreviewed** bootstrap candidates; no P7 import code | plan feature 9 "fed back to P7" needs a reviewed, separately approved import; automatic profile changes are out of scope |
| D-18 | No migration, no new table, no Scylla writes | analytics metadata (manifests, runs) lives with the outputs |

## 2. Rejected shortcuts

- Reading the TTL'd `*_by_domain_day` tables for date windows (silent loss
  beyond 30 days).
- Treating several table scans as one consistent snapshot.
- Counting excluded or rejected rows as exported.
- Overwriting a published export in place, or appending a re-export to it.
- Falling back to live Scylla when a dataset is missing from an export.
- Filling missing matches/attribution/edges from co-occurrence.
- Sampling sites when the clustering bound is exceeded.

## 3. Configuration defaults (`CRAWLER2_ANALYTICS__*`)

`root=var/analytics`, `segments=64`, `max_retries=3`, `retry_backoff_s=2.0`,
`duckdb_memory_limit=1GB`, `duckdb_threads=2`. Clustering parameters are CLI
flags (`--min-pages 3 --min-similarity 0.6 --min-samples 3 --max-sites
2000`). Nothing runs implicitly; P11 feedback and P7 advice stay off.

## 4. Open owner decisions

| # | Question | P13 default until answered |
|---|---|---|
| OD-P13-1 | Run the read-only real-data export of `crawler2_m1` (full scan, ~tens of minutes on the USB HDD) to demonstrate completeness on real crawl data? | **not run** — the session's permission policy refused launching it; command in validation.md §5 |
| OD-P13-2 | Retention of analytics exports (they copy URLs/titles out of Scylla); who may read them (ADR-005) | kept until removed by an operator; same access as the keyspace |
| OD-P13-3 | Shared location for exports in a multi-host deployment (NFS path vs. object store sync) | local filesystem root |
| OD-P13-4 | Export cadence (periodic job vs. on demand) | on demand (`crawler2-analytics export`); no scheduler added |
| OD-P13-5 | Who reviews cluster bootstrap candidates and whether a P7 import workflow is wanted | candidates stay `unreviewed`; no import |
