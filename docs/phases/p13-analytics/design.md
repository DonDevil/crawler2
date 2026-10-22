# P13 design — analytics layer (as implemented)

Status: implemented 2026-10-22. Code: `crawler2/analytics/` — `datasets.py`
(catalog + mappers), `source.py` (read-only row sources), `export.py`
(scan → stage → build → reconcile → publish), `query.py` (DuckDB views,
re-validation), `reports.py`, `graph.py`, `cluster.py`, `cli.py`
(`crawler2-analytics`), `sql/` (analyst queries). No migration.
Operator guide: [../../architecture/analytics.md](../../architecture/analytics.md).

## 1. Data flow

```
crawler2 keyspace (Scylla)                                   never: Redis, MinIO, fingerprinter keyspace
   │  SELECT … WHERE token(pk) >= ? AND token(pk) <= ?   (N segments, LOCAL_QUORUM, driver paging)
   ▼
<root>/_staging/<run_id>/            run.json · segments/<table>/seg-NNNNN.json (counters)
   rows/<dataset>/seg-NNNNN.jsonl.gz (mapped rows; renamed into place per completed segment)
   │  DuckDB (disk-backed stage.duckdb, memory_limit, temp_directory)
   ▼  collapse exact duplicates · refuse key conflicts / null keys · COPY → Parquet · read back
<root>/_staging/<run_id>/data/<dataset>/schema_version=N/date=YYYY-MM-DD/part-K.parquet
   │  reconcile (design §6) · manifest · publish lock
   ▼  unchanged content → nothing new;   changed → atomic rename + CURRENT
<root>/exports/<export_key>/rev-NNNN/{_manifest.json, <dataset>/…}     CURRENT → newest revision
<root>/runs/<run_id>.json                                              every run: published|unchanged|failed
   │  DuckDB views over the manifest's file list only
   ├─► reports (campaign, source)               stdout / --out
   ├─► <root>/derived/<key>/rev-NNNN/graph-v1/  nodes.parquet, edges.parquet, _manifest.json
   └─► <root>/derived/<key>/rev-NNNN/cluster-v1-<params>/  features, assignments, clusters,
                                                template groups, bootstrap-candidates.json (unreviewed)
```

Nothing in a crawler, extraction, media, matching, evidence or feedback path
imports `crawler2.analytics` (enforced by `tests/unit/analytics/test_boundaries.py`).
The exporter issues `SELECT` statements only.

## 2. Export boundary and consistency guarantees

An export **scope** is `(source keyspace, since, until, datasets + schema
versions)`; its SHA-256 prefix is the **export key**. `until` (exclusive, default
the run's start) and `since` (inclusive, optional) bound each windowed
dataset's **time column**; dimensions (`domains`, `targets`, `campaigns`,
`source_profiles`) are exported whole as current state.

What is guaranteed:

- **Coverage:** the token ranges are disjoint and cover the full Murmur3 ring,
  so each scan returns every row present when its page was read, once.
- **Per-page read consistency:** LOCAL_QUORUM — every acknowledged
  authoritative write before the page read is seen.
- **Window semantics:** a row is in the export iff its time column is in
  `[since, until)`; rows with a null time column are kept in `date=unknown`
  and counted (`unknown_time`), never dropped.

What is **not** guaranteed (and is not claimed): a snapshot. Tables are read
one after another and each segment at its own instant (recorded as
`scan_started_at`/`scan_finished_at`). A row written during the scan may or
may not be included; a late write with an old event time, or a TTL expiry,
changes a past window. Such changes are visible as a different content id on
re-export and become a new revision (§5). `--verify-counts` re-counts every
segment after the scan and records the drift per table.

## 3. Datasets

### 3.1 Inventory (schema version 1)

| Dataset | Source (table → column) | Key | Time column | Rows mean |
|---|---|---|---|---|
| `fetch_attempts` | `fetch_attempts.doc` (W1) | fetch_attempt_id | finished_at | every fetch attempt, any outcome |
| `page_observations` | `page_observations.doc` (W7) | observation_id | observed_at | a response was recorded (any status) |
| `page_extracts` | `page_extracts.doc` (P5) | page_version_id | observed_at | a raw page version was parsed |
| `page_links` | `page_extracts.doc.links` | page_version_id, ordinal | observed_at | one link of an extract |
| `page_media_refs` | `page_extracts.doc.media` | page_version_id, ordinal | observed_at | media discovered by extraction |
| `urls` | `url_state` (W12) | url_id | first_seen | a URL became known (discovered) |
| `domains` | `domains` (W13) | domain_id | — (dimension) | one host |
| `page_revisions` | `page_revisions_by_url` (P5) | url_id, revision_id | first_seen | a normalized content state |
| `media` | `media` (M1) | media_id | first_seen | a media entity |
| `media_observations` | `media_observations.doc` (M2) | page_observation_id, media_id | observed_at | media seen on a page observation; `content_id` = identified bytes |
| `targets` | `targets.doc` (P2) | target_id | — (dimension) | latest target version known to crawler2 |
| `matches` | `matches_by_content.doc` (P3) | content_id, match_id | decided_at | the matcher's claim (verdict, confidence unmodified) |
| `evidence_candidates` | `evidence_candidates` (E5) | evidence_id | collected_at | a working dossier (status at export) |
| `evidence_sources` | `evidence_candidates.doc.sources` | evidence_id, ordinal | collected_at | one attributed source with its basis |
| `evidence_versions` | `evidence_versions` (E6) | evidence_id, version | finalized_at | a finalized package version |
| `campaigns` | `evidence_campaigns` (E8) | campaign_id, target_id | — (dimension) | campaign membership |
| `source_profiles` | `intel_source_profiles` (I1) | domain_id | — (dimension) | P7 fold, current state, TTL'd |

