# P2 Audit — starting point for the storage layer

Date: 2026-09-28. Base: P1 commit `ae6c3bc` (P0 `7dc1913`), clean tree.

## Inputs read

| Input | What it fixes for P2 |
|---|---|
| P1 `access-patterns.md` | 38 query shapes; one table per shape; buckets for anything growing with time/popularity; LWT only E1/E2; outbox X1–X3 |
| P1 `events.md`, catalog | per-event idempotency keys (not event ids); at-least-once; producer ownership |
| P1 contracts | derived IDs (UUIDv8) and allocated IDs (UUIDv7) → stable, uniformly distributed shard keys; `BlobRef` = opaque URI + digest; `snapshot.digest == body_digest` |
| ADR-002/003/004/005/006 | Scylla, service-owned keyspaces; MinIO content-addressed; Redis Streams + outbox; legal retention open; no authoritative local state |
| P0 compose | Scylla 6.2.3, 1 node, `--smp 2 --memory 1400M`, RF=1; app containers 1 CPU / 512 MB, read-only root fs; host Python cannot reach CQL |

## Findings that shaped the design (verified on the dev node)

1. **ICS is unavailable** in Scylla 6.2 OSS (`Unable to find compaction
   strategy class 'IncrementalCompactionStrategy'`): choices are STCS/LCS/TWCS.
2. **NetworkTopologyStrategy keyspaces default to tablets, and tablets
   forbid LWT** (`LWT is not yet supported with tablets`). Keyspaces must be
   created with `tablets = {'enabled': false}` (ADR-012 §10).
3. **Future write timestamps are refused** (> 3 days, `restrict_future_timestamp`),
   which rules out the usual `MAX − t` "earliest wins" trick (ADR-012 §4).
4. `system.large_partitions` threshold is live-updatable
   (`system.config`), and the REST API (flush, major compaction,
   `max_row_size`) is reachable from inside the Scylla container → exact,
   server-side partition sizes for the benchmark.
5. The CQL batch limits are 128 KB warn / 1 MB fail → a 10 000-link batch
   cannot be one batch (outbox pattern B for links).
6. No S3 client is installed; the stdlib covers SigV4 (four calls).

## V1

V1 has no durable storage layer to port (plan A.2 D16); it was not used.

## GitHub / change management

`origin` = `github.com/DonDevil/crawler2`; `main` is 12 commits ahead of
`origin/main` (nothing is pushed automatically). One workflow
(`.github/workflows/ci.yml`: quality + compose jobs); no CODEOWNERS, issue
or PR templates. Convention: one focused conventional commit per phase
(`feat(contracts): p1 …`). P2 follows it and changes CI only through
`scripts/validate-stack.sh`, which the compose job already runs.
