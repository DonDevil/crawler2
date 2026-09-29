# crawler2 architecture

Current-state architecture, by component. Decisions live in
[`../adr/`](../adr/); how each phase got here lives in
[`../phases/`](../phases/); the plan is [`../v2-phase-plan.md`](../v2-phase-plan.md).

## Where state lives

| Store | Holds | Authority | Loss tolerance |
|---|---|---|---|
| **ScyllaDB** (`crawler2` keyspace) | durable knowledge: URLs and domains ever seen, fetch attempts, page observations/versions, links, media, evidence metadata, outbox, processed-event markers | authoritative | none (RF=3 in production, ADR-002/-012) |
| **MinIO** | raw immutable objects (page snapshots, media artefacts) | authoritative for bytes | none (ADR-003/-014) |
| **Redis** | hot execution/coordination state: the frontier (what is executable now, leases, politeness gates), event transport (Streams) | **not** authoritative | bounded: lost frontier state is re-derived from Scylla by admission; events are re-published from the outbox |
| process memory / scratch | caches only | none | total (ADR-006: no authoritative local state) |

Rule of thumb: *durable knowledge → Scylla, executable state → Redis.*
"Seen historically" (Scylla) is not "currently scheduled" (Redis).

## Components (implemented so far)

| Component | Package | Doc |
|---|---|---|
| Configuration, identity, observability | `crawler2/core` | [P0](../phases/p00-foundations/what-was-built.md) |
| Contracts (IDs, models, events) | `contracts/` | [P1](../phases/p01-contracts/what-was-built.md) |
| Storage: repositories, object store, outbox/relay | `crawler2/storage` | [P2](../phases/p02-storage/what-was-built.md) |
| **Frontier & scheduling** | `crawler2/frontier` | [frontier.md](frontier.md), [P3](../phases/p03-frontier-scheduling/p3-frontier-scheduling.md) |

Workers (P4) claim from the frontier and write outcomes to Scylla; they
never touch Redis structures directly.
