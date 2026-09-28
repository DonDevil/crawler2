# P2 Schema — table ↔ access-pattern mapping

Source of truth: `crawler2/storage/scylla/cql/V001__initial_schema.cql`
(keyspace `crawler2`, owner **crawler2**; the fingerprinter keyspace is not
created or read by crawler2). Layout functions: `crawler2/storage/layout.py`.
Rules: ADR-012. Every table below exists because of the listed P1 pattern;
there is no other table except `schema_migrations` / `schema_lock`
(migration history and lease).

Abbreviations: **A** authoritative, **D** derived (rebuildable, source
given), **P** projection of another service's events. Bucket `day` =
UTC `YYYYMMDD`, `month` = UTC `YYYYMM`, `shard` = `id.uuid.int % N`.
Timestamp rules: `LW` latest_wins, `EW` earliest_wins, `VW` version_wins.

## 1. Keys, growth and bounds

| Table | Pattern | A/D | Partition key | Clustering (order) | Rows per partition (1M-page worst case) | Bound by |
|---|---|---|---|---|---|---|
| `fetch_attempts` | W1 | A | `fetch_attempt_id` | — | 1 | single row |
| `fetch_attempts_by_domain_day` | W2 | A (copy of W1) | `(domain_id, day, shard)` N=16 | `finished_at DESC, fetch_attempt_id` | 1M/16 ≈ 62.5k (all 1M fetches of one domain in one day) | day bucket × 16 shards |
| `fetch_attempts_by_url` | W3 | A (copy of W1) | `url_id` | `finished_at DESC, fetch_attempt_id` | ≤ 43.2k (one fetch per minute for the 30-day TTL) | 30-day TTL |
| `page_observations` | W4, W7 | A | `observation_id` | — | 1 | single row |
| `page_observations_by_url` | W4, W6 | A | `(url_id, month)` | `observed_at DESC, observation_id` | ≤ 44.6k (one per minute for 31 days) | month bucket |
| `latest_observation_by_url` | W5 | D (W6) | `url_id` | — | 1 (cells `LW`) | single row |
| `page_versions_by_url` | W8 | D (W6) | `(url_id, month)` | `page_version_id` | ≤ 44.6k (a new version every minute) | month bucket |
| `links_by_page_version` | W9 | A | `page_version_id` | `target_url_id, relation` | ≤ 10k (P1 batch max) | contract max |
| `inlinks_by_url` | W10 | D (W9) | `(target_url_id, shard)` N=32 by source | `source_url_id` | 1M/32 ≈ 31k (one hub linked from every page) | 32 shards; one row per linking URL |
| `urls_by_domain` | W11 | D (W12) | `(domain_id, shard)` N=64 by URL | `url_id` | 10M/64 ≈ 156k (10M known URLs of one domain) | 64 shards |
| `url_state` | W12 | D (links, W6) | `url_id` | — | 1 (`EW` first_seen, `LW` last_*) | single row |
| `domains` | W13 | D | `domain_id` | — | 1 (`EW`) | single row |
| `observations_by_domain_day` | W14 | D (W6) | `(domain_id, day, shard)` N=16 by URL | `observed_at DESC, observation_id` | ≈ 62.5k | day × 16 shards |
| `media` | M1 | D (M2) | `media_id` | — | 1 (`EW`/`LW`) | single row |
| `media_observations` | M2 | A | `page_observation_id` | `media_id` | ≤ 1 000 (P1 `media.discovered` max) | contract max |
| `media_observations_by_media` | M3 | A (copy of M2) | `(media_id, month, shard)` N=16 by page obs | `observed_at DESC, page_observation_id` | 1M/16 ≈ 62.5k (one file on every page in a month) | month × 16 shards |
| `media_by_page_version` | M4 | D (M2) | `page_version_id` | `media_id` | ≤ 1 000 | contract max |
| `media_by_content` | M5 | D (M2) | `(content_id, shard)` N=8 by media | `media_id` | 1M/8 = 125k (same bytes at 1M locators) | 8 shards; one row per locator |
| `media_content_versions` | M6 | D (M2) | `(media_id, month)` | `content_id` | ≤ sightings per month | month bucket |
| `representation_status` | P1 | P (`encode.*`, `representation.ready`) | `content_id` | `spec_key` | 1–10 specs | spec count |
| `targets` | P2 | P (`target.*`) | `bucket` (= 0) | `target_id` | 10²–10⁴ total | total targets (10k measured) |
| `matches_by_content` | P3 | P (`match.found`) | `content_id` | `match_id` | 0–10² | targets per content |
| `matches_by_target` | P4 | P (`match.found` + M5) | `(target_id, month, shard)` N=16 | `match_id` | 1M/16 ≈ 62.5k (every page matched in one month) | month × 16 shards |
| `matches_by_domain` | P5 | P (`match.found` + M5 → pages) | `(domain_id, month, shard)` N=16 | `match_id` | ≈ 62.5k | month × 16 shards |
| `evidence_by_match` | E1 | A (LWT) | `match_id` | — | 1 | single row |
| `evidence` | E2 | A (LWT) | `evidence_id` | — | 1 | single row |
| — (no table) | E3 | — | E2 + W7 point reads | — | 1–10² refs | — |
| `evidence_by_target` | E4 | D (E2) | `(target_id, month, shard)` N=16 | `evidence_id` | 1M/16 ≈ 62.5k | month × 16 shards |
| `outbox` | X1, X2 | operational | `(shard, bucket)` N=32, 1-minute bucket | `event_id` | rate × 60 / 32 (18.75k at 10k events/s) | time bucket × 32 shards |
| `outbox_relay_checkpoints` | X2 | operational | `shard` | — | 1 | single row |
| `processed_events` | X3 | operational | `(consumer, idempotency_key)` | — | 1 | single row, TTL |

