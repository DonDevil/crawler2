# P7 Validation

Chronology: [audit.md](audit.md) §1–§4 (gate not passed) → §5
(reassessment; M1b rejected, D-12) → [g5.md](g5.md) (G5 FAIL, D-16) →
[design.md](design.md) → increment 1 ([implementation.md](implementation.md)) →
increment 2 review (audit §6, D-18, ADR-023) → increment 2 → increment 3,
WHAT ([what.md](what.md), D-19).

## 1. Gate status (2026-10-07)

| Gate | Status | Note |
|---|---|---|
| G1 seed domains with ≥ 30 attempts | **FAIL** (11/20) | limit = seed availability (audit §5.3) |
| G2 seen in both windows | **FAIL** (10/20) | as G1 |
| G3 change-rate exposure | **FAIL**, capacity not provable | not needed for increment 1, which uses no change rate. It would matter for a per-page WHEN estimator, which the G5 result rules out on this population (design §7) |
| G4 changes across page classes | **FAIL**, proxy invalid | path depth is not a page class; WHAT defines page kinds (design §6) |
| G5 change precision ≥ 0.8 | **FAIL** (0.070 / 0.015) | WHEN stays gated (D-16) |
| G6 capability outcomes | **FAIL** for the seed set; Tor deferred | browser logic validated on off-target evidence only |
| G7 targets | deferred to P9 (D-7) | — |
| G8 media baseline | **PASS** | 127 pages, one domain |
| M2 | not started | prerequisites in design §10 |

No gate definition or threshold was changed. **M1b: not run. The old
`m1:*` Redis keys: not deleted.**

## 2. Increment 1 results

- **Unit tests:** `tests/unit/intelligence`, 33 passed.
- **Offline replay** (design §5.3, policy `source-health/v1` fixed before
  the run): `results/p7-sources-replay.json`, details in
  [benchmarks.md](benchmarks.md) §3.

| Population | M1 attempts | Suppressed | Of which blocked / unreachable / error / moved | Suppressed but would have answered | Fetch time saved |
|---|---:|---:|---|---:|---:|
| off-target (labelled) | 214,973 | 37,674 (17.5 %) | 35,676 / 477 / 872 / 646 | 2 | 6.7 % |
| seed set | 32,131 | 124 (0.4 %) | 0 / 121 / 0 / 1 | 1 | 1.6 % |

HOW (off-target evidence only): browser-first was advised 6,458 times. That
saved 1,848 http attempts that would have needed JS, at the cost of 98
browser starts where http would have answered.

**Reading.**

- Off target, the gain is almost entirely the 429 storm (X-14): 35,676 of
  50,306 blocked attempts are avoided, with 2 answers lost.
- On the seed set the gain is small, because M1's static cadence already
  spent few attempts on the 39 dead seed domains (3–26 each, at the 6 h seed
  interval, which equals the probe minimum).
- The seed-set waste that remains is elsewhere: spam pages on the live sites
  (WHAT), page-level 4xx (5,341 attempts), and frontier starvation by
  off-target work.
- The replay shows cost avoided and value lost on M1's own workload. It
  cannot show what the freed budget would have found (audit A-7).

## 3. Regression for increment 1 (2026-10-07, host)

| Tier | Result |
|---|---|
| `make check` (ruff, mypy, unit + contract) | **574 passed**, 116 skipped (integration needs the stack; the 33 new P7 tests are included) |
| `tests/unit/intelligence` | 33 passed (hypothesis 200 examples) |
| Replay determinism | two runs with different `PYTHONHASHSEED`: byte-identical `p7-sources-replay.json` |
| Storage / P6 integration tiers | not re-run: increment 1 changes no runtime code. Last run at `8921980` (X-8/X-11): storage 192 passed, P6 104 passed (1 Chromium test skipped) |

## 4. Increment 2 results (2026-10-07)

Gate status (§1) is unchanged: G5 FAIL, WHEN gated, M2 not started.
Increment 2 adds no new evaluation of P7's value. It makes source health
available at runtime and safe to switch on.

**Failure injection** (`test_a_stopped_p7_never_holds_back_the_event_path_and_catches_up`,
real Scylla and Redis, real relay and publisher, a 64 KiB stream budget,
the main `extraction` group on `page.observed`): **passed**.

