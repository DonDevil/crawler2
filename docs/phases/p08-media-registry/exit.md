# P8 Exit — requirement traceability and exit decision

Status: **P8 COMPLETE (2026-10-09).** Every mandatory exit requirement
below is PASS; nothing was waived and no gate was lowered. Items marked
DEFERRED are assigned to later phases by the plan or approved decisions.

Scope: every P8 requirement of [the phase plan](../../v2-phase-plan.md)
(§P8 with PC-1/PC-2 applied, B.1, B.3–B.5), the approved design
([design.md](design.md) §10 increments and exit gate, §10.1 C-1…C-11) and
the decisions ([decisions.md](decisions.md)). Each row maps a requirement
to its implementation, the test or command, the observed result and a
status. Detailed evidence is in [validation.md](validation.md) (rows
I1-…, I2-…, I3-…, X-…); it is linked, not repeated.

Status values: **PASS**, **FAIL**, **BLOCKED**, **SKIPPED**, **NOT
IMPLEMENTED**, **DEFERRED** (assigned to a later phase by the plan or an
approved decision; not a P8 gate).

## 1. Answer

**P8 passes its exit gate and definition of done.** The synthetic dedup
benchmark gives 0 duplicate encode tasks and 0 false merges (X-1); C-1…C-11,
the probe/hash and encode suites pass on the real Scylla/Redis and on the
two-host profile; new-code coverage is 92 %; the end-to-end smoke suite
passes. The limitations in §6 are design-stated, not open gates.

## 2. What P8 delivered

| Part | Code | Operator interface |
|---|---|---|
| Resolver (level 1): `media.discovered` from the outbox → one claimed `MediaObservation` + `media.observed` per (page observation, media) | `crawler2/media/resolver.py`, `store.py` | `crawler2-media resolve run\|status\|accept-gap` |
| Probe pool (level 2): P4 media probe behind the SSRF guard | `probe.py`, `ssrf.py`, `work.py`, `schedule.py` | `crawler2-media probe run` |
| Hash reader (level 3): `full-bytes/v1` | `hashing.py`, `probe.py` | `crawler2-media hash run` |
| Encode requester + sweeper; representation projection | `encode.py` | `crawler2-media encode sweep`, `representations run` |
| Read-model repair and diagnostics | `store.py`, `cli.py` | `crawler2-media rebuild`, `show` |
| Storage | `cql/V005__media_registry.cql` (8 P8-owned tables) | `crawler2-storage migrate` |
| Exit benchmark | `benchmarks/p8-media/dedup_bench.py`, `run.sh` | results `benchmarks/p8-media/results/dedup-bench.json` |
| Docs | [architecture/media-registry.md](../../architecture/media-registry.md) (architecture, runbook), [ADR-024](../../adr/ADR-024-authoritative-outbox-readers.md), this phase directory | — |

## 3. Traceability matrix

### 3.1 Plan §P8 (with PC-1, PC-2)

| # | Requirement | Implementation | Test / command | Observed result | Status |
|---|---|---|---|---|---|
| P-1 | Registry consumes `media.discovered` | resolver (ADR-024) | C-1…C-11, I1-12 | every reference resolved once; crawl path unaffected with the registry stopped | PASS |
| P-2 | Level 1 `MediaId` = locator; a token URL is another locator (P8-D2) | P1 `Media.of` | I1-16 | distinct `MediaId`s; one locator on 2 domains = 1 `MediaId`, 2 sightings | PASS |
| P-3 | Probe for level-2 hints by a media-probe pool, not the crawler; SSRF guard ported (PC-1) | `probe.py`, `ssrf.py` | I2-1…I2-5 | 28 ported SSRF tests; redirect-to-private/loopback refused on both paths | PASS |
| P-4 | Full-content hash for level 3, bytes streamed and discarded, never stored (PC-1) | `hashing.py` | I2-6…I2-11 | SHA-256 of the served identity bytes; nothing written to MinIO or disk | PASS |
| P-5 | Level-3 equality is byte identity; a sampled match never shares a `ContentId` (PC-2) | `full-bytes/v1` | I2-8, X-1 | crafted collisions (equal length, equal first/middle/last 64 KiB) → distinct ContentIds | PASS |
| P-6 | `encode.requested` only for content identities lacking the required representation, learnt from `representation.ready` | `encode.py` | I3-1…I3-7 | suppression by the projection; one claim per content; streams applied once | PASS (interim spec rule, §6) |
| P-7 | Test: byte-identical video on 3 URLs → one level-3 identity, one encode | pipeline | I2-9, X-1 | 8 groups × 3 URLs → 1 ContentId and 1 claim/event each | PASS |
| P-8 | Test: same media in different encodings/packaging → distinct level-3 identities, not merged by P8 | `full-bytes/v1` | X-1 | 200k vs 900k H.264, MP4 vs Matroska vs WebM, fragmented vs progressive MP4: pairwise distinct; HLS 1 s / 2 s: registered and probed, no content key (P8-D6) | PASS |
| P-8b | … "later linked as equivalent by the fingerprinter (cross-checked in P10)" | — | — | not P8 scope | DEFERRED (P10) |
| P-9 | Test: crafted level-3 collision → not the same media | `full-bytes/v1` | I2-8, X-1 | 2 crafted pairs, distinct ContentIds and claims | PASS |
| P-10 | Test: same URL with changed content → new version | re-probe → re-hash → M6 | I2-14 | new ContentId + new M6 row; stale hash hidden meanwhile | PASS |
| P-11 | Test: SSRF suite ported | `tests/unit/media/test_ssrf.py` | I2-1, I2-2 | 28 passed | PASS |
| P-12 | **Exit gate:** zero duplicate encode tasks for byte-identical content in a synthetic dedup benchmark | `dedup_bench.py` | X-1 | **0** | PASS |
| P-13 | **Exit gate:** zero false merges in the different-content set | `dedup_bench.py` | X-1 | **0** (28 pairs, 0 shared ContentIds, 0 shared MediaIds, no P8 equivalence record) | PASS |

