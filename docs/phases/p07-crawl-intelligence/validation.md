# P7 Validation

Chronology: [audit.md](audit.md) §1–§4 (gate not passed) → §5
(reassessment; M1b rejected, D-12) → [g5.md](g5.md) (G5 FAIL, D-16) →
[design.md](design.md) → increment 1 ([implementation.md](implementation.md)).

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

## 3. Regression (2026-10-07, host)

| Tier | Result |
|---|---|
| `make check` (ruff, mypy, unit + contract) | **574 passed**, 116 skipped (integration needs the stack; the 33 new P7 tests are included) |
| `tests/unit/intelligence` | 33 passed (hypothesis 200 examples) |
| Replay determinism | two runs with different `PYTHONHASHSEED`: byte-identical `p7-sources-replay.json` |
| Storage / P6 integration tiers | not re-run: increment 1 changes no runtime code. Last run at `8921980` (X-8/X-11): storage 192 passed, P6 104 passed (1 Chromium test skipped) |
