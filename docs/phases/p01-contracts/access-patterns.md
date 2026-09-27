# P1 Access-pattern catalog (input to P2 data modeling)

Scylla is modeled query-first (ADR-002): one table per query shape,
denormalized, no joins on hot paths. This catalog lists the reads and
writes that later phases will perform, so that P2 designs tables from
queries instead of from an entity diagram. **No tables are defined here.**

## Ground rules for P2

1. **One shared cluster, service-owned keyspaces** (ADR-002, ADR-011).
   Patterns marked `crawler2` live in the crawler2 keyspace and those
   marked `fingerprinter` in the fingerprinter keyspace. No service reads
   the other's keyspace. Cross-service facts are **projections** built
   from events (P-rows below).
2. Every write is an **idempotent upsert keyed by contract IDs**. Derived
   IDs make concurrent writers on different hosts converge. LWT only where
   noted (write-once or compare-and-set).
3. Partitions must stay bounded (plan P2 gate: none > 100 MB at a 1M-page
   synthetic load). Anything that grows with time or popularity is
   **bucketed** (by time, or by hash).
4. Time-series observations are append-only. Prefer TWCS and whole-partition
   TTL, and avoid per-row deletes (tombstones).
5. "Derived" rows can be rebuilt from authoritative rows (plan B.5 #3).
   The rebuild procedure is a P2/P13 deliverable.
6. Host-side Python cannot reach Scylla in the dev setup (P0 audit).
   Repository tests run inside the app containers.

## Legend

- **Freq** (relative to the crawl rate *R* = fetches/s across the cluster):
  `per-fetch` ≈ R, `per-page` ≈ R × success ratio, `per-media` ≈ new media
  sightings, `per-match` ≪ R, `batch` = scheduled or offline jobs.
- **Path**: `hot` = on the fetch/processing path, latency-bound; `warm` =
  event consumers, seconds OK; `cold` = batch/analytics/reporting.
- **Consist.**: `EC ok` = eventual consistency acceptable (LOCAL_ONE/
  LOCAL_QUORUM reads may lag); `strong` = LOCAL_QUORUM read-after-write
  or LWT required.
- **Auth/Der**: authoritative record, or derived/projection (rebuildable).

## 1. Web memory (crawler2 keyspace)

| # | Actor | Operation | Lookup key → result | Partitioning need | Cardinality per key | Ordering | Time dimension | Freq | Path | Consist. | Auth/Der |
|---|---|---|---|---|---|---|---|---|---|---|---|
| W1 | crawler_worker | write fetch attempt | `fetch_attempt_id` → `FetchAttempt` | spread by id or `(domain_id, day)` | 1 | — | started_at | per-fetch | hot | EC ok | Auth |
| W2 | crawl_intelligence | recent fetch attempts of a domain (fetch-profile learning) | `(domain_id, day bucket)` → attempts | bucket by day; popular domains may need `(domain_id, day, shard)` | 10²–10⁶ | finished_at desc | window: last N days, TTL | batch/warm | warm | EC ok | Auth (W1 dup) |
| W3 | crawl_intelligence | recent attempts of one URL (escalation history) | `url_id` → last N attempts | `url_id` | tens (TTL-bounded) | finished_at desc | last N | per-fetch (when scheduling a retry) | warm | EC ok | Auth (W1 dup) |
| W4 | crawler_worker | write page observation | `observation_id` → `PageObservation` | by `url_id` (history) + point table | 1 | — | observed_at | per-page | hot | EC ok | Auth |
| W5 | crawler_worker (conditional GET), crawl_intelligence | **latest observation of a URL** (validators, last version, last status) | `url_id` → 1 row | `url_id` | 1 (overwrite) | — | latest only | per-fetch | **hot** | EC ok (a stale ETag costs one full fetch) | Der (from W4) |
| W6 | crawl_intelligence, evidence | observation history of a URL | `url_id` → observations | `url_id`, bucket by month if a URL is crawled very often | 10–10⁴ | observed_at desc | range / last N | warm | warm/cold | EC ok | Auth |
| W7 | evidence_collector, extraction (re-run) | get observation by id | `observation_id` → observation | point | 1 | — | — | per-match, rare | warm | strong-ish (LOCAL_QUORUM) | Auth |
| W8 | crawl_intelligence | page versions of a URL (change history, change rate) | `url_id` → versions with first_seen/last_seen/observation count | `url_id` | 1–10³ | first_seen desc | range | warm | warm | EC ok | Der (from W4) |
| W9 | extraction (write), intelligence/graph export (read) | links out of a page version | `page_version_id` → `DiscoveredLink`s | `page_version_id` | 1–10⁴ | none (set) | immutable per version | per-page (write) / batch (read) | warm/cold | EC ok | Auth |
| W10 | intelligence, evidence (referrer), analytics | inlinks of a URL ("who links here") | `url_id` → (source url_id, page_version_id, first_seen) | `url_id` + hash bucket (hub pages have huge fan-in) | 1–10⁶ | first_seen desc | range | batch | cold | EC ok | Der (from W9) |
| W11 | crawl_intelligence | known URLs of a domain (recrawl planning, pattern learning) | `(domain_id, bucket)` → url_id, last status, last observed | `(domain_id, hash bucket)` | 10²–10⁷ per domain | none | last observed | batch | cold | EC ok | Der |
| W12 | crawl_intelligence, frontier admission fallback | "known URL?" + minimal URL state (first seen, last observed, next due) | `url_id` → 1 row | `url_id` | 1 | — | — | per discovered link | **hot** (Redis filter first, P3) | EC ok | Der |
| W13 | crawl_intelligence | domain record + source intelligence (profile, politeness, productivity, negative knowledge) | `domain_id` → `Domain` + profile | `domain_id` | 1 (+ small history) | — | versioned updates | per-fetch read (cached), batch write | hot read (cacheable) | EC ok | Der (learned from W2/W14/matches) |
| W14 | crawl_intelligence | recent observations for scheduling (what changed / what is productive, per domain and window) | `(domain_id, day)` → observation summaries | bucket by day | 10²–10⁶ | observed_at desc | sliding window, TTL | batch | warm | EC ok | Der (from W4) |
| W15 | crawl_intelligence | domain/path-pattern change history | `(domain_id, path pattern)` → change events | `(domain_id, pattern)`, bucket by month | 10–10⁴ | time desc | range | batch | cold | EC ok | Der |

## 2. Media registry (crawler2 keyspace)

| # | Actor | Operation | Lookup key → result | Partitioning need | Cardinality | Ordering | Time | Freq | Path | Consist. | Auth/Der |
|---|---|---|---|---|---|---|---|---|---|---|---|
| M1 | media_registry | upsert media entity | `media_id` → `Media` (+ first/last seen) | `media_id` | 1 | — | first/last seen | per-media | warm | EC ok (idempotent) | Auth |
| M2 | media_registry | record media observation | `(page_observation_id, media_id)` → `MediaObservation` | by `media_id` (history) + by page (M4) | 1 | — | observed_at | per-media | warm | EC ok | Auth |
| M3 | evidence_collector, crawl_intelligence | observations of a media entity | `media_id` → observations | `media_id` + month bucket (a popular CDN file is seen on many pages) | 1–10⁵ | observed_at desc | range / last N | per-match / batch | warm | EC ok | Auth |
| M4 | crawl_intelligence (page productivity), evidence | media on a page / page version | `page_version_id` (or `url_id`) → media ids + kind | `page_version_id` | 0–10³ | none | per version | batch / per-match | warm | EC ok | Der (from M2) |
| M5 | media_registry, evidence_collector | **media by content identity** (map `match.found` back to media; dedupe encodes) | `content_id` → media ids (+ first seen) | `content_id` | 1–10⁴ | first_seen | — | per-media (write) / per-match (read) | warm | EC ok for dedupe (duplicates only cost a redundant idempotent request) | Der (from M2) |
| M6 | media_registry | versions of a media entity (content changes at one locator) | `media_id` → (`content_id`, first/last seen) | `media_id` | 1–10² | first_seen desc | range | per-media | warm | EC ok | Der (from M2) |

## 3. Cross-service projections kept by crawler2 (built from events)

| # | Actor | Operation | Lookup key → result | Partitioning | Cardinality | Ordering | Time | Freq | Path | Consist. | Auth/Der |
|---|---|---|---|---|---|---|---|---|---|---|---|
| P1 | media_registry | **representation status** (should we request an encode?) | `content_id` → per spec: requested / ready / failed(+retryable, attempts) | `content_id` | 1–10 specs | — | last transition | per-media (read), per fingerprinter event (write) | hot for the registry | EC ok (duplicate requests are idempotent at the encoder) | Der (from `representation.ready`/`encode.failed`) |
| P2 | crawl_intelligence, feedback, evidence | **active targets** | `target_id` → latest `Target` version, retired flag; plus "all active" listing | single small table; listing by a fixed bucket | 10²–10⁴ total | — | registered/retired at | per event (write), per plan cycle (read) | warm | EC ok | Der (from `target.*`) |
| P3 | feedback, evidence, reporting | matches by content | `content_id` → `MatchResult`s | `content_id` | 0–10² | decided_at desc | — | per-match | warm | EC ok | Der (from `match.found`) |
| P4 | reporting, campaign coverage, crawl_intelligence | **matches by target version** (target/media relationships) | `(target_id, month bucket)` → match, content_id, media ids, first page | `(target_id, bucket)` | 10–10⁶ | decided_at desc | range | per-match (write) / batch (read) | cold | EC ok | Der (from `match.found` + M5) |
| P5 | feedback (P11) | matches by source domain (source value learning) | `(domain_id, month)` → match summaries | `(domain_id, bucket)` | 0–10⁵ | decided_at desc | range | per-match | cold | EC ok | Der |

## 4. Evidence (crawler2 keyspace)

| # | Actor | Operation | Lookup key → result | Partitioning | Cardinality | Ordering | Time | Freq | Path | Consist. | Auth/Der |
|---|---|---|---|---|---|---|---|---|---|---|---|
| E1 | evidence_collector | open at most one candidate per match | `match_id` → `evidence_id` | `match_id` | 1 | — | — | per-match | warm | **strong: LWT insert-if-not-exists** | Auth |
| E2 | collector (write), finalizer (read/transition) | evidence item and its state | `evidence_id` → candidate, state (collecting → sealed) | `evidence_id` | 1 | — | collected/finalized at | per-match | warm | **strong: LWT state transition; sealed is write-once** | Auth |
| E3 | finalizer, exporter, audit | provenance of an evidence item | `evidence_id` → observation ids → W7 rows → snapshot `BlobRef`s → object store | via E2 + W7 point reads | 1–10² refs | — | — | per-match | warm/cold | LOCAL_QUORUM | Auth |
| E4 | reporting | evidence by target | `(target_id, month)` → evidence ids + state | `(target_id, bucket)` | 0–10⁵ | finalized_at desc | range | batch | cold | EC ok | Der |

## 5. Fingerprinter keyspace (owned and designed by the fingerprinter, P9/P10)

Listed so that the boundary is complete. crawler2 never runs these
queries.

| # | Actor | Operation | Lookup key → result | Partitioning | Cardinality | Freq | Path | Consist. | Auth/Der |
|---|---|---|---|---|---|---|---|---|---|
| F1 | encoder | representation exists? / write it | `(content_id, spec)` → representation metadata + vector ref | `content_id` | 1–10 | per encode request | hot for the encoder | strong enough to avoid duplicate GPU work (LWT or a lease in Redis) | Auth |
| F2 | index builder | all representations of a spec (backfill, index rebuild) | `(spec, bucket)` → representation ids | `(spec, hash bucket)` | 10⁶+ | batch | cold | EC ok | Auth |
| F3 | target manager | target version and its reference media | `(target_id, version)` → target | `target_id` | 1–10 | rare | warm | LOCAL_QUORUM | Auth |
| F4 | matcher | target representations of a version | `(target_id, version, spec)` → representation | `target_id` | 1–10 | per match batch | warm | EC ok | Auth |
| F5 | matcher | match results (authoritative) | `match_id`; by `(target_id, bucket)`; by `content_id` | as listed | — | per-match | warm | EC ok | Auth |
| F6 | matcher | ANN search | vector index (derived, not Scylla; ADR to be chosen in P10) | — | top-k | per new rep/target | hot | — | Der |

## 6. Event plumbing (P2 design, listed for completeness)

| # | Actor | Operation | Key | Notes |
|---|---|---|---|---|
| X1 | every producer | append outbox row with the entity write | `(producer shard, time bucket)` → envelope JSON | ADR-004 outbox; same partition/batch as the entity where possible |
| X2 | outbox relay | scan unsent rows, mark sent | same | bounded partitions; TTL after send |
| X3 | every consumer | "already processed?" on the idempotency key | `(consumer, idempotency key)` → processed_at | hot; TTL ≥ the redelivery horizon; Redis or Scylla decided in P2 |

## 7. Observations that shape P2

- Most hot reads are **point lookups by a derived ID** (W5, W12, P1, F1).
  These fit single-row partitions and cache well.
- Everything keyed by domain, target, media popularity or hub URLs has
  **unbounded fan-in** and needs bucketing (W2, W10, W11, M3, P4, E4).
- Several read models are **the same fact denormalized** (W1 → W2/W3; W4 →
  W5/W6/W8/W14; M2 → M3/M4/M5/M6). The writer, or an event consumer,
  maintains them. P2 should decide per pair whether it is written in the
  same batch or derived asynchronously.
- Only E1, E2 (and possibly F1) need LWT. Everything else is an idempotent
  upsert.
- Nothing requires a cross-keyspace read. Every cross-service need is met
  by projections P1–P5.