Measured sizes of the worst partitions: [benchmarks.md](benchmarks.md).

W15 (domain/path-pattern change history) has **no table** in P2: it is
derived from `page.changed` (P5) over path patterns learned in P7; the
table is added additively when those exist. Orderings not expressible as
clustering (W8 by first_seen, P4/P5/E4 by time) are sorted by the reader
over a bounded partition.

## 2. Writes, consistency, retention

| Table | Write pattern & idempotency key | Write CL | Read CL | LWT | TTL / retention | Compaction |
|---|---|---|---|---|---|---|
| `fetch_attempts` | INSERT by `fetch_attempt_id`; logged batch with outbox (`fetch.completed`) | LOCAL_QUORUM | LOCAL_ONE | no | none (authoritative) | STCS |
| `fetch_attempts_by_domain_day` | INSERT, same key | LOCAL_ONE | LOCAL_ONE | no | 30 d (learning window) | TWCS 1 d |
| `fetch_attempts_by_url` | INSERT, same key | LOCAL_ONE | LOCAL_ONE | no | 30 d | TWCS 1 d |
| `page_observations` | INSERT by `observation_id`; logged batch with W6 + outbox (`page.observed`) | LOCAL_QUORUM | LOCAL_QUORUM (W7) | no | none | STCS |
| `page_observations_by_url` | INSERT, same key, same batch | LOCAL_QUORUM | LOCAL_ONE | no | none | STCS |
| `latest_observation_by_url` | UPDATE `USING TIMESTAMP LW(observed_at)` | LOCAL_ONE | LOCAL_ONE | no | none (derived) | STCS (LCS candidate) |
| `page_versions_by_url` | UPDATE: first_* `EW`, last_* `LW`; one-partition batch | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `links_by_page_version` | INSERT by (`page_version_id`, target, relation), ≤ 200-row one-partition batches, then outbox (`urls.discovered`, pattern B) | LOCAL_QUORUM | LOCAL_ONE | no | none | STCS |
| `inlinks_by_url` | UPDATE by (target, source): first `EW`, last `LW` | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `urls_by_domain` | UPDATE by `url_id`: first `EW`, last `LW` | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `url_state` | UPDATE by `url_id`: first `EW`, last `LW` | LOCAL_ONE | LOCAL_ONE | no | none | STCS (LCS candidate) |
| `domains` | UPDATE by `domain_id`, `EW` | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `observations_by_domain_day` | INSERT by `observation_id` | LOCAL_ONE | LOCAL_ONE | no | 30 d (scheduling window) | TWCS 1 d |
| `media` | UPDATE by `media_id`: first `EW`, doc/last `LW` | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `media_observations` | INSERT by (`page_observation_id`, `media_id`); logged batch with M3 + outbox (`media.observed`) | LOCAL_QUORUM | LOCAL_ONE | no | none | STCS |
| `media_observations_by_media` | INSERT, same key, same batch | LOCAL_QUORUM | LOCAL_ONE | no | none | STCS |
| `media_by_page_version` | UPDATE `LW`; content_id only when probed | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `media_by_content` | UPDATE by (content, media), `EW` | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `media_content_versions` | UPDATE: first `EW`, last `LW` | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `representation_status` | UPDATE per fact: requested `LW(occurred_at)`, ready `LW(created_at)`, failure `VW(attempt)`; state derived on read (ready > failed > requested) | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `targets` | UPDATE: version/doc `VW(version)`, retired_at `EW` (terminal) | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `matches_by_content` | UPDATE by `match_id`, `LW(decided_at)` | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `matches_by_target` | UPDATE by `match_id`, `LW`; `media_ids = media_ids + ?` (set union, no tombstones) | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `matches_by_domain` | UPDATE by `match_id`, `LW` | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `evidence_by_match` | `INSERT … IF NOT EXISTS` (first proposal wins) | LOCAL_SERIAL | LOCAL_SERIAL | **yes** | none (ADR-005) | STCS |
| `evidence` | candidate `INSERT … IF NOT EXISTS`; seal `UPDATE … IF state='collecting'`; outbox pattern B | LOCAL_SERIAL | LOCAL_SERIAL | **yes** | none (ADR-005) | STCS |
| `evidence_by_target` | UPDATE collected / finalized columns (state = finalized_at present) | LOCAL_ONE | LOCAL_ONE | no | none | STCS |
| `outbox` | INSERT by `event_id` (bucket = enqueue minute); UPDATE `published_at` | LOCAL_QUORUM | LOCAL_QUORUM | no | 14 d | TWCS 1 d |
| `outbox_relay_checkpoints` | UPDATE by shard (monotone in practice; regressions only cause rescans) | LOCAL_QUORUM | LOCAL_QUORUM | no | none | STCS |
| `processed_events` | INSERT by (consumer, key) | LOCAL_ONE | LOCAL_ONE | no | 14 d (= outbox horizon) | TWCS 1 d |

