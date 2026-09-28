# P2 Validation

Date: 2026-09-28. Base: P1 `ae6c3bc`. Docker Engine 29.8.1 / Compose v5.5.1,
Scylla 6.2.3 (1 node, RF=1), MinIO `RELEASE.2026-08-04T00-00-00Z`, Redis 7.4.2.

## Results

| Check | Command | Result |
|---|---|---|
| Lint + format | `make lint` | clean |
| Types | `make typecheck` (strict; now also `benchmarks/p2-storage`) | no issues, 98 files |
| Host test suite | `make check` | **188 passed** (157 P0/P1 + 31 P2 unit), 31 integration skipped outside the stack |
| P1 contract suite | `pytest tests/contract` | 125 passed (unchanged) |
| Schema drift | `python -m antipiracy_contracts.schemas --check` | current (contracts untouched) |
| Requirements drift | `make requirements && git diff` | no change; no dependency added |
| Storage integration (real Scylla/MinIO/Redis), per host | inside `app`, `app-host-1`, `app-host-2` via `validate-stack.sh` | **31 passed** in each of `app`, `app-host-1`, `app-host-2` (29 storage + 2 P0 stack tests; 4–5 min each on this disk) |
| Concurrent schema bootstrap | `crawler2-storage migrate` on both hosts at once | one applies V001, the other waits on the lease and finds nothing pending; `check` passes |
| Concurrent writers | `multihost write` on both hosts (barrier-synchronised), `verify` | converged: 150 shared facts written by both hosts in different orders without duplicates, 60 conflicting observations resolved by observed_at (LWW) with earliest first_seen, 40 E1 races with one evidence id per match and identical views on both hosts, both worker identities in the outbox |
| Crash between commit and publication | `multihost crash-*` + `docker compose restart scylla` | passed twice (dev runs and the final `validate-stack.sh` run): 200 committed page observations with events; relay on host-1 killed after publishing part of a batch before marking; Scylla restarted; all 200 observations durable; host-2's relay delivered the rest; 209–222 deliveries in total, each observation applied exactly once by the idempotent consumer, 9–22 duplicates ignored. In the final run Scylla needed ~10 min to become healthy after the restart (commitlog replay on the HDD), longer than the script's 300 s timeout at the time; recovery was completed with the script's remaining commands and the restart wait is now 900 s |
| Partition gate (1M-page load) | `benchmarks/p2-storage/run.sh partitions` | **passed**: largest 43.4 MB (W6); see benchmarks.md |
| Latency at 10× per-host load | `benchmarks/p2-storage/run.sh latency` | **not met** on this host (USB HDD saturated); 1× medians sub-ms; see benchmarks.md |
| P0 stack checks | `validate-stack.sh` | healthy services, service-name connectivity, distinct host ids, scratch isolation, read-only root fs |

## Guarantees actually tested

| Guarantee | Test |
|---|---|
| fresh schema init, idempotent re-run, ordering, checksums, destructive DDL refused, tablet keyspace flagged, migrator lease | `tests/unit/storage/test_migrations.py`, `tests/integration/storage/test_schema.py` |
| every access pattern W1–W14, M1–M6, P1–P5, E1–E4 returns what was written | `test_web.py`, `test_media_projections_evidence.py` |
| same fetch attempt / page observation / media observation / link batch / projection event / evidence candidate twice → same state | same files (explicit replays) |
| LWW by fact time, not arrival order; earliest first_seen; highest version; retirement terminal; ready > failed | same files + multihost |
| LWT: one evidence per match under 8 concurrent proposals; write-once candidate; single seal | `test_evidence_lifecycle_is_compare_and_set`, multihost races |
| entity + outbox row committed together; payload/entity mismatch rejected with nothing written | `test_outbox_row_is_written_with_the_entity` |
| relay: byte-identical publication, crash after publish before mark → duplicates only, replay, shard spread, 14-day TTL | `test_outbox_objects.py`, `tests/unit/storage/test_events_unit.py` |
| real process kill (`os._exit(137)`) + Scylla restart → all committed events delivered, consumer applies each once | `validate-stack.sh` crash step |
| consumer dedupe by catalog key, incl. a retried producer with a new event id | unit + integration |
| derived rows rebuildable after a crash between authoritative and derived writes | `test_derived_rows_are_rebuildable_after_a_crash`, `rebuild_media` |
| object store: hashing, dedupe, wrong digest rejected, server-side payload check, no overwrite, verified reads, BlobRef round trip | `test_outbox_objects.py` (object-store section) |
| TTL on windows/markers, none on authoritative rows | `test_ttl_applies_to_windows_and_markers_but_not_to_authoritative_rows` |

## Exit criteria

| Criterion | Status |
|---|---|
| crawler2 keyspace, deterministic + idempotent init, versioned migrations | ✅ |
| tables derived from P1 patterns, no generic tables, service ownership | ✅ (schema.md; W15 deferred with reason) |
| partitioning, bucketing, compaction, TTL, consistency, LWT documented/justified | ✅ (schema.md, ADR-012) |
| RF=1 works; repository tests against real Scylla in Docker | ✅ |
| repository interfaces + implementations; contracts independent of Scylla | ✅ (contracts unchanged) |
| idempotent / duplicate / concurrent writes converge; LWW and LWT tested | ✅ |
| MinIO abstraction + implementation, hashing, digest verification, idempotent writes, BlobRef, layout, immutability, no extraction | ✅ (ADR-014) |
| outbox bucketed, P1 envelope, deterministic serialization, relay, crash recovery, safe replay, at-least-once, no exactly-once claim | ✅ (ADR-013) |
| consumer dedupe, bounded idempotency state, event-specific keys | ✅ |
| 1M-page partition benchmark, no partition > 100 MB | ✅ |
| 10× per-host load defined, p99 measured | ✅ defined and measured |
| **p99 targets met at 10× on the dev node** | ❌ **not met** — disk-bound (USB 5 400-rpm HDD at 99–100 % util); open until re-run on SSD/NVMe |
| no fake RF=3 result | ✅ RF=3 not validated; P14 entry criterion added to the plan |
| two-host profile, concurrent writes, shared state, scratch isolation | ✅ |
| quality gates, P0/P1 tests green, drift clean, V1 untouched | ✅ (nothing under `../crawler` written) |

## Known limitations

- **Latency gate open** (above). Remedy: put Docker's data root or the
  `scylla-data`/`minio-data` volumes on the NVMe drive, re-run
  `run.sh latency`.
- RF=3, node loss, batchlog replay after coordinator loss: not validated
  (no multi-node environment) — P14 entry criterion.
- LWT under contention on this disk needs retries (implemented, bounded:
  5 attempts); throughput of evidence writes here is ≈ 20 items/s.
- ADR-005 open: retention of authoritative data and raw objects is "keep";
  no deletion path exists yet.
