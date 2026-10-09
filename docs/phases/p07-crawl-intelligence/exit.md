# P7 Exit audit — requirement traceability and exit decision

Status: **P7 CLOSED (2026-10-09) under [D-22](decisions.md), approved by
the operator: implementation complete; WHEN and the dependent live M2
acceptance gates are explicitly deferred pending adequate seed-population
evidence.** This is a scoped closure, not a pass of every P7 requirement
or gate (§3). Nothing was collected, run or re-tuned for this audit or
the closure: no crawl, W1, M1b or M2.

Scope: every P7 requirement in [the phase plan](../../v2-phase-plan.md)
(§P7, B.1, B.3), the data-sufficiency gate G1–G8 ([audit.md](audit.md) §3)
and the M2 exit gate. It is mapped to code, tests, results and decisions.
The detailed reports are linked, not repeated.

## 1. Answer

**Decision (D-22, approved 2026-10-09): P7 is closed with an explicit
deferred gate.** That means
increments 1–3 are implementation-complete. WHEN, priority composition, the
CrawlTask/scheduler path and the M2 exit gate are **deferred, not
passed**. Every failed gate stays FAIL.

The plan does **not** allow P7 to exit without WHEN:

- The M2 exit gate includes "a page added under an already-crawled
  `/movies/` is found within its **scheduled recrawl window**". That needs
  WHEN.
- "≥ X % more new pages" needs a live A/B on a usable seed population
  (D-10, D-15).

So an exit needs a formal exception. D-22 is the smallest one:

- It changes no threshold.
- It passes no gate, and changes no recorded gate result.
- It waives no definition-of-done item: coverage was measured and passed
  (89.4 %, DoD-3).
- It reuses no held-out data (WHAT holdout, G5 labels).
- It moves the WHEN-dependent work to a reopen condition (§7).
- It permits only closing increments 1–3 and moving on to P8.

The project already has precedents for this:

- P4 was declared complete and frozen with its success gate OPEN (D-6).
- P7 started while P6 Gate G was open.

**Apart from WHEN-dependent work, no required P7 functionality is
missing.** Every NOT IMPLEMENTED row in §3 needs the same missing
evidence: novelty on hub revisits across independent publishers. P8 does
not consume P7 (§8).

## 2. What P7 delivered

| Increment | Delivered | Detail |
|---|---|---|
| 1 Source health + HOW (`source-health/v1`) | pure library: outcome classes, bounded per-host profile, lifecycle (unknown/active/degraded/moved/blocked/unreachable/error), probe-only backoff 6 h–7 d, reactivation on one success, http/browser starting capability; offline M1 replay | [implementation.md](implementation.md), [design.md](design.md) §4–§5, `crawler2/intelligence/sources.py` |
| 2 Projection + advice (ADR-023) | outbox reader with own checkpoint/generations/gap rebuild, V004 `intel_source_profiles` + `intel_projection_state`, `crawler2-intel sources run\|rebuild\|status\|report`, snapshot advice at P6 admission **off by default**, fallback = today's admission | design §9, `projection.py`, `store.py`, `advice.py`, `V004__crawl_intelligence.cql` |
| 3 WHAT (`what/v1`) | pure **offline** library: URL templates and roles, page kind (hub/detail/other), relevance (relevant/spam/unknown) with operator vocabulary; no table, consumer, setting or runtime caller | [what.md](what.md), `what.py`, `data/what_vocabulary.toml` |
| WHEN readiness (no increment) | offline audit tool and candidate rule `when-candidate/v0`; W1 proposed, reviewed and **not run** | [when.md](when.md), [when-w1-review.md](when-w1-review.md), `benchmarks/p7-intelligence/when_audit.py` |

## 3. Traceability matrix

Status vocabulary:

- **PASS:** evidence meets the requirement as written.
- **IMPL-NFV:** implemented, not fully validated.
- **FAIL:** measured, below the threshold.
- **BLOCKED:** blocked by data or operator input.
- **DEFERRED:** deferred by a recorded decision.
- **NOT IMPL:** not implemented.

The class column uses the brief's categories:

- **A:** core implementation.
- **B:** a live-data gate that cannot pass on this population.
- **C:** doable offline.
- **D:** a later phase.
- **E:** an operational or environmental limit.

"Live?" says whether new live collection is required.

The data-sufficiency gate was **not waived**. Implementation proceeded on
the existing M1 data by operator decision D-13, with the gate recorded as
NOT PASSED.

