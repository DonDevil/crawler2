# ADR-012 — Scylla storage model: query tables, convergent writes, bounded partitions

Status: Accepted (P2, 2026-09-28). Refines ADR-002.

## Context

P1 catalogued the crawler2 access patterns (W1–W15, M1–M6, P1–P5, E1–E4,
X1–X3) and fixed stable contract IDs. P2 turns them into tables that stay
correct under at-least-once delivery, concurrent writers on several hosts,
and bounded partition sizes at a 1M-page load, on Scylla 6.2 (open source).

## Decision

1. **One table per access pattern**, in the service-owned `crawler2`
   keyspace (29 tables; mapping in `docs/phases/p02-storage/schema.md`).
   No generic entity table, no ORM, no secondary indexes or materialized
   views. W15 (path-pattern change history) is deferred: its producer
   (`page.changed`, P5) and pattern definition (P7) do not exist yet.
2. **Rows = key columns + the exact contract JSON** (`doc`) for
   authoritative records; read models store only the columns their query
   needs. IDs are stored as `uuid` (the type prefix is implied by the column).
3. **Buckets and shards** for everything that grows with time or
   popularity: UTC day (`YYYYMMDD`) or month (`YYYYMM`) buckets plus fixed
   hash shards (`id.uuid.int % N`, N per table family in
   `crawler2/storage/layout.py`). Shard counts are part of the physical
   model; changing one is a migration.
4. **Convergence without LWT through write timestamps** (`layout.py`):
   - `latest_wins(t)` = t in µs: the cell keeps the value of the newest fact
     (W5 latest observation, last_seen columns). P1 requires "compare
     observed_at, never arrival order" — this is exactly that.
   - `earliest_wins(t)` = 2·anchor − t (anchor 2020-01-01): first_seen
     columns keep the oldest fact. The textbook `MAX − t` is rejected by
     Scylla (`restrict_future_timestamp`: no timestamps > 3 days ahead), so
     the mirror keeps every timestamp in the past; valid until 2070.
   - `version_wins(v)` = v: the highest target version / encode attempt wins.
   Each fact family has its own columns, so the rules never compete.
   Ties (same instant, different values) are resolved by Scylla's value
   comparison — deterministic on every replica.
5. **LWT only for E1/E2** (one evidence item per match; `collecting →
   sealed` exactly once) and the migration lock. Everything else is an
   idempotent upsert keyed by contract IDs.
6. **Consistency** (`scylla/session.py`, identical at RF=1 and RF=3):
   authoritative writes and the outbox `LOCAL_QUORUM`; derived writes and
   "EC ok" reads `LOCAL_ONE`; W7/E3, relay and schema reads `LOCAL_QUORUM`;
   E1/E2 `LOCAL_SERIAL`.
7. **Write order**: authoritative rows (+ outbox row) first, then derived
   rows. A crash in between leaves derived rows stale, never authoritative
   state inconsistent; replay (at-least-once) or `rebuild_url` /
   `rebuild_media` restores them.
8. **Compaction**: TWCS (1-day windows) only for tables with a table-level
   TTL (W2, W3, W14, outbox, processed events); STCS elsewhere. ICS is not
   available in Scylla 6.2 OSS; LCS is a candidate for the overwrite-heavy
   single-row tables (W5, W12) once read amplification is measured.
   `gc_grace_seconds = 3 h` on TTL-only tables (no deletes, nothing to repair).
9. **Retention categories**: authoritative (attempts, observations, links,
   media observations, evidence) — no TTL (ADR-005: no unagreed deletion);
   operational windows (W2, W3, W14) — 30 days; outbox and consumer
   markers — 14 days; derived/projection tables — no TTL, rebuildable.
10. **Keyspaces are created with `tablets = {'enabled': false}`**: in Scylla
    6.2 a NetworkTopologyStrategy keyspace defaults to tablets, which do not
    support LWT. The migrator refuses a tablet keyspace.

## Consequences

- Readers of sharded/bucketed tables fan out (e.g. 16 queries for a
  domain-day); all such reads are warm/cold paths in P1.
- The partition benchmark (`benchmarks/p2-storage`) exercises exactly these
  layout functions; its results bound the model at a 1M-page load.
- Cells written with `earliest_wins`/`version_wins` must only ever be
  written through those functions (documented in the CQL and layout.py).
- Full-table rebuilds (token-range scans) and archival of authoritative
  history (e.g. to Parquet) are P13/P14 work; retention periods stay open
  until ADR-005 closes.