Column lists and types are declared once in `datasets.py` (`crawler2-analytics
datasets` prints the catalog). Every row carries wire ids
(`url_…`, `obs_…`, `mat_…`), so it traces back to the Scylla row and to events.

### 3.2 Normalization

- `host` = lower-cased URL host without trailing dot; `site` = registrable
  domain (eTLD+1 via the P6 PSL helper; an IP is its own site). Both are
  `NULL` when the URL has no host; reports label that `(unknown site)`.
  Original URLs are kept next to them.
- Timestamps are `TIMESTAMP WITH TIME ZONE` (UTC); driver datetimes (naive
  UTC) are made explicit. Page-declared dates (`published_at`, `modified_at`)
  that cannot be parsed become `NULL` (they are claims of the page).
- Lists (`redirect_urls`, `truncated`, `gap_codes`) are `VARCHAR[]`, empty
  rather than `NULL` when the source has none.

### 3.3 Exclusions and privacy

Excluded tables and the reason for each are in `datasets.EXCLUDED` (also in
every manifest under `excluded_sources`). Never exported: page bodies, visible
text, response headers, snapshot bytes (only the `snapshot_uri` reference),
E6 `finalized_by` and `reason`, the E7 audit trail (operator identities, free
text), search snippets. Exported URLs and page titles are what the crawler
already stores; analytics outputs must be treated with the same access
controls as the keyspace (ADR-005 is open; nothing is redacted beyond the
exclusions).

## 4. Parquet layout and versioning

```
<root>/exports/<export_key>/rev-NNNN/
  _manifest.json
  <dataset>/schema_version=<v>/date=<YYYY-MM-DD|unknown>/part-<k>.parquet   (windowed)
  <dataset>/schema_version=<v>/part-0.parquet                               (dimensions, empty)
```

- `date` = UTC day of the dataset's time column, stored **in** the file too,
  so readers never depend on directory names (`hive_partitioning = false`).
- Domain is **not** a directory level (D-3): rows are sorted by `site` (then
  time, key) inside each file, so Parquet row-group statistics prune by site;
  no per-domain or per-URL directories, no tiny-file explosion.
- An empty dataset still gets one zero-row file with the full schema.
- zstd compression; one file per (dataset, day) at the current volumes.
- **Schema versioning:** any change of columns, types or meaning → a new
  `schema_version` directory and catalog entry; the version is part of the
  export key. `ExportView` refuses (marks unavailable) a dataset whose
  manifest version this code does not know. Old revisions stay readable by
  code that knows their version; no in-place migration of Parquet.

## 5. Publication, idempotency, recovery

- **Run identity:** `run_id = <UTC start>-<random>`; staging under
  `<root>/_staging/<run_id>/`.
- **Segment checkpoints:** a segment's staged rows and counters are renamed
  into place only after the segment's scan completed. A transient
  `StorageUnavailableError` retries the *whole* segment (its partial file is
  discarded) up to `max_retries` with linear backoff; retries are counted.
- **Failed run:** `run.json` gets `status=failed` and the error; `runs/<id>.json`
  records it; nothing is published. `export --resume <run_id>` reuses completed
  segments and continues (same scope, same source label enforced).
- **Content id:** SHA-256 over `{dataset: [schema_version, status, rows,
  digest]}`; `digest` is an order-independent XOR of per-row MD5s of DuckDB's
  JSON rendering (change detection, not a cryptographic commitment — file
  SHA-256s are).
- **Publish (under `.publish.lock`, `O_EXCL`):** if `CURRENT`'s revision has
  the same content id the run is **unchanged** — no new files, staging
  removed, run recorded. Otherwise the staged `data/` directory, with its
  manifest already inside, is renamed to `rev-(max+1)` (atomic on one
  filesystem) and `CURRENT` is replaced (write-temp + `os.replace`).