| ID | Requirement (plan, condensed wording) | Location | Evidence | Status | Remaining | Live? | Disposition |
|---|---|---|---|---|---|---|---|
| P7-1 | Audit: M1 history, which signals exist | audit.md §1–§5 | `results/m1-audit*.json`, `m1-reassess-seed.json` | PASS | — | no | closed |
| G1 | ≥ 20 seed domains with ≥ 30 attempts | audit §3, §5.3 | 11/20 | FAIL | seed refresh (D-15) | no¹ | B; stays FAIL |
| G2 | ≥ 20 domains seen in both windows | same | 10/20 | FAIL | as G1 | no¹ | B; stays FAIL |
| G3 | change-rate exposure (≥ 3 obs over ≥ 24 h) | same | 9 URLs / 500 | FAIL | as G1 | no¹ | B; stays FAIL |
| G4 | changes across page classes | same | proxy invalid (depth ≠ class) | FAIL | redefine on WHAT kinds when WHEN reopens | no¹ | B; stays FAIL |
| G5 | `page.changed` precision ≥ 0.8 | g5.md §4 | 0.070 / 0.015 weighted, 71 pairs | FAIL | none: a final negative result for raw changes (D-16) | no | B; labels held out (F-16) |
| G6 | outcomes for every capability | audit §5.3 | seed set HTTP-only; Tor absent | FAIL | browser-needing seed workload; Tor daemon | no¹ | B (browser), E (Tor) |
| G7 | targets | D-7 | none before P9 | DEFERRED | P9 `target.registered` | no | D |
| G8 | media baseline | audit §3.2 | 127 pages, 1 domain | PASS | — | no | closed |
| P7-3 | HOW fetch profiles: cheapest recent capability, escalation updates, decay re-tests cheaper path | design §4.4; `sources.py` | replay: 6,458 browser-first, 1,848 http attempts saved, 98 unnecessary browser starts; **off-target only** | IMPL-NFV | seed-set evidence impossible (no browser subset) | no¹ | B/E; frozen (F-9) |
| P7-4 | WHEN recrawl: per-page change rate, bounded intervals, boost by relevance/release | design §7; when.md | 6 candidate events vs 50 required, all on G5 hubs; W1 rejected (D-21) | BLOCKED | §7 prerequisites | yes, after seed refresh | B; deferred (D-22) |
| P7-5 | WHAT: URL template learning; page-type classification | what.md; `what.py` | holdout scored once (n = 144): kind 0.972; hub P/R 1.000/0.962; relevant P/R 0.977/0.804; spam P/R 1.000/0.548; 76 % decided. **No acceptance threshold was pre-set** | IMPL-NFV | runtime use needs its own design (F-11) | no | A done offline; runtime deferred with WHEN |
| P7-6 | temporal relevance (URL year, publish dates, target release dates) | what.md §4 | `{year}` and `published` features used in WHAT; target release dates deferred (D-7) | IMPL-NFV | target part after P9 | no | D (targets) |
| P7-7 | source profile + lifecycle | design §4.3 | health lifecycle implemented and replayed. Plan states `discovered/validated/high-value/stale` are **not** implemented: they need content-yield evidence | IMPL-NFV | content states with WHEN evidence | with WHEN | A done for health; rest deferred |
| P7-8 | negative knowledge | design §5.3 (pre-registered replay) | 37,798 of 247,104 attempts suppressed; 37,762.5 s (6.2 % of fetch time); 2 suppressed attempts had an actual outcome `ok` (`suppressed_ok`) | PASS (offline) | live effect never measured (advice off) | no | closed; frozen |
| P7-9 | productivity decay / diminishing returns | — | no code: needs per-source new-content yield on revisits, the evidence WHEN lacks | NOT IMPL | with WHEN | with WHEN | B; deferred (D-22) |
| P7-10 | reactivation on new target/event | design §4.3 | reactivation on a successful probe (unit tests, replay); target-triggered deferred (D-7) | PASS (non-target) | target part after P9 | no | D (targets) |
| P7-11 | single documented priority function | design §8 | none; its input `hub_rate` is WHEN's output (F-18) | NOT IMPL | with WHEN | with WHEN | B; deferred (D-22); P6 priorities stay |
| P7-12 | intelligence services as event consumers producing profile updates | design §9; ADR-023 | real-stack failure injection, gap rebuild, catch-up (validation §4–§5) | PASS | — | no | closed; frozen (F-10) |
| P7-13 | producing CrawlTasks; scheduler feeding P3's future-dated ZSET | design §3, when.md §9 | none; delivery path (P6 advice vs `crawl.requested` vs P3) undesigned by intent | NOT IMPL | WHEN design review | with WHEN | B; deferred (D-22) |
| P7-14 | replay tests on recorded history, deterministic | validation §3, §6, §7 | replay, `what_eval`, `when_audit` byte-identical under different `PYTHONHASHSEED` | PASS | — | no | closed |
| P7-15 | offline eval: new-content discovery latency vs M1 | — | needs WHEN/priority; unseen pages need a live A/B (D-10) | NOT IMPL | with WHEN / M2 | yes (A/B) | B |
| P7-16 | offline eval: fetch cost per new page vs M1 | — | as P7-15 | NOT IMPL | with WHEN / M2 | yes (A/B) | B |
| P7-17 | offline eval: % budget on dead/stale sources vs M1 | benchmarks.md §3 | seed 0.4 % suppressible (M1 already spent little on dead seeds); off-target 17.5 % | PASS | — | no | closed |
| M2-1 | ≥ X % more new pages/media, same seeds and budget | design §10 | X not set; needs a power calculation on a refreshed population; live A/B (D-10) | BLOCKED | seed refresh, X, A/B | yes | B; deferred (D-22) |
| M2-2 | HOW metric replacing "≥ Y % fewer browser fetches" (D-8) | design §10 | undefined: the seed set has no browser-requiring subset | BLOCKED | define on an escalating population, or record N/A for these seeds | no¹ | B |
| M2-3 | S1→S2: a new page under `/movies/` found within its scheduled recrawl window | — | needs a WHEN schedule | NOT IMPL | fixture test once WHEN exists | no | C after WHEN; deferred (D-22) |
| DoD-1 | design approved; validation doc written | design.md, validation.md | increments reviewed (D-17, D-18, D-19); WHEN/priority sections remain draft | PASS | — | no | closed |
| DoD-2 | contract + integration tests green | validation §6.1, §7.1 | `make check` 691 passed; storage + P7 integration 199 passed; P7 unit **146 passed at HEAD on 2026-10-09** | PASS | — | no | closed |
| DoD-3 | coverage of new code ≥ 85 % | §9 | measured 2026-10-09 over the `scripts/test-intelligence.sh` test set (177 passed): `crawler2/intelligence` **89 %** (1,152 statements, 113 missed, branch-aware); P7-added lines in `discovery/admission.py` 20 of 20 covered; P7-added lines in `discovery/cli.py` 0 of 12 covered; all P7 new code combined **89.4 %** (1,184 statements, 125 missed). The operator CLIs are untested: `intelligence/cli.py` is at 0 % | PASS | none for the gate; CLI tests would be optional hardening | no | closed |
| DoD-4 | no silent degradation; typed errors | design §9.2 table | malformed rows counted, CAS conflict re-read, failure → fallback (tests) | PASS | — | no | closed |
| DoD-5 | metrics emitted | design §9.6 | every `intel_sources_*` / `intel_source_*` name is present in code; never scraped in a live run | IMPL-NFV | observe in the next operator-approved run | no | E |
| DoD-6 | config documented, defaults justified | design §4.5, §9.4 | `intelligence.advice_enabled` off; parameter origins table | PASS | — | no | closed |
| DoD-7 | multi-host invariants, two-host topology | design §9.2 | checkpoint CAS loss unit-tested (`test_projection.py`); one operator-started instance; **no two-host run** | IMPL-NFV | P14 multi-host proof (plan F #2: hardware open) | no | E |
| DoD-8 | B.5 invariants (crawler runs with P7 stopped) | design §9.5 | fallback-equivalence and stopped-reader tests (validation §4) | PASS | — | no | closed |
| DoD-9 | integrate: wired behind config | validation §4 | advice wired, off by default, real-frontier test; never enabled in a live crawl. WHAT is not wired, by decision (F-11) | IMPL-NFV | optional bounded live check with advice on | optional | E |

¹ More runtime on the same seeds does not provide this evidence (§5). A
different population would. That is an operator decision (D-15), not
collection time.

**Counts (36 rows):**

| Status | Rows |
|---|---:|
| PASS | 13 |
| IMPL-NFV | 7 |
| FAIL | 6 (G1–G6) |
| BLOCKED | 3 (P7-4, M2-1, M2-2) |
| NOT IMPL | 6 (P7-9, P7-11, P7-13, P7-15, P7-16, M2-3) |
| DEFERRED | 1 (G7), plus the target parts of P7-6/P7-10 |

**Required work left for the exit:** none. D-22 was approved on
2026-10-09; the counts above are the final P7 record.

## 4. Implementation completion vs live-data validation

These are separate claims and must not be merged.

- **Implementation-complete:** increments 1–3 are built to their frozen
  designs. Their tests pass, and their offline evidence (replay, labelled
  holdout, failure injection) is recorded and deterministic.
- **Not validated live:** no P7 component has influenced a live crawl:
  - advice has never been enabled;
  - WHAT has no runtime caller;
  - WHEN does not exist.

  No measurement shows that P7 finds more pages or saves budget in
  production. M2 has not started (F-15).
- **The M1 data supports no general scheduling claim.** It contains one
  actively publishing film-site family (when-w1-review.md §2).

## 5. Why the seed population, not runtime, is the limit

Measured facts:

- **11 of 50 seed domains are live** (audit §5.1).
- **One family publishes films.** Of the 11 live domains:
  - one family (B: moviesda.business and its mirror tamilrockers2024.com)
    published films during or just before M1;
  - family A (3 domains) publishes SEO and casino posts only;
  - 5 domains are dormant (latest post 2024-10 to 2026-07);
  - 1 domain is unknown.

  Source: when-w1-review.md §2.
- **Too few candidates.** M1 has 172 hub revisits, 35 with a new-to-hub
  link, and 6 candidate events on 2 domains:
  - 5 of the 6 are on moviesda.business;
  - all 6 are on G5 hubs;
  - no hub has two events.

  Source: when.md §5.

What follows:

- **Families do not grow with time.** A longer run on these seeds adds
  revisits of the same single publisher, plus timeouts on dead domains.
  G1 and G2 would then pass numerically with no new knowledge
  (audit §5.3).
- **W1 cannot meet its own gate.** It needs ≥ 3 independent site
  families; mirrors count once. More days cannot produce families that do
  not publish.
- **The independence requirement stays.** Lowering the 0.8 precision
  threshold or the ≥ 3 independent-source requirement would make a pass
  meaningless. Neither is changed.

**Multi-day runs are not a P7 default.** The 4–5 day M1b plan (D-5,
audit §3.4, benchmarks.md §2) was rejected by D-12 and is history.
Nothing in P7 now requires a continuous multi-day run:

- The operator's laptop has power and network limits (stated 2026-10-09).
  A 24 h window is already fragile: P6 run 1 ended at 16.7 h, and run 2
  hit an 80-min Redis-full stall.
- A future WHEN collection must therefore **not depend on continuous
  uptime**. Its unit is a hub revisit pair with known exposure time, and
  the design must account for interrupted windows as exposure gaps.

That is a requirement for the future design (§7), not a validated method.

## 6. WHAT and WHEN status (summary; details linked)

**WHAT**

- **Built:** `what/v1`, implemented and frozen (F-11).
- **Population:** the template profiles were built over 20,966 extracted
  pages on 11 live seed domains.
- **Labelled sample:** 286 pages (dev 142 / holdout 144). G5 URLs were
  excluded and G5 labels were not read.
- **Holdout boundary verified in git:**
  - labels and vocabulary `d699b94`;
  - rules `2b8233f`;
  - scoring `0677dc8`;
  - none of these files changed afterwards.

  That rules were developed on dev only rests on the process record; git
  cannot prove it.
- **Limitations** (what.md §6):
  - 11 sites;
  - one labeller;
  - spam recall 0.548;
  - 24 % abstention;
  - "other" has n = 3;
  - SEO year-slug false positives;
  - no page regions in P5 links.
- **Holdout status:** consumed. A new policy version needs a new
  evaluation sample.

**WHEN**

- Not implemented; gated (D-16, F-17).
- G5 failed, and raw `page.changed` and plain URL novelty are rejected as
  signals.
- The M1 readiness audit is insufficient (D-20).
- W1 is reviewed and not run (D-21): the gate needs ≥ 3 independent
  publishing families, and these seeds have one.
- How a WHEN interval would reach the crawler is deliberately undesigned
  (when.md §9).

## 7. What reopening WHEN would require (not current P7 work)

These steps must happen in order. Each one is an explicit operator
decision.

1. **Seed refresh** (D-15, a P6 operator procedure). It needs ≥ 3
   independent site families that published film items in the last 30
   days, evidenced before any run. Mirrors count once
   (when-w1-review.md §10.1).
2. **A cheap offline check before collection.** Confirm the families from
   dated posts on the root or current archive, with single fetches. That
   is the "cheapest decisive step" in when-w1-review.md §9.
3. **A revised collection design** (when-w1-review.md §10.2–§10.6):
   - stratified hub selection;
   - targets fetched within 24 h;
   - evidence cards with the hub timeline and archived HTML;
   - gates counted by site family and distinct cluster, with split
     accounting;
   - a stop rule consistent with the 40 % cap;
   - reviewed `run.sh` switches (`M1_SEEDS`, no-admit);
   - **resumable in windows** (§5).
4. **A new labelled sample** scored against precision ≥ 0.8:
   - G5 labels held out;
   - WHAT holdout not reused;
   - `when-candidate/v0` fixed before labelling.
5. **Then a WHEN design review.** It must decide:
   - the estimator;
   - the delivery path (P6 revisit-gate advice, `crawl.requested`, or P3),
     under F-5 and F-8;
   - the as-of WHAT profiles (when.md §10.1);
   - the priority function.
6. **Then M2:**
   - X fixed from a power calculation;
   - the HOW metric defined (D-8);
   - a live A/B (D-10).

   M2 also depends on P6 Gate G-level stability (a run-3 window with no
   lost events).

## 8. P8 handoff

- **P8 can begin.** P8 (media registry) consumes `media.discovered` from
  P5 and emits `encode.requested`. It needs nothing from P7.
  - G8's baseline: 127 media pages on one seed domain. That is a thin
    fixture population.
  - P8's dedup benchmark is synthetic by plan.
- **What P8 must not assume:**
  - that P7 schedules or prioritizes anything;
  - that source advice is enabled;
  - that WHAT runs at runtime;
  - that the seed set is representative of active piracy sites.
  - that a new long-running crawl will be run for it; P8's gates are
    synthetic/fixture-based by plan.
- **P6 Gate G stays open.** Closing P7 does not close it (P6
  validation §4).
- **Frozen and unchanged by this audit:**
  - P1 contracts;
  - ADR-022 and ADR-023;
  - P2 publisher and relay;
  - P3–P6;
  - `source-health/v1`, V004 and `what/v1`;
  - F-1…F-20.
- **Shared constraints:**
  - P8's level-3 identity is the future answer to "two URLs, one film"
    (when.md §9). WHEN must not reimplement it.
  - Target-driven relevance and reactivation (G7, P7-6, P7-10) unblock
    only after P9.

## 9. Evidence checked in this audit (2026-10-09)

| Check | Command / source | Result |
|---|---|---|
| Working tree before edits | `git status --short` | clean; `main` 23 commits ahead of `origin/main`, unpushed |
| P7 unit tests at HEAD | `env/bin/python -m pytest tests/unit/intelligence -q -p no:cacheprovider` | **146 passed** in 9.4 s (= validation §7.1) |
| Replay figures | read `results/p7-sources-replay.json` | 247,104 attempts; 37,798 suppressed; 37,762.5 s; seed 124 / off-target 37,674 (= validation §2) |
| WHAT holdout ordering | `git log` on `labels.csv`, `what_vocabulary.toml`, `what.py` | as §6; no later edits |
| Metrics present | `grep intel_sources_ / intel_source_` in `crawler2/intelligence` | every name in design §9.6 present |
| Multi-instance safety | `tests/unit/intelligence/test_projection.py` (checkpoint CAS lost) | covered by a unit test; no two-host run |
| Coverage (DoD-3) | `coverage==7.16.2` installed into the session scratchpad only (`uv pip install --target`), not the project venv or dependencies. Then `PYTHONPATH=<scratch> env/bin/python -m coverage run --branch --source=crawler2.intelligence,crawler2.discovery -m pytest tests/unit/intelligence tests/unit/discovery tests/integration/storage/test_source_projection.py`, with the env of `scripts/test-intelligence.sh` (throwaway keyspace `crawler2_it_p7_host`). P7-added lines found with `git diff -U0 52a28c6 HEAD` | 177 passed in 125 s. `crawler2/intelligence` 89 %; P7 new code 89.4 % (125 of 1,184 statements missed); ≥ 85 % **met**. `settings.py` fields (declarative) not measured |

**Not run, deliberately:**

- `make check` and the integration tiers: this audit changes only
  documents;
- the replay, `what_eval` and `when_audit`: their outputs are committed
  and recorded as deterministic;
- W1, M1b, M2 and any crawl.

## 10. Known limitations and unresolved questions

- Coverage ≥ 85 % (B.3) is met at 89.4 %. Both operator CLI entry
  points, `crawler2-intel` and the P7 lines of `crawler2-discover`, have
  no test coverage. Earlier phases record no coverage figure.
- No two-host test (P14), and no live run with advice on.
- The HOW evidence is off-target.
- WHAT rests on one labeller and one population.
- The G1–G6 failures are final for this seed set. They are recorded, not
  waived.
- **Open, operator:**
  - when to refresh the seeds (D-15);
  - whether to delete the `m1:*` keys (D-14, still kept).
- **Open, design (deferred):**
  - the WHEN delivery path;
  - the priority function;
  - lifecycle content states (high-value, stale);
  - productivity decay.