| Step | Result |
|---|---|
| 1. P7 consumes normally | profiles of 2 hosts equal the pure fold of 16 facts |
| 2–4. P7 stopped; 80 more `fetch.completed` (more than the stream's byte budget) and 7 `page.observed` | relay `backpressure = None`; `fetch.completed` has **no group**; the main group read all 7 and has 0 pending; the ungrouped stream was trimmed (shorter than the facts produced); P7 state untouched |
| 9. No crawl data lost | every attempt is in `fetch_attempts` |
| 5–8. P7 restarts | catches up from the outbox; 3 profiles equal the pure fold of all 96 facts |
| gap: checkpoint set 10,000 buckets before retention | `new_generation = gap`, generation + 1, `gaps = 1`; the rebuilt profiles equal the pure fold |
| 10. Main consumers healthy | yes (step 2–4) |

**The rejected design, reproduced** (`test_an_ordinary_group_on_fetch_completed_would_stall_the_relay`):
an unread group `intel-sources` on `fetch.completed` and 120 facts →
the relay reports backpressure on that stream. **Passed** (the stall is
shown).

**P7 → P6 → P3** (`test_admission_follows_p7_and_falls_back_when_it_is_gone`,
real frontier): live host admitted on http, dead host `source_deferred`,
JS host admitted on the **browser** queue. With the P7 store failing, both
hosts are admitted on http. **Passed.**

**Invariants (design §9.5):** 1 and 2 by `test_p7_fallback_is_exactly_admission_without_p7`
and the advice fallback tests; 3 by `test_p7_cannot_admit_what_p6_rejected`
and `test_p6_decisions_come_before_p7_and_tor_is_not_asked`; 4 by the import
boundary and the integration test (no group); 5 by the rebuild/determinism
tests and the integration catch-up and gap.

**Latency** ([benchmarks.md](benchmarks.md) §4): P7 adds +1.2 to +2.2 µs
per URL when it falls back, and +23 µs informed (including a debug log line
per deferral), against 18.6 µs per URL for in-process P6 admission. A
hanging P7 store costs nothing (no I/O on the admission path).

## 5. Regression for increment 2 (2026-10-07, host)

| Tier | Result |
|---|---|
| `make check` (ruff, mypy, unit + contract) | **612 passed**, 119 skipped (integration needs the stack) |
| `scripts/test-intelligence.sh tests/integration/storage` (P7 unit + discovery unit + every storage integration test incl. P2 stream retention, outbox, schema V004, P5, P6 M1 loop, P7) | **139 passed** |
| `scripts/test-filter-discovery.sh` (P6 tier incl. the M1 fixture loop and the Chromium interception test) | **109 passed** |

**Not run:** M1b (rejected), M2, any collection. The old `m1:*` Redis keys
were not touched (tests use private stream prefixes and frontier
namespaces). `crawler2_m1` was not migrated or read. The tests use the
throwaway keyspace `crawler2_it_p7_host`.


## 6. Increment 3 (WHAT) results (2026-10-08)

Gate status (§1) is unchanged: G5 FAIL, WHEN gated, M2 not started. WHAT
is evaluated on its **own** labelled sample (what.md §3). It does not
re-score G5, and no G5 label was read.

**Method.** There are 286 seed pages (11 domains × 26), stratified and
deterministic, with the G5 URLs excluded. Hash split: dev 142, holdout 144.
The labels and the vocabulary were committed before any rule (`d699b94`).
The rules were developed on dev and frozen in `2b8233f`. The holdout was
scored once after that, with no change afterwards.

| Measure (holdout) | Value |
|---|---|
| page kind accuracy | **0.972** (n = 144) |
| hub P / R / F1 | 1.000 / 0.962 / 0.981 (n = 79) |
| detail P / R / F1 | 0.953 / 0.984 / 0.968 (n = 62) |
| other P / R / F1 | 0.750 / 1.000 / 0.857 (n = 3, too few to estimate) |
| relevance decided | 105 / 138 (76 %); `unknown` = abstention |
| relevant P / R / F1 | 0.977 / 0.804 / 0.882 (n = 107) |
| spam P / R / F1 | 1.000 / 0.548 / 0.708 (n = 31) |
| relevant details whose template is a content template | 43 / 43 |
| spam details inside a content template | 5 / 19 (2 of them called relevant) |

Dev numbers, which were tuned on, and the per-domain tables are in
what.md §5. **Per domain:** page kind is ≥ 0.83 on every domain.
Relevance abstains mostly on gofullmovies.com and stripemovies.com, whose
titles carry no years or vocabulary. The 2 wrong holdout relevance
decisions are on moviesda.com.in (an SEO "…for 2026" article) and
moviesda16.best (a defaced film URL). No single domain carries the result.

**Determinism.** `what_eval.py` output is byte-identical under
`PYTHONHASHSEED` 1 and 999. Hypothesis checks order and duplicate
independence of profiles and results. Every state is bounded by
`WhatPolicy`.

**Performance** ([benchmarks.md](benchmarks.md) §5). Over 20,966 pages,
building profiles for 27 hosts takes 3.2 s. `analyze_page` costs a median
of 268 µs and p99 4.5 ms (CPU only, I/O excluded).

**Readiness.** WHAT is ready to be the **input** on which the WHEN signal
is designed and validated: hub detection (holdout hub precision 1.0) and
the content-template links per hub. It is **not** production-ready:

- one population of 11 sites;
- a single labeller;
- spam recall 0.55;
- 24 % abstention;
- no runtime projection.

WHEN still needs its own labelled hub-revisit sample (design §7).

### 6.1 Regression for increment 3 (2026-10-08, host)

| Tier | Result |
|---|---|
| `make check` (ruff, mypy, unit + contract) | **672 passed**, 119 skipped (integration needs the stack). That is +60 WHAT tests over increment 2 |
| `scripts/test-intelligence.sh tests/integration/storage` (P7 + discovery unit, every storage integration test incl. the P7 projection) | **199 passed** |
| `scripts/test-filter-discovery.sh` (P6 tier) | not re-run: increment 3 changes no P6, P5 or runtime code (last run at increment 2: 109 passed) |

**Not run:** M1b (rejected), M2, any collection or crawl. MinIO
`crawler2-m1` was read only, for the label cards. `crawler2_m1` and the old
`m1:*` Redis keys were not touched. No migration and no setting were added.