- **Readers** resolve `CURRENT` or a named revision; a directory without a
  manifest is never read. A crash between rename and pointer update is
  repaired on the next publish (`repair_current`: newest manifested revision).
- **Re-export after a source change** publishes the next revision of the same
  export key; earlier revisions are immutable and kept (explicit deletion only).
  Old and new rows are never merged: each revision is a complete, independent
  extraction.

## 6. Reconciliation (per dataset, in every manifest)

| Count | Meaning |
|---|---|
| `source_rows` | rows the scan returned for the source table (all segments) |
| `rejected` | source rows the mapper could not map (malformed/missing required facts); samples kept |
| `candidates` | output rows produced by the mapper |
| `out_of_window` | candidates outside `[since, until)` (excluded by scope, disclosed) |
| `unknown_time` | candidates with a null time column (kept, `date=unknown`) |
| `extracted` | candidates − out_of_window (staged) |
| `staged` | rows DuckDB read from the staged files |
| `duplicates_collapsed` | exact duplicate rows removed |
| `key_conflicts` | rows sharing a key with different content (→ run fails) |
| `null_keys` | rows with a null key (→ run fails) |
| `written` | distinct rows written to Parquet |
| `readback` / `readback_distinct_keys` | rows / keys read back from the Parquet files |
| `missing_keys` / `unexpected_keys` | anti-joins staged ↔ Parquet on the key |
| `segment_retries` | segments re-scanned after a transient failure |

Invariants (any violation fails the run): `candidates + rejected =
source_rows` (1:1 datasets); `candidates − out_of_window = extracted =
staged`; `written + duplicates_collapsed = staged`; `readback =
readback_distinct_keys = written`; no missing/unexpected keys; staged and
Parquet digests equal; no key conflicts or null keys. Per file: path, date,
rows, bytes, SHA-256. `crawler2-analytics validate` re-checks a published
revision independently (files, sizes, SHA-256, rows, key uniqueness, digest).

## 7. DuckDB layer

`ExportView(revision_dir)` opens an in-memory DuckDB (`TimeZone=UTC`) and
creates one view per readable dataset over **exactly the manifest's files**;
missing or truncated files are refused, `--verify` also checks SHA-256. The
view never connects to a store. `crawler2-analytics sql` runs ad-hoc SQL over
those views; `crawler2/analytics/sql/*.sql` are maintained analyst queries.

## 8. Standard reports (`campaign-report/v1`, `source-report/v1`)

Pure functions of (revision, parameters); JSON is authoritative, Markdown is
a rendering. Each report carries `export` (key, revision, content id, scope,
dataset versions/rows), `definitions`, `complete` + `incomplete_reasons`,
`data_quality` (rejected/unknown-time/out-of-window/duplicate counts of each
input) and `report_digest` (SHA-256 of the canonical body). Rates are
`{numerator, denominator, value}` with `value = null` when the denominator is
0.

**Policy for missing input:** the scoping dataset (`campaigns` for the
campaign report; at least one source dataset for the source report) missing →
the report fails (exit 3). Any other missing dataset → its sections are `null`
and `complete = false` with the reason. Reports never fall back to a live store.

**Campaign report** (`--campaign`): targets in campaign / in projection / not
in projection / retired; matches *reported* (any verdict, decided in the window
and ≥ the campaign's `since`) vs *confirmed by matcher* (verdict `confirmed`;
automated, not reviewed), by target and day; media carrying matched content;
pages/sites where that content was observed (registry co-occurrence) and
matches without observed content; evidence *candidates* by status vs
*finalized packages* (≥ 1 E6 version) by latest-version status, superseded
versions, matches without candidate; rates candidate coverage and finalized
share. Counting follows P12's statistics (match once per id, page once per
URL, site once per registrable domain).

