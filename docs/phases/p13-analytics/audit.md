# P13 audit — analytics layer

Date: 2026-10-22. Scope: what existed for offline analytics before P13, which
durable records can feed it, and the constraints found. Design:
[design.md](design.md). Decisions: [decisions.md](decisions.md).

## 1. Starting state

- Branch `main`, clean working tree at `5732430` (P12 docs). No P13 code,
  no DuckDB/Parquet dependency, no analytics configuration.
- Plan §P13: "periodic export durable store → Parquet (partitioned by
  date/domain); DuckDB notebooks/queries; derived graph (domain↔domain,
  page→media→target) built offline; site/template clustering (plan feature 9)
  prototyped here and fed back to P7 as source-profile bootstrap. Test:
  export completeness (row counts vs source), idempotent re-export. Exit
  gate: standard campaign/source reports reproducible from Parquet alone."
- Gates of earlier phases this phase depends on but does not change:
  **P10 / M3 NOT PASSED** (fingerprinter repo: no real `match.found` exists);
  **P11 / M4 NOT PASSED** (feedback off by default); **P12 / M5 NOT PASSED —
  awaiting owner review**; P7 closed with WHEN/M2 deferred; P6 Gate G open.

## 2. What existed and was reused

| Existing piece | Where | Used by P13 for |
|---|---|---|
| Scylla schema V001–V007, one table per access pattern; `doc` columns hold exact contract JSON | `crawler2/storage/scylla/cql/` | the source of every dataset; docs are parsed, never re-derived from other tables |
| `ScyllaSession` (prepared statements with named consistency, driver paging `stream`, unavailability → `StorageUnavailableError`) | `storage/scylla/session.py` | token-range scans at `STRONG_READ` (LOCAL_QUORUM) |
| Contract wire ids `<prefix>_<hex>` (ADR-008), `UrlRef.of` | `contracts/` | stable identifiers in every row; `page_url_id` of P12 sources |
| `registrable_domain` (PSL, IPs are their own site) | `filtering/inputs.py` (P6) | the `site` normalization of every URL |
| `tokenize_url` (URL templates) | `intelligence/what.py` (P7 increment 3) | clustering feature and template groups |
| P5 `PageHashes.structural`, metadata, stats, links, media refs in `page_extracts.doc` | P5 | page features (no body/visible text needed) |
| P12 E5 candidate docs (sources with attribution basis), E6 versions, E8 campaigns | V007 | evidence datasets, campaign scope |
| Ad-hoc read-only M1 export to JSONL | `benchmarks/p7-intelligence/export_m1.py` | precedent for which tables carry the facts; not reused as code (unvalidated, no reconciliation, single pass) |
| P12 campaign statistics | `evidence/stats.py` | metric definitions the campaign report stays consistent with (match/evidence dedup, page/domain counting) |

## 3. Source records actually available

Inspected the CQL of V001–V007 and the mapping code of the repositories.

- **Exportable, authoritative:** `fetch_attempts` (W1, doc), `page_observations`
  (W7, doc), `page_extracts` (P5, doc incl. links + media refs),
  `url_state` (W12), `domains` (W13), `page_revisions_by_url` (P5),
  `media` (M1), `media_observations` (M2, doc), `targets` (P2 projection,
  doc), `matches_by_content` (P3 projection, doc), `evidence_candidates`
  (E5), `evidence_versions` (E6), `evidence_campaigns` (E8).
- **Derived current state:** `intel_source_profiles` (P7 I1, 30-day TTL) —
  exported as a dimension, labelled "not history".
- **Excluded** (reasons in design §3.3 and `datasets.EXCLUDED`): outbox and
  delivery state, TTL'd per-day indexes, read models that duplicate an
  exported table, P8 workflow state, fingerprinter projections, P11
  feedback rows, E2/E4 indexes, the evidence audit trail (actors, free
  text), P6 rule/admission state, search results (third-party snippets),
  seeds/scope, Redis, the fingerprinter keyspace.
- **Not implemented anywhere** (so no dataset): screenshots, WARC/headers,
  infrastructure indicators (IP/ASN/TLS/hosting), page-type labels stored
  per page (P7 WHAT is offline-only), source lifecycle history (I1 holds
  only the current fold), campaign-scoped crawl coverage (page observations
  are target-independent — the same limit P12 reports).

## 4. Constraints discovered

1. **No snapshot reads.** Scylla offers no consistent snapshot across
   partitions or tables; every table is keyed by id, not time, so a time
   window cannot be pushed down: each export is a **full token-range scan**
   whose cost is O(table), filtered client-side by the dataset's time
   column. Tables cannot be read "as of" an instant.
2. **TTL'd tables shrink between exports** (`intel_source_profiles`; and
   the excluded per-day indexes). A re-export of an unchanged boundary can
   therefore legitimately differ.
3. **Late writes with old event times** (e.g. a P5 extract written for an
   old observation) change the content of a past window.
4. **Dev keyspaces lag migrations:** the dev `crawler2` keyspace was not
   migrated past V005 (V006/V007 unapplied, P11/P12 records); `crawler2_m1`
   predates P7–P12 tables. Missing tables must surface as *unavailable*,
   never as empty.
5. **`page_revisions_by_url` has no host column**; site analysis joins
   `urls`.
6. **DuckDB's Python client needs `pytz`** to return `TIMESTAMP WITH TIME
   ZONE` values; crawler2 does not depend on it, so query helpers return
   timestamps as ISO strings.
7. **DuckDB auto-detects Hive partitions** from `date=` paths and would
   re-type the `date` column; exports store `date` in the files and readers
   pass `hive_partitioning = false`.
8. **Dev host limits:** USB HDD (Scylla disk-bound), 15 GB RAM, oomd kills
   jobs under memory pressure — exports must stream, spill to disk and be
   memory-capped.
9. **Real data:** `crawler2_m1` (M1, ~46 h, stopped 2026-10-05) holds real
   crawl history (≈ 215k observations, 139k extracts per the P7 export) but
   no targets, matches or evidence. No real match exists anywhere (P10).

## 5. Assumptions

- Analytics output is a local/shared filesystem tree (`var/analytics` by
  default), not MinIO (decision D-4).
- The UTC day of each dataset's own event time is the right partition key;
  domain is a sorted column, not a directory (D-3).
- One export reads one keyspace; the fingerprinter keyspace is never read
  (B.5 #2), so matcher-side facts come only from crawler2's projections.
