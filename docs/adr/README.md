# Architecture Decision Records

| ADR | Title | Status |
|---|---|---|
| [001](ADR-001-repository-layout.md) | Repository layout and logical boundaries | Accepted |
| [002](ADR-002-scylladb-durable-store.md) | ScyllaDB as the durable store | Accepted |
| [003](ADR-003-object-storage.md) | S3-compatible object storage (MinIO) | Accepted |
| [004](ADR-004-redis-streams-events.md) | Redis Streams as event transport, with an outbox | Accepted |
| [005](ADR-005-legal-compliance.md) | Legal / compliance posture | **Open** |
| [006](ADR-006-multi-host-topology.md) | Multi-host topology, host identity and time | Accepted |
| [007](ADR-007-contract-package.md) | Versioned cross-repository contract package | Accepted (amended by 010) |
| [008](ADR-008-identity-scheme.md) | Identity scheme: typed, prefixed UUIDs; derived vs allocated | Accepted |
| [009](ADR-009-event-envelope-and-evolution.md) | Event envelope, ownership catalog and contract evolution | Accepted |
| [010](ADR-010-contract-package-scope.md) | Contract package scope and schema source of truth | Accepted |
| [011](ADR-011-fingerprinter-boundary.md) | crawler2 ↔ fingerprinter boundary and data ownership | Accepted |
| [012](ADR-012-scylla-storage-model.md) | Scylla storage model: query tables, convergent writes, bounded partitions | Accepted |
| [013](ADR-013-outbox-and-event-delivery.md) | Outbox, relay and consumer idempotency | Accepted |
| [014](ADR-014-object-store-layout.md) | Object-store key layout and integrity | Accepted |
| [015](ADR-015-frontier-execution-model.md) | Frontier execution model: execution queues, shared domain gate, eligible-domain index | Accepted |
| [016](ADR-016-frontier-task-lifecycle.md) | Frontier task lifecycle: temporary dedup, scheduling, retry authority, admission limits | Accepted |
| [017](ADR-017-fetch-runtime-boundary.md) | Fetch runtime boundary: one attempt per fetcher, outcomes as facts, frontier as the only retry | Accepted |
| [018](ADR-018-fetch-engine-retention.md) | Fetch engines retained: httpx + Playwright; Scrapling and Selenium dropped (D14) | Accepted |
| [019](ADR-019-frontier-domain-inflight-limit.md) | Frontier per-domain in-flight limit, global across queues and hosts | Accepted |

An ADR records one irreversible or expensive-to-reverse choice. Changing an
accepted ADR requires a new ADR that supersedes it.
