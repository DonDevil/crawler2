# P7 Decisions, open questions and deferred work

Audit: [audit.md](audit.md).

## 1. Decisions

| # | Decision | Date | Reason |
|---|---|---|---|
| D-1 | The data-sufficiency gate is evaluated on the **seed set** (the 50 registrable domains of the W52 seed file), with whole-data values shown for comparison only | 2026-10-07 | M2 is defined "on the same seed set and budget". 85 % of M1 history is NCBI/Scholar reached through irrelevant search results (audit §2.2) |
| D-2 | Thresholds G1–G8 are derived from estimator requirements (audit §3.1). They will not be lowered to make the gate pass | 2026-10-07 | plan §P7; task brief §4 |
| D-3 | **Gate result: NOT PASSED** (G1, G2, G3, G4, G6, G7 fail; G5 open; G8 passes). P7 implementation is stopped | 2026-10-07 | audit §3.2 |
| D-4 | The M1 history is exported read-only to `var/p7/m1-export` (git-ignored) and analysed offline. Nothing in `crawler2_m1` was changed | 2026-10-07 | M1 facts are evidence. Outbox and `*_by_domain_day` TTLs would otherwise expire run 1 (audit §1) |

| D-5 | **Q1 = A.** A new seed-focused M1 collection run of about 4–5 days, with a fresh dataset (keyspace, Redis namespace, bucket), search disabled, the same 52-line W52 seed file, and the static M1 revisit policy (6 h seeds / 24 h links). It starts only after X-8/X-11 are fixed and validated, and only with explicit approval. The run ends on the G1–G4 volume/coverage thresholds, not on the calendar. The earlier M1 history stays as evidence | 2026-10-07 (operator) | audit §3.3–§3.4 |
| D-6 | **Q2: the P4 §33a restriction is overridden for P7.** P4 stays complete and frozen for the current baseline. P7 neither reopens nor modifies P4 (its success gate, engines or `decide()`). P7 still has to satisfy its own data-sufficiency gate before implementation | 2026-10-07 (operator) | audit A-1 |
| D-7 | **Q3 = b (scope/dependency change, not a gate pass).** P7 v1 may learn HTTP profiles, and browser profiles where real escalation exists; change-rate recrawl, productivity, negative knowledge, decay, and reactivation from non-target evidence; URL patterns, page types and source lifecycle. **Deferred:** target relevance, release timing and target-triggered reactivation (until P9 produces `target.registered`), and Tor fetch profiles (until a Tor daemon and workload exist). G6 and G7 stay **FAIL** as recorded. The re-audit reports G6 for HTTP/browser and states Tor as deferred, and reports G7 as deferred until P9 | 2026-10-07 (operator) | audit §3.3 items 3–4 |
| D-8 | **Q4: the M2 browser criterion "≥ Y % fewer browser fetches than M1" is withdrawn.** Its replacement must measure HOW: browser use on the domains/path patterns where HTTP → browser escalation actually occurs, at equivalent successful-page coverage. Order: focused M1 history → identify the browser-requiring subset → measure its M1 baseline → define the metric and X/Y → record them **before** any final P7 evaluation. X/Y are never changed after P7 results are seen | 2026-10-07 (operator) | audit A-6 |
| D-9 | **Q5: G5 is a real quality check.** Label ≥ 50 changed page pairs from the focused run, covering several seed domains and path depths, real changes, template/widget changes and metadata/link-only changes. G5 passes only at precision ≥ 0.8. The labels are held out: no P5/P7 rule is tuned on them | 2026-10-07 (operator) | audit §2.4 |
| D-10 | **M2 evaluation:** offline replay evaluates ranking and scheduling over observed history only. Discovering pages M1 never fetched needs a controlled live A/B comparison (same seed set, comparable budget and period), with thresholds fixed before P7 results are observed | 2026-10-07 (operator) | audit A-7 |
| D-11 | Pre-collection hardening done: X-8 (consumer-aware, byte-bounded stream retention with publisher backpressure) and X-11 (relay retries in-process), ADR-022, commit `8921980` | 2026-10-07 | P6 decisions X-8, X-11, X-16 |

Until the re-audit passes for the implementable scope, **no P7
intelligence is implemented**: no scheduler, fetch profiles, lifecycle,
patterns, page types, priority, P3 integration or M2.

## 2. Questions as raised by the audit (answered in D-5 … D-10)

| # | Question | Options | Recommendation |
|---|---|---|---|
| Q1 | How should the missing history (G1–G4) be collected? | **A** a seed-focused M1 continuation for about 4–5 days: off-target search roots excluded by P6 operator rules (or search off), the frontier drained or a fresh M1 dataset, after X-8/X-11. **B** continue M1 with its old configuration (mostly adds NCBI history). **C** wait for P14 hardening first | **A**, after X-8 and X-11 (audit §3.4). Starting it needs your explicit go |
| Q2 | P4 §33a says P7 is not in scope while the P4 success-metric decision is pending | decide Option A/B for P4, or explicitly override §33a for P7 | decide P4 §33a first, or record the override here |
| Q3 | G6/G7 cannot be met by collection: piracy seeds don't use the browser, there is no Tor daemon, and there are no targets before P9 | **a** keep the gate as written: P7 waits for a browser-needing seed workload, Tor and P9. **b** decide at design review that P7's first increment learns http/browser profiles from the domains that exhibit them, and that target-driven temporal relevance and reactivation are deferred to after P9 (documented as a gap, not passed) | needs your decision. Option **b** changes the plan's P7 scope and has to be recorded as a plan change, not a gate pass |
| Q4 | M2's "≥ Y % fewer browser fetches" | in M1, browser use is 85 % NCBI and the seed set had 2 browser fetches, so Y on the seed set measures WHAT (not crawling NCBI) rather than HOW (fetch profiles) | redefine the browser criterion before any P7 run (e.g. browser fetches per new seed-set page, or measured on a workload that needs JS); X/Y set from the new M1 history after Q1 |
| Q5 | G5: label M1 change pairs | label ≥ 50 changed pairs from the seed set (bodies are in MinIO `crawler2-m1`) | do it with the Q1 data; the result decides whether P7 change rates need template-aware normalization (P5/P6 hook) first |

## 3. Deferred

| Item | Owner |
|---|---|
| P7 design (algorithms, storage, P3 integration, priority function, M2 X/Y) | P7, after Q1–Q4 |
| Target relevance, release timing, target-triggered reactivation | after P9 (`target.registered`) |
| Tor fetch profiles | needs a Tor daemon (environment) |
| Media-identity-based "new media" | P8 |
