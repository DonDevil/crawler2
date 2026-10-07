# P7 Implementation

Design: [design.md](design.md). Decisions: [decisions.md](decisions.md) (D-17).

## Increment 1 — source health and fetch profile (implemented 2026-10-07)

| Module | Contents | Pure |
|---|---|---|
| `crawler2/intelligence/sources.py` | `OutcomeClass`, `classify`, `FetchFact`, adapters `fact_from_attempt` (P1 `FetchAttempt`, uses redirect hops) and `fact_from_export` (M1 export rows); `SourceProfile` + `fold` (bounded, commutative decayed weights); `assess` (lifecycle states, design §4.3); `capability` (HOW, §4.4); `advise` (admit / probe-only, next probe time); `SourcePolicy` `source-health/v1` (§4.5) | yes |
| `benchmarks/p7-intelligence/replay_sources.py` | pre-registered offline replay on the M1 export (design §5.3) | — |

**Not implemented** (by design, increment 2+): the P7-owned table, the
`fetch.completed` consumer, admission wiring, `crawl.requested`, WHAT,
WHEN, priority. Nothing in `crawler2/intelligence` is imported by a runtime
component. A test enforces that the package imports no frontier, storage,
worker, discovery, Redis or Scylla module.

Deviations from the design draft, found while implementing:

1. `min_evidence` is the decayed weight of 3 attempts within 12 h (2.21),
   not a literal 3. Attempts spread over hours never sum to exactly 3.
   design §4.5 was updated before the replay ran.
2. Bug found by the tests: `fold` failed on the first move target (a
   `None` comparison). Fixed.

## Tests

`tests/unit/intelligence/test_sources.py`: 33 tests.

- classification of every outcome class, including the same-site vs moved
  rule;
- both adapters;
- order independence and equal advice under shuffling (hypothesis, 200
  examples);
- bounded profile size over 5,000 facts;
- the wrong-host guard;
- every lifecycle state and its thresholds;
- dominance;
- probe-only scheduling, the 6 h minimum and 7 d maximum, reactivation by
  one success, ageing-out of negative evidence, query times before the last
  fact;
- browser-first, the 7 d http re-test and decay back to http;
- the policy constants;
- the import boundary.

## Increment 2 — source-profile projection and admission advice (implemented 2026-10-07)

Design: §9 (frozen after the review, D-18). [ADR-023](../../adr/ADR-023-projection-readers-outside-stream-retention.md).

| Module / file | Contents |
|---|---|
| `crawler2/intelligence/projection.py` | `SourceProjector`: the outbox reader (settled minute buckets, `fetch.completed` only, `fact_from_attempt`, bounded dedupe by idempotency key, fold, `through_bucket` guard, checkpoint CAS); generations on bootstrap, gap, policy change and operator rebuild; in-process retry; metrics. `ProjectionState`, `StoredProfile`, the lossless profile JSON codec, the `OutboxReader` and `SourceProfileStore` protocols |
| `crawler2/intelligence/store.py` | `ScyllaSourceProfileStore` over the P7-owned tables (LWT only on the state row) |
| `crawler2/intelligence/advice.py` | `source-advice/v1`: `SourceAdvice`, `SourceAdvisor`, `fallback()`, `SnapshotAdvisor` (background snapshot, staleness rules, per-process probe token; never raises, no I/O on `advise`), `load_snapshot` |
| `crawler2/intelligence/cli.py` | `crawler2-intel sources run / rebuild / status / report` |
| `crawler2/storage/scylla/cql/V004__crawl_intelligence.cql` | `intel_source_profiles` (TTL 30 d, STCS), `intel_projection_state` |
| `crawler2/core/configuration/settings.py` | `IntelligenceSettings` (`CRAWLER2_INTELLIGENCE__*`); `advice_enabled = false` |
| `crawler2/discovery/admission.py`, `cli.py` | optional `advisor` on `Admitter`, asked after filter, scope and the revisit gate, for http-queue URLs only; `Outcome.SOURCE_DEFERRED`; browser starting queue; any exception → fallback. The advisor is built only when enabled |

**Unchanged:** ADR-022, the publisher, the relay, stream settings, P1
contracts, P3, P4, P5, P6 filter/scope/revisit/priorities, increment 1
(`sources.py`).

**Not implemented** (by design): WHAT, WHEN, priority, `crawl.requested`,
target relevance, Tor profiles. No compose service was added. The reader
is started by the operator (`crawler2-intel sources run`, one instance),
and `crawler2-storage migrate` must run first (V004).

Deviations from the draft (design §9 before the review):

1. Outbox reader instead of an `intel-*` consumer group (ADR-023).
2. No LWT on profile rows. The single writer is guarded by `through_bucket`,
   and the state row's LWT detects a second writer.
3. `source_profile_moves` dropped. The latest target is in the profile, and
   the history is in W1.
4. `intel_source_profiles` has a TTL but stays size-tiered. The P2 schema
   test assumed every TTL table is time-series (TWCS). It now lists this
   upserted table as an explicit exception.

Defects found by the tests while implementing (test-side, fixed): a
projection test injected the crash at an empty bucket, and the first
integration test ran the reader's clock 15 min ahead, so it passed buckets
before they were written. That is the late-row case that settle exists
for, so the test now uses the real clock with settle 0 and waits for
bucket close.

## Tests (increment 2)

- `tests/unit/intelligence/test_projection.py` (19): bootstrap equals the
  pure fold; only settled buckets; other event types ignored; incremental
  cycles equal one cycle; duplicates across and within buckets; out-of-order
  facts; crash between profile write and checkpoint (no double fold); failed
  write retried, not deduped away; malformed and unsupported-schema rows;
  deterministic rebuild; gap → new generation from retained facts; policy
  change; operator rebuild mid-cycle (CAS conflict, then follow); storage
  outage retried in-process (outbox and state store); bounded profile size;
  exact JSON round trip; moved host; browser capability.
- `tests/unit/intelligence/test_advice.py` (15): fallback for no snapshot,
  never-run projection, stale snapshot, stale projection, rebuilding, policy
  mismatch, internal error, and a hanging store (advice in < 50 ms); empty
  projection = unknown → admit http; failed refresh keeps the old snapshot
  until it ages out; deferral of a dead host; one probe per interval;
  browser start; live host; the snapshot reads the current generation and
  policy only.
- `tests/unit/discovery/test_admission.py` (+4): P7 fallback (and a raising
  advisor) gives exactly the admissions without P7; blocked, recent and Tor
  URLs are not sent to P7; deferral and browser queue (priorities stay P6;
  a deferral is neither an admission nor a filter decision); P7 cannot
  admit what P6 blocked.
- `tests/integration/storage/test_source_projection.py` (3, real Scylla and
  Redis): failure injection (validation §4), the rejected-design contrast,
  and P7 → P6 → P3 on the real frontier with fallback.
- `tests/unit/intelligence/test_sources.py`: the import boundary now allows
  P2 storage for the projection, store and CLI. `sources.py` and
  `advice.py` stay storage-free, and no P7 module imports the frontier,
  workers, discovery or Redis.