### 3.2 Design increments and C-1…C-11

| # | Requirement | Validation rows | Status |
|---|---|---|---|
| D-1 | C-1…C-10 on the real Scylla, injected clock, pause hooks | I1-1…I1-11 | PASS |
| D-2 | C-6 with k ∈ {1, 3, 5}: one `event_id`, byte-identical copies, M2/M3 = claim, checkpoint not past uncommitted work, eventual commit; physical rows recorded, not bounded | I1-6, I1-6b | PASS |
| D-3 | C-11 two-host lease handover mid-bucket | I1-18 | PASS |
| D-4 | Retention gap: halt, metric/alarm, non-zero status, `accept-gap`; no silent skip | I1-13 | PASS |
| D-5 | Scylla failure and recovery | I1-9 (container frozen) | PASS |
| D-6 | Registry stopped: crawl relay and main group unaffected | I1-12 | PASS |
| D-7 | Probe/hash: Range honoured/ignored, redirects (provenance), 403, non-media, oversize, timeout, unreachable, changed during read, content encoding, framing | I2-3…I2-11 | PASS |
| D-8 | Stale hash never attached; one probe/hash per media under concurrency | I2-14, I2-15 | PASS |
| D-9 | M5/M6 from hash facts; commute with the observation path; rebuild | I2-13, I2-16 | PASS |
| D-10 | Encode: two-host race → 1 claim; crash after claim → same `event_id`; ready/requested suppresses; contract fixtures for ready/failed | I3-1…I3-10 | PASS |
| D-11 | Metrics of design §10 (`media_reader_lag_seconds`, `media_reader_gap_total`, `media_observations_total`, `media_probes_total{status}`, `media_hashes_total{outcome}`, `media_hash_bytes_total`, `media_ssrf_rejections_total`, `media_encode_claims_total{result}`) | all exported (architecture doc); gap/invalid/SSRF/consumer metrics asserted in tests | PASS |

### 3.3 Definition of done (B.1, B.3) and invariants (B.4, B.5)

| # | Requirement | Evidence | Status |
|---|---|---|---|
| DoD-1 | Design doc approved; validation doc written | design approved 2026-10-09; [validation.md](validation.md) | PASS |
| DoD-2 | Contract + integration tests green | `make check` (unit + contract); `scripts/test-media.sh` 126 passed incl. the outage test; `make validate-stack` passed (§5). There is no hosted CI in this repository; the runs are local, as for P0–P7 | PASS |
| DoD-3 | Coverage of new code ≥ 85 % | X-3: `crawler2/media` 92 % | PASS |
| DoD-4 | No silent degradation: typed errors | `StorageUnavailableError` on Scylla/Redis failures (retried, never skipped), `IntegrityError` on a stored envelope that does not round-trip, `UnsafeDestinationError`; outcomes recorded for every probe/hash failure | PASS |
| DoD-5 | Metrics emitted | D-11 | PASS |
| DoD-6 | Config documented; defaults justified | `MediaSettings` + [architecture doc](../../architecture/media-registry.md) tables; probe/hash defaults are the approved Q2 assumptions, **not performance-validated** | PASS (stated) |
| DoD-7 | Benchmark gate met and recorded | X-1, [benchmarks index](../../benchmarks.md) | PASS |
| DoD-8 | Multi-host invariants (B.4) on the two-host topology | resolver handover, probe/hash workers on both hosts, concurrent encode claims (I1-18, I2-19, I3-9, I3-10) | PASS |
| DoD-9 | B.5 re-checked | §4 | PASS |
| DoD-10 | Integrate: wired behind config, end-to-end smoke suite still passes | P8 processes are operator-started CLIs (no compose service, like P7); smoke suite passed (§5) | PASS |