**Source report** (all sites or `--site`): per site — URLs *discovered*,
fetch *attempts* (requested site), *observations* and *OK* observations
(final site), pages *fetched OK*, pages *parsed*, media *discovered* (extract
refs), media *observed*/*identified* (registry), matched contents *seen*,
evidence sources by basis; rates response rate, OK rate, parse coverage;
per-site daily observations; totals.

## 9. Derived graph (`graph/v1`)

Nodes `node_id = <type>:<natural id>` (`site:`, `page:` = url id, `media:`,
`content:`, `match:`, `target:`, `evidence:`) with `label`, `site` and
`known` (present in its own dataset). Edges carry `edge_id` (SHA-256 of
type|src|dst), `directed`, `weight` (supporting records), `distinct_pages`,
`status`, `confidence`, `first_at`/`last_at`, `source_dataset` and up to 5
sorted `source_keys` (record keys in that dataset).

| Edge | From → to | Evidence basis | Aggregation |
|---|---|---|---|
| `site_links_to` | site → site (directed) | `page_links` with `cross_site` | link rows; keys `page_version_id#ordinal` |
| `site_redirects_to` | requested site → final site | `page_observations` | observations; keys `observation_id` |
| `site_embeds_media_from` | page site → media host site | `page_media_refs` | refs |
| `site_shares_content` | site — site (undirected, `src < dst`) | `media_observations` with the same `content_id` | distinct contents; keys content ids |
| `page_has_media` | page → media | `media_observations` | observations; status = probe statuses |
| `media_has_content` | media → content | `media_observations.content_id` | observations |
| `match_of_content` / `match_for_target` | match → content / target | `matches` | one per match; status = verdict, confidence = matcher's |
| `evidence_of_match` | evidence → match | `evidence_candidates` | status = candidate status |
| `evidence_attributes_page` | evidence → page | `evidence_sources` | status = basis (`content_verified`/`locator_only`) |

No edge is inferred: a match whose content was never observed on a page has
no path to a page (counted as `matches_without_observed_content`); edges are
co-occurrence and say nothing about ownership or coordination. Validation
(build fails otherwise): no duplicate edge/node ids, no null or dangling
endpoints. Output is rebuilt from the revision only; same revision ⇒ same ids
and content digest (`unchanged`); different content ⇒ refused unless
`--replace`.

## 10. Site/template clustering (`site-features/v1`, `dbscan-jaccard/v1`)

- **Input:** latest extract per URL with a known site, plus anchor-link and
  media-ref aggregates — persisted P5 facts only (no body, no visible text).
- **Template groups:** pages per (site, P7 URL template) with the number of
  distinct structural hashes and the dominant structural share.
- **Site features:** tokens `tpl:<template>` (≥ 2 pages and ≥ 10 % of the
  site), `og:`/`lang:` (dominant on ≥ 20 %), `mkind:`/`mmethod:` (≥ 10 %),
  `text:`/`anchors:` (log2 bucket of the median), `media_pages:`,
  `internal_links:`, `struct_reuse:` (quarter buckets). Missing facts give
  `0` buckets or no token, never invented values.
- **Minimum data:** `min_pages` (3) distinct URLs; fewer → `insufficient_data`.
- **Algorithm:** DBSCAN over Jaccard similarity of token sets; neighbours at
  `≥ min_similarity` (0.6); core points need `min_samples` (3, itself
  included); others are `noise` (unassigned). Deterministic (sorted sites, no
  randomness, so no seed). O(n²) similarity, bounded by `max_sites` (2000;
  more is refused, never sampled).
- **Labels:** `cluster_id = c-` + SHA-256(sorted members)[:12]; output
  directory `cluster-v1-<params digest>`.
- **Outputs:** `site_features`, `site_clusters` (status, cluster, core,
  nearest site/similarity), `clusters` (members, shared features ≥ 80 %,
  mean/min similarity), `template_groups` (Parquet) and
  `bootstrap-candidates.json`; manifest with params, feature provenance,
  export reference, `bootstrap_status: unreviewed` and the disclaimer.
- **Limitations:** token features are coarse; DBSCAN can chain similar
  sites; template tokens depend on P7's literal-word list; a site's latest
  pages only; nothing validates clusters against ground truth (none exists).

### 10.1 P7 bootstrap boundary

P13 produces **unreviewed** candidates and never imports them. The handoff
file `bootstrap-candidates.json` (format
`crawler2.analytics.p7-bootstrap-candidates/v1`) lists each cluster with its
members, shared features, per-member templates (≥ 2 pages) and an empty
`review` block (`decision`, `reviewer`, `note`). A later, separately
approved P7 workflow would: (1) accept only files whose export content id and
params match a manifest it can verify; (2) import only clusters with
`review.decision = "accepted"`; (3) use them as *priors* for unknown hosts
(e.g. initial template roles), never as overrides of observed profile state;
(4) record the source file digest in every profile it seeds. None of that is
implemented; P7 runtime profiles, admission, priorities and P11 feedback are
untouched by P13.

## 11. Operations, security, retention

- Memory: DuckDB `memory_limit` (1 GB) and `threads` (2), disk-backed stage
  database with a temp directory inside the run; Python holds one page of
  rows at a time.
- Cost: full scans of the keyspace per export (§2); the largest table
  (`page_extracts`) is scanned once for three datasets.
- Retention: exports, derived outputs and run records are never deleted
  automatically. `gc` removes only failed/abandoned **staging** runs.
  Removing a revision is a manual operator action (never the `CURRENT` one).
  Exported data inherits the keyspace's sensitivity (URLs, titles).
- Failure classes: storage unavailable (retried per segment, then run
  failed/resumable), validation failure (run failed, staging kept for
  inspection), publish lock held (run failed, resumable after the lock is
  cleared), corrupted/missing published file (readers refuse; `validate`
  reports).
