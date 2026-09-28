# ADR-002 — ScyllaDB as the durable store

Status: Accepted (decided 2026-09-28; recorded in P0; refined by ADR-012)

## Context

V2 persists pages, page versions, media, observations, source intelligence
and evidence metadata (V1 stored no page content or HTTP metadata at all,
plan A.2 D16). Write volume grows with crawl rate and must scale out
across hosts. The fingerprinter needs its own durable state.

## Decision

- **ScyllaDB** is the single durable store. One cluster, **service-owned
  keyspaces**: `crawler2` and `fingerprinter`. A service never reads the
  other's keyspace; cross-service data flows via events/contracts.
- Python driver: **`scylla-driver`** (shard-aware fork of the DataStax
  driver), pinned in `uv.lock`.
- Keyspace replication is configuration (`CRAWLER2_SCYLLA__*`):
  `SimpleStrategy RF=1` on a single host, `NetworkTopologyStrategy RF=3`
  multi-host (ADR-006). Code is identical in both.

## Consequences

- **Query-first data modeling.** P1 produces an access-pattern catalog;
  each table is designed for the queries that read it, and the partition
  key is chosen for even distribution and bounded partition size.
- **No joins in hot paths.** Data needed together is denormalized into
  one partition; one logical entity may be written to several tables.
- **Derived tables** (maintained by the writer or by an event consumer)
  answer secondary access patterns. Materialized views/secondary indexes
  only where their write amplification is measured and acceptable.
- **DuckDB (over Parquet exports) for offline aggregation** and ad-hoc
  analysis (P13); Scylla is never scanned for analytics on the hot path.
- **LWT (Paxos) only where genuinely required**: write-once evidence
  records and lifecycle state transitions that must be compare-and-set.
  Everything else is idempotent upserts keyed by deterministic IDs.
- **Counters** live in dedicated counter tables (Scylla requires it) and
  are treated as approximate; exact aggregates come from derived tables
  or DuckDB.
- Consistency levels are chosen per access pattern (e.g. `LOCAL_QUORUM`
  writes for evidence) and are identical at RF=1 and RF=3.
- Dev: one node, `--smp 2 --memory 1400M --reserve-memory 512M
  --overprovisioned 1 --developer-mode 1` inside a 2 GB container
  (see P0 design, *Resource budget*). Multi-node RF=3 is tested only on
  additional machines, never on the 15 GB dev host (plan B.4 #8).
- P0 implements no repositories; P2 does.