`gc_grace_seconds = 10800` on the five TTL tables (no deletes; expired data
needs no repair). All other tables keep the default.

## 3. Why these choices (non-obvious ones)

- **Two authoritative copies** (W1 + W2/W3 copies, W7 + W6, M2 + M3): P1
  marks both shapes authoritative. The history copies of W6/M3 are in the
  same logged batch as the point row, so they cannot diverge; W2/W3 are
  windows with a TTL and are written after the point row.
- **Summary columns instead of `doc`** in W2, W3, W14, M3, P4, P5, E4: those
  partitions are the large ones, and their readers need only the summary;
  the full record is one point read away.
- **W6 keeps `doc`** (full observation) because W6 is the evidence/
  intelligence history read; at ≤ 44.6k rows/month it stays far below the
  gate (benchmarks.md).
- **W8 has no observation count**: counters are not idempotent under
  at-least-once delivery (a replay would double-count). The count is
  computed from W6 when needed (batch).
- **Requested vs final URL**: W5/W6/W12/W14 are keyed by the *requested*
  URL (what the scheduler asks about); page versions (W8) by the *final*
  URL, matching `PageVersionId`'s derivation.
- **Readers merge month buckets** from a caller-given range (e.g. from
  `url_state.first_seen`), so a rarely crawled URL costs one read per month
  of its life, not a scan.
