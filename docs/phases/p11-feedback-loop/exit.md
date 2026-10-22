# P11 exit review — feedback loop (milestone M4)

Date: 2026-10-22. Evidence: [validation.md](validation.md). Design:
[design.md](design.md). Decisions: [decisions.md](decisions.md).

## 1. Status

**P11 IMPLEMENTED; M4 NOT PASSED.** The feedback loop is implemented,
wired into the real admission → frontier path (behind
`CRAWLER2_FEEDBACK__ENABLED`, default off) and demonstrated end to end on the
real Redis/Scylla stack with a deterministic replay: with the same budget, the
next cycle reached 15 matching media with feedback versus 2 without. M4 is
not declared passed because two links of the required chain are simulated —
the fingerprinter's encode/match (P10 M3 is NOT PASSED) and the network fetch
— and the feature is not enabled on any deployed stack. Passing M4 needs the
operator decision OD-2 (accept the simulated-boundary demonstration) or a
real-stack run once P10's matcher is accepted.

## 2. M4 gate table

| Criterion | Status | Evidence |
|---|---|---|
| Match-result feedback reaches source profiles | **PASS** | `match.found` consumer → B1 evidence per page host (unit attribution test; replay: alpha/beta evidence, no dud evidence) |
| Productive URL patterns / page types receive feedback | **PASS** | WHAT template + detail/listing kind on every row; pattern boost with ≥ 2-page support (unit; replay pattern boost 2) |
| Useful fetch-capability evidence affects future behaviour | **PASS** | alpha evidence `{browser}` only (its http 200s gave none); cycle 2 admits alpha on the browser queue with a 20 % http re-test share |
| Target relevance used where supported | **PASS** (bounded weighting only) | P2 projection from `target.*`; active/unknown/retired weights; no per-URL target ranking (design §11) |
| Feedback bounded and decays | **PASS** | bounds under 2,000 targets; alpha 58 → 53 → 51 → 50 at 0/14/30/60 d; neutral at 91 d |
| Deterministic replay shifts priority toward productive sources | **PASS** | priorities 58/58 vs 50; identical output across runs |
| Stability / no oscillation | **PASS** | 480 evaluations over 120 d: no order flip, monotone, pure |
| Effective priority consumed by real crawl scheduling/admission | **PASS** | real `Admitter` → `RedisFrontier` claim order; disabled/stale/error fall back to static priorities |
| Subsequent cycle discovers more matching media | **PASS in replay (simulated fetch + matcher)** | 15 vs 2 of 16 claims; no-match control identical allocation |
| Regression tests pass | **PASS** | 796 unit/contract; 42 stack-backed (schema, projections, discovery, P7 advice, P9 cross-service) |
| End-to-end with the real matcher and real fetches | **NOT MEASURED** | P10 M3 not passed; no live crawl in tests by policy |
| **M4** | **NOT PASSED** | see §1 |

## 3. Enablement steps (not performed)

`crawler2-storage migrate` (V006) → run `crawler2-intel feedback run` →
set `CRAWLER2_FEEDBACK__ENABLED=true` for `crawler2-discover`. The running
dev containers were **not** migrated or reconfigured (their image predates
V006). See the [operator guide](../../architecture/match-feedback.md).

## 4. Next

P12 — evidence collector & finalizer (M5) is the next planned phase; it is
the other `match.found` consumer and can reuse P11's attribution path.
