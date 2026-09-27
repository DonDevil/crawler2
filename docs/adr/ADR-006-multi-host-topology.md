# ADR-006 — Multi-host topology, host identity and time

Status: Accepted (P0, 2026-09-28)

## Context

V2 must run on one machine and on N machines from day one, with no code
difference — only configuration (plan B.4). V1 kept authoritative state in
local files (blacklist mutated at runtime, SQLite), which cannot scale out.

## Decision

### 1. No authoritative local state

Anything that must survive a process restart or be shared between hosts
lives in **Redis** (coordination), **ScyllaDB** (durable memory) or the
**object store** (blobs). Local disk is only cache, temporary data,
scratch space or logs, and is safe to delete at any time. In the dev stack
this is enforced: app containers run with a **read-only root filesystem**;
the only writable paths are `/tmp` (tmpfs) and a per-container scratch
volume (`CRAWLER2_SCRATCH_DIR`). Shared config/rules (filter rules, fetch
profiles, seeds) live in Scylla, never in files a process writes.

### 2. Host identity and roles

- `host_id` (`CRAWLER2_HOST_ID`): stable per host, `^[a-z0-9][a-z0-9-]{0,62}$`
  (no `:`; it is part of the worker identity).
- `roles` (`CRAWLER2_ROLES`): the subset of capabilities a host runs:
  `http, browser, tor, intelligence, media_probe, finalizer, encoder`.
  `encoder` requires `limits.gpu_vram_mb > 0` (GPU hosts only; one
  GPU-owning encoder process per host). Workers arrive in P3+; P0 fixes the
  configuration surface so adding a role never changes code paths.
- **Worker identity** = `{host_id}:{role}:{pid}:{instance_id}` where
  `instance_id` is a random 128-bit hex value per process start (pid alone
  repeats across hosts and restarts). Every claim, event and evidence
  record carries it (`crawler2.core.identity.WorkerIdentity`).

### 3. Time

**Redis `TIME` is the authoritative clock** for everything distributed:
leases, heartbeats, schedules, recrawl due-times, backoff. Hosts' wall
clocks are used only for human-readable log timestamps and must never
decide lease ownership or ordering. (V1 already does this in its Lua
scripts; P3 ports it.) `crawler2-check` verifies Redis `TIME` is readable.

### 4. Deployment modes

| | Single host (dev) | Multi-host |
|---|---|---|
| App processes | one host, all roles | roles split per host by config |
| Redis | one instance (AOF on) | one primary; replica + Sentinel option later (P14). Frontier/lease scripts assume a single primary (atomic Lua). |
| ScyllaDB | 1 node, `SimpleStrategy RF=1` | ≥3 nodes, `NetworkTopologyStrategy RF=3`, `LOCAL_QUORUM` where required |
| Object store | MinIO single node | MinIO distributed (erasure coding) or any S3 |
| Test profile | compose `single` | compose `two-host` (two app containers, separate filesystems/host_ids, one shared backend set); multi-node Scylla only on extra machines |

## Consequences

- Single-host shortcuts (shared local files, pid-only identity, local
  clocks) break the `two-host` profile, which runs in CI.
- Redis is a single point of coordination; its HA is a P14 concern and
  must preserve Lua atomicity (no Redis Cluster key-spreading for frontier
  keys without redesign).