## 4. Architectural invariants (B.4, B.5)

| Invariant | Check |
|---|---|
| B.4 no authoritative local state | facts in Scylla; Redis holds only wants, leases, host limits; nothing on local disk |
| B.4 leases and rate limits in Redis on `TIME` | resolver lease (`now` from Redis `TIME`), per-media leases, per-host spacing/slots |
| B.4 idempotent handlers | claims, conditional markers, latest-wins/earliest-wins derived writes, `IdempotentConsumer` |
| B.4 LWT single-partition only | reader state, observation claims, encode claims — each one partition |
| B.5 #1 crawler never depends on P8 | no group on `media.discovered` (I1-12) |
| B.5 #2 no fingerprinter keyspace access | P8 writes only crawler2 tables; the fingerprinter learns P8 facts only from `encode.requested` |
| B.5 #3 derived data rebuildable | `crawler2-media rebuild` (I2-16) |
| B.5 #5 no contract change | P1 untouched; `full-bytes/v1` is a free versioned scheme name |
| P4 64 KiB media-probe invariant | P4 code untouched; asserted in I2-4 |
| P2 code/schemas | unchanged; V005 additive; `record_observation` used as is |

## 5. Final runs (2026-10-09)

| Run | Result |
|---|---|
| `make lint`, `make typecheck`, `make check` | clean; 774 passed, 156 skipped |
| `P8_SCYLLA_OUTAGE=1 scripts/test-media.sh` under coverage | 126 passed; `crawler2/media` 92 % |
| `scripts/test-media-two-host.sh` | C-11, probe/hash workers on both hosts, concurrent encode claims: all passed |
| `benchmarks/p8-media/run.sh` | gate PASS (0 duplicate encode tasks, 0 false merges) |
| `make validate-stack` | `make validate-stack` exit 0: integration suites 141 passed / 5 skipped in each of `app`, `app-host-1`, `app-host-2` (P8 suites included; the outage test skips without its flag); P2 concurrent writers converged; P2 crash + Scylla restart: 200 committed, 200 durable, 206 delivered, 200 applied once |

Nothing was crawled; the M1 export and run keyspaces were not touched.
Throwaway keyspaces: `crawler2_it_*`, `crawler2_p8_mh`, `crawler2_p8_bench`.

## 6. Known limitations (stated, not hidden)

- **Delivery** of `media.observed` and `encode.requested` is at-least-once:
  one logical event (`event_id`) per fact, with physical outbox copies
  unbounded under repeated crashes (one per minute bucket of a re-commit).
- **Downstream duplicates** remain possible: `processed_events` is a
  non-atomic check-then-insert (ADR-013); P8 does not fix it.
- **No strict lease fencing** of single observation writes (ADR-024): a
  stale owner may still win a claim; that claim is valid and committed
  identically by any reader.
- **Gap recovery is not lossless:** a retention gap halts the resolver
  until an operator accepts the loss; no backfill (F-2).
- **SHA-256 collision resistance** is assumed for level 3.
- **Not detected:** content changed with unchanged validators within
  `rehash_after_s` (30 d). Validators that differ between ranged and
  full responses of one server compare only where both are stated.
- **Bandwidth/size bounds** (one full transfer per media content version;
  8 GiB, 3,600 s) are approved assumptions, not measured on real media.
- **DNS rebinding** is not prevented (P14).
- **Per-host limits** are keyed by the locator's host; redirect targets
  are not limited separately. P4's probe sends no `Referer`.
- **Default spec identity:** suppression treats any ready representation
  as satisfying the request until P9 defines the default spec.
- **Manifests** get no content key (P8-D6); HLS/DASH dedup is a future
  reviewed design.
- Resolver throughput per reference is not measured.

## 7. Handoffs and deferred work

| To | Item |
|---|---|
| P9 | `encode.requested` re-request semantics after `encode.failed` (P8-D7, F-3); the default spec identity before the first `representation.ready`; P9 may acquire from any media of a `ContentId` (byte-identical); whether the fingerprinter's publisher applies ADR-022 to the `media-registry` group |
| P10 | level-4/5 equivalence across different ContentIds (P8 gives P10 no level-3 collision to split) |
| P14 | SSRF hardening of page fetching; DNS-rebinding pinning |
| later review | manifest/rendition content keys (P8-D6, F-5); outbox-gap backfill (F-2); verify-on-collision bandwidth optimisation (F-1, rejected for v1) |
