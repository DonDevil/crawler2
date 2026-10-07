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
