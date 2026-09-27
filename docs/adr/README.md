# Architecture Decision Records

| ADR | Title | Status |
|---|---|---|
| [001](ADR-001-repository-layout.md) | Repository layout and logical boundaries | Accepted |
| [002](ADR-002-scylladb-durable-store.md) | ScyllaDB as the durable store | Accepted |
| [003](ADR-003-object-storage.md) | S3-compatible object storage (MinIO) | Accepted |
| [004](ADR-004-redis-streams-events.md) | Redis Streams as event transport, with an outbox | Accepted |
| [005](ADR-005-legal-compliance.md) | Legal / compliance posture | **Open** |
| [006](ADR-006-multi-host-topology.md) | Multi-host topology, host identity and time | Accepted |
| [007](ADR-007-contract-package.md) | Versioned cross-repository contract package | Accepted |

An ADR records one irreversible or expensive-to-reverse choice. Changing an
accepted ADR requires a new ADR that supersedes it.
