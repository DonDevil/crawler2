# to_complete.md — Anti-Piracy Crawler V2 outstanding-work register

Audit date: 2026-10-22. Audit-only session: nothing was implemented, run, installed,
configured, committed or deployed; this file is the only change. Method: read-only
inspection of `docs/`, ADRs, phase directories, code, compose, CI, benchmark results,
Git metadata and the fingerprinter repository (`../fingerprinter`, branch `p10-impl`).
No test was executed by this audit, so every "tests pass" below is **a recorded result
from a phase document**, not something re-verified today.

**Evidence tags** used throughout:
`[T]` proven by a recorded test/benchmark result in the repo · `[C]` claimed by a status
document, not independently re-verified · `[I]` inferred from code/config inspection ·
`[U]` unknown / evidence or resource unavailable · `[O]` reported by the operator in the task
brief but **not recorded in the repository**.

Paths are relative to `crawler2/` unless prefixed `fp:` (= `../fingerprinter/`).

---

## 1. Executive status

| Question | Answer | Basis |
|---|---|---|
| **Implementation-complete?** | **No — close for P0–P9, P11–P13; P7 is scoped, P10 and P14 are not.** P7 WHEN / priority / CrawlTask scheduler / productivity decay deliberately not built (D-22). P10 is "implementation complete" only in `fp:` branch `p10-impl` (30 commits ahead of `main`, **not pushed**, `fp:docs/phases/p10-vector-index-matcher/README.md`); crawler2 has no P10 record at all. P14 has **no implementation**: no worker/extraction metrics endpoints, no dashboards/alerts/tracing, no page-fetch SSRF guard, no backup tooling, no orchestration/supervisor package, no chaos or soak harness (`[I]`: `ls crawler2/`, grep for `serve()`, `ssrf`, `backup`). | `[T]`/`[I]` |
| **Functionally validated?** | **Partly, and only on one host with synthetic or limited data.** Strong evidence for P2 (RF=1), P3, P5 CPU, P6 gates A–F/H, P8, P9 (licensed real media, GPU), P10 gates M3-1/M3-2/T1/T2/T3/T5/T11 on a single host. Missing: any real matcher output flowing through P11/P12/P13 (all three validated with simulated/synthetic inputs), P7 live acceptance, real-data analytics export, anything across physical machines. | `[T]` per phase docs |
| **Milestone-accepted?** | **No milestone beyond the fixture-package part of M5 is accepted.** M1 (Gate G) **not passed**; M2 **deferred, not passed**; M3 **not passed** (T4 FAIL; T6/T8/T10 BLOCKED); M4 **not passed**; M5 **fixture-package review accepted `[O]`** — *not recorded in the repo*, which still says "awaiting owner review"; real-data M5 check still outstanding; M6 not started. | `[C]`+`[O]` |
| **Production-ready?** | **No.** No P14 work exists; production topology never exercised (single laptop, USB HDD, RF=1, Redis without auth/HA, MinIO root credentials in app env, no TLS); the matcher's `match.found` publication is **held** (calibration UNAVAILABLE), so the system cannot yet emit a real match event; ADR-005 (legal/evidence) is open. The compose file itself says "Development only; production deployment is designed in P14" (`docker-compose.yml:10`). | `[I]`/`[T]` |

**One-line truth:** the pipeline is built through analytics; what is *proven* is individual
components on one machine. What remains is (a) closing three upstream gaps that make the
real end-to-end chain impossible (P10 matcher gates, P6 Gate G disposition, P4 gate
methodology), (b) the whole of P14, and (c) a large block of owner decisions and hardware
that do not currently exist.

---

## 2. Phase and milestone status matrix

Status vocabulary: IMPL = implementation complete · TESTS = automated tests recorded as passing ·
VALID = acceptance criteria evidenced · ACC = accepted by the required reviewer · PEND ·
BLOCK · DEFER (by recorded decision) · N/A.

| Phase / gate | Implementation | Validation | Acceptance | Outstanding work | Blockers / evidence |
|---|---|---|---|---|---|
| **P0** foundations | IMPL, TESTS `[T]` | VALID: baselines, ADR-001–007 `[T]` (`p00-foundations/validation.md`) | ACC (ADR-005 allowed open) | CI only "partially verified" — workflow never shown to have run on GitHub; crawler2 is **40 commits ahead of origin/main** (`git status -sb`). | ADR-005 OPEN. `docs/README.md` is 0 bytes; no `docs/installation.md`. |
| **P1** contracts | IMPL, TESTS `[T]` | VALID `[T]` | ACC `[C]` | Re-verify cross-repo contract pin: `fp:requirements-p9.txt` pins contracts **1.1.0**; confirm P10 `match.found` 1.0 "held" envelope and P12/P13 consumers against the pinned version. | none structural |
| **P2** storage | IMPL, TESTS `[T]` (RF=1) | **PARTIAL**: 10× p99 latency gate **NOT MET** (USB HDD); RF=3 / node loss / batchlog **never run** | Accepted with two carried items `[C]` | RF=3 suite (TC-010…TC-019), latency re-run on non-HDD storage | no multi-node environment; no fast disk (NVMe is the owner's Windows disk — excluded) |
| **P3** frontier | IMPL, TESTS `[T]` | VALID: 1M-claim kill run 0 lost/0 dup; starvation `[T]` | ACC `[C]` | Redis HA/failover + absolute throughput on production Redis hardware (explicit P14 items, `p3-frontier-scheduling.md` §rows 559–560) | hardware |
| **P4** fetch | IMPL, TESTS `[T]` | **Exit gate OPEN**: success 95.51 % vs V1 96.82 % (`docs/benchmarks.md`); methodology decision §33a pending | **Not accepted** (frozen; P7 D-6 overrode §33a *for P7 only*) | Owner decision TC-007: accept status-aware redefinition (rerun V1+V2) / accept as-is / re-attempt. | OD only. Not formally closed anywhere. |
| **P5** extraction | IMPL, TESTS `[T]` | CPU gate PASS (17.9 %); change precision 1/23, 0.07 on seed pairs | ACC `[C]` | change-precision limitation handed to P6/P7 — resolved only by P7 deferral, not by a fix | — |
| **P6** filter/discovery → **M1** | IMPL, TESTS `[T]` | Gates A–F, H PASS; **Gate G (24 h, no lost events) NOT PASSED** (run 2 lost ~1,850 events, X-16) | M1 **not accepted** | Disposition of Gate G: X-8/X-11 fixed by ADR-022 (2026-10-07) but **run 3 never run**; Chromium-tree RSS drift "inconclusive"; X-14 (429 back-off), X-15 open | Run needs explicit operator go-ahead (no-runs rule). Laptop cannot sustain multi-day runs. |
| **P7** intelligence → **M2** | IMPL for inc. 1–3 (scoped), coverage 89.4 % `[C]` | G1–G6 FAIL, G7 DEFER, G8 PASS | **Closed under D-22 (scoped)**; M2 **DEFERRED, not passed** | WHEN, priority function, CrawlTask/scheduler path, productivity decay, S1→S2 scenario, live A/B | seed-population evidence (D-15 seed refresh); not retried |
| **P8** media registry | IMPL, TESTS, coverage 92 % `[T]` | exit gate PASS (0 dup encodes, 0 false merges, synthetic) | ACC `[C]` | equivalence cross-check (P10), DNS-rebinding pinning → P14; real-stack tiers not re-run after P9 | — |
| **P9** encoder (fp) | IMPL, 216 tests `[T]` | G-1,G-3,G-5,G-8 PASS; **G-2 FAIL → FP16 gated**; G-9 real multi-machine **DEFER→P14** | **ACC 2026-10-15 (scoped, with limitations)** `[C]` | limitations list `exit.md` §6 (FP16 gated; soak criteria 3/4 vacuous; partial truncation detection; DNS rebinding; replay re-publishes every event; P8 real-stack tiers not re-run) | not production-deployed |
| **P10** matcher (fp) → **M3** | IMPL (branch `p10-impl`, unpushed) | M3-1 PASS, M3-2 PASS, T1/T2/T3/T5/T11 PASS (single host); **T4 FAIL**, **T6/T10 BLOCKED**, **T8 BLOCKED**, N-SOAK baseline BLOCKED | **M3 NOT PASSED** (`fp:…/validation.md` §11) | owner decisions O-1…O-6; fast storage for E-R1; calibration data; `match.found` **publication held** | blocks M4-real, M5-real, P13-real |
| **P11** feedback → **M4** | IMPL, TESTS `[T]` | replay on real Redis/Scylla with **simulated fetch + matcher**: 15 vs 2 | **M4 NOT PASSED**; OD-2 open | real-stack run with real matcher; enablement (`CRAWLER2_FEEDBACK__ENABLED`) never done; V006 not on dev keyspace | P10/M3 |
| **P12** evidence → **M5** | IMPL, TESTS `[T]` | fixtures on real stack only; no real match | Fixture-package review **accepted `[O]`** (repo says "awaiting") ; **real-package check pending** | record the acceptance; OD-P12-1…7 (ADR-005); no screenshots/clips/WARC/object-lock/RFC 3161; V007 not on dev keyspace | P10 real output; legal inputs |
| **P13** analytics | IMPL, 923-passing suite `[T]` | gate PASS on synthetic + throwaway Scylla | Not a milestone; real-data **PEND** | `crawler2_m1` real export (OD-P13-1, refused by permission policy); real campaign report; clustering unevaluated; volume perf unmeasured | owner approval; P10 for campaigns |
| **P14** hardening → **M6** | **NONE** | none | not started | everything in §4 | hardware, decisions, P10 |

Sub-gates worth tracking separately: **P2-10×latency** (open), **P2-RF3** (not run), **P4-success** (open),
**P6 Gate G** (open), **P7 G1–G7 / WHEN / M2-live** (deferred), **P9 G-2 FP16** (gated by design),
**P9 G-9 multi-machine** (deferred to P14), **P10 T4/T6/T8/T10** (fail/blocked), **P11 OD-2**,
**P12 OD-P12-1…7**, **P13 OD-P13-1…5**.

### 2a. Discrepancies between documents and repository state

| # | Discrepancy | Evidence |
|---|---|---|
| X-1 | P12 documents say "M5 awaiting owner review"; the task brief says the fixture package was accepted. **No acceptance record exists in the repo** (git is clean; `p12-evidence-collector/exit.md`, `decisions.md` OD-P12-7, README, plan row all say awaiting). | `[O]` vs `[C]` |
| X-2 | crawler2 plan has **no P10 status row / decisions-log entry**, no `docs/phases/p10-*`, and ADR-028 ("proposed") lives only in fp. P10-D16 (move/link docs) is undecided. | `ls docs/phases`; `fp:…/decisions.md` |
| X-3 | Plan says Gate G "run 3 follows byte-bounded retention and relay retry (X-8, X-11)" and `p6-filter-discovery/validation.md` calls those "P14 hardening" — but ADR-022 closed X-8/X-11 on 2026-10-07. Run 3 was never scheduled; M1 stopped 2026-10-05 09:31 UTC and M1b was rejected, so "M1 continues as P7 history" is stale. | plan decisions log rows 2026-10-07 |
| X-4 | Plan P9 says target media/artifacts go to MinIO with Redis build-on-miss lock (13D fix). P9 audit A-5 says the 13D fix was an **opt-in shared directory**, default host-local. fp README row says multi-host = "SIMULATED MULTI-HOST only; REQUIRES MULTI-HOST VALIDATION". Whether P9/P10 actually moved target artifacts into MinIO/Scylla is **`[U]`** — must be verified in fp code before the multi-host proof. | `p09-…/audit.md:119,177–187`; `fp:README.md:248–249` |
| X-5 | Plan Part F #1 says ADR-005 "blocks the P12 design only"; P12/P13 retention, WORM, redaction and Tor policy now all depend on it. | plan Part F |
| X-6 | `docs/benchmarks.md` lists a P9 row "gates still not measured … superseded"; retained as history but easy to misread. | `docs/benchmarks.md` |
| X-7 | Plan lists `docs/installation.md` and a deployment story; none exists in crawler2 (fp has `docs/installation.md`, 201 lines). `docs/README.md` is an empty file. | `ls docs` |
| X-8 | CI runs `validate-stack.sh` for P0–P2 only; later `scripts/test-*.sh` tiers (crawlers, media, evidence, analytics, feedback, intelligence, fingerprinter-p9) are **not in CI**. | `.github/workflows/ci.yml` vs `scripts/` |

---

## 3. Outstanding-work register

Priority: **P0** = blocks other validation / critical path · **P1** = required for M6 · **P2** = required for completeness
· **P3** = hygiene. Categories: IMPL, TEST, VALID, INTEG, BENCH, SEC, OBS, DOC, OPS, DECISION.

### 3.1 Decisions and prerequisites (do first)

| ID | Work item | Cat. | Phase/gate | Pri. | Depends on | Definition of done | Evidence / status |
|---|---|---|---|---|---|---|---|
| TC-001 | **Hardware inventory** (plan Part F #2 is still open): which physical machines exist besides this laptop; ≥3 Scylla nodes; Redis host; MinIO drives (≥4 for distributed); network; a non-HDD volume | DECISION | P14, P2-RF3 | P0 | — | Written list of hosts/roles/disks/NIC/OS; go/no-go for RF=3 and for distributed MinIO | `[U]` — plan Part F #2 |
| TC-002 | Record M5 fixture-package acceptance (date, reviewer, scope = fixture package only; real-package check and ADR-005 items remain) | DOC/DECISION | P12/M5 | P0 | — | `p12-…/exit.md`, `decisions.md` OD-P12-7, README, plan row updated | `[O]` unrecorded |
| TC-003 | ADR-005 legal/evidence decision and OD-P12-1…6 (jurisdictions, screenshots/clips/WARC and permission to re-visit sources, retention/WORM, RFC 3161/signing, partial packages, personal data) | DECISION | P12, P14 backup/retention | P1 | — | ADR-005 closed or explicitly scoped; each OD answered or deferred with reason | open |
| TC-004 | P10 owner decisions **O-1** (index storage), **O-2** (level-5 recall target), **O-3** (calibration data / meaning of "verified"), **O-4** (N-SOAK V1 baseline), **O-5**, **O-6**, plus D12/D13(b) deferrals | DECISION | P10/M3 | P0 | TC-001 for O-1 | each marked decided in `fp:…/decisions.md` §6.1 | O-4 "owner: not now"; others open |
| TC-005 | Decide P10 doc/ADR location (P10-D16): move to crawler2 phase tree and ADR index, add plan row | DECISION/DOC | P10 | P2 | — | decision recorded | open |
| TC-006 | P6 **Gate G disposition**: run 3 (needs explicit operator go-ahead) vs move/replace the gate with the P14 soak | DECISION | P6/M1 | P1 | — | recorded decision in P6 decisions + plan | open (X-3) |
| TC-007 | P4 **success-metric methodology** (§33a Option A/B) or formal acceptance of 95.51 % vs 96.82 % | DECISION | P4 | P2 | — | recorded in P4 doc and plan | open since 2026-09-29 |
| TC-008 | P11 **OD-2**: accept simulated-boundary demonstration for M4, or require a real run | DECISION | P11/M4 | P1 | TC-004 | decision recorded | open |
| TC-009 | P13 OD-P13-1…5: approve read-only `crawler2_m1` export; retention/access; shared location; cadence; cluster review | DECISION | P13 | P2 | — | each answered | open |

### 3.2 P2 / RF=3 storage (P14 entry criteria)

| ID | Work item | Cat. | Phase/gate | Pri. | Depends on | Definition of done | Evidence / status |
|---|---|---|---|---|---|---|---|
| TC-010 | Provision 3-node Scylla, `NetworkTopologyStrategy` RF=3, **tablets disabled** (ADR-012), Scylla 6.2.3 (or documented upgrade) | OPS | P2→P14 | P0 | TC-001 | `DESCRIBE KEYSPACE` shows NTS RF=3, tablets off; `crawler2-storage migrate` + `check` green; non-HDD data disks | not available `[U]`; dev compose = 1 node RF=1 |
| TC-011 | Run the P2 storage suite (29 storage tests + 2 stack tests) against the cluster | TEST | P2 | P0 | TC-010 | all pass; command + log committed | command exists: `scripts/validate-stack.sh` (P2 steps) / `tests/integration/storage`; **never run on >1 node** |
| TC-012 | `validate-stack.sh` P2 steps against RF=3 — **script is hard-wired to the compose services** and the 1-node Scylla restart step; needs a parameterised cluster mode | INTEG/TEST | P2 | P0 | TC-010 | script (or new profile) runs against external contact points | `[I]` script uses `docker compose`; adaptation required |
| TC-013 | `benchmarks/p2-storage` (`run.sh partitions`, `run.sh latency`) at RF=3 over a real network; re-run the **10× latency gate** on non-HDD storage | BENCH | P2 | P0 | TC-010 | p99 table vs targets committed in `benchmarks/p2-storage/results/` | exists; 10× gate **not met** on HDD; RF=3 `[U]` |
| TC-014 | LOCAL_QUORUM read-after-write proof (write on node A, read via B/C, with one node down) | TEST | P2 | P1 | TC-010 | new multi-node test; **no such test exists** (existing tests prove RF=1 convergence only) | `[I]` missing |
| TC-015 | LWT (E1/E2) correctness under node loss: concurrent `evidence` proposals while killing a replica; one winner, no split | TEST | P2 | P1 | TC-010 | new chaos test; existing LWT tests are RF=1 (`test_evidence_lifecycle_is_compare_and_set`, multihost E1 races) | `[I]` missing |
| TC-016 | Outbox batchlog replay after **coordinator failure** (kill the coordinating node mid-batch; all outbox+entity rows eventually present) | TEST | P2 | P1 | TC-010 | new test; existing crash test kills the **relay** and restarts the single Scylla (200 obs) not a coordinator | `[I]` missing |
| TC-017 | Confirm `fetch→outbox` consistency levels documented in ADR-012 are what the code uses at RF=3 (LOCAL_QUORUM writes) | VALID | P2 | P1 | TC-010 | settings/CL audit + test | `[U]` |
| TC-018 | Partition-size gate re-check at RF=3 (1M pages) | BENCH | P2 | P2 | TC-010 | ≤ 100 MB, committed | passes at RF=1 (43.4 MB) |
| TC-019 | Fingerprinter keyspace on the same cluster (RF=3) — P9/P10 integration suites | TEST | P9/P10 | P1 | TC-010 | fp suites pass (P9 unit 100; P10 148 tests) | RF=1 only today |

### 3.3 Upstream correctness and integration (P10 and consumers)

| ID | Work item | Cat. | Phase/gate | Pri. | Depends on | Definition of done | Evidence / status |
|---|---|---|---|---|---|---|---|
| TC-020 | **P10-T4 level-5 equivalence recall** (equiv/v1 0.760, equiv/v2 0.933, 1 s spec 0.953; criterion 1.0; 0/12,420 false links) — resolve via O-2 | VALID/DECISION | P10/M3 | P0 | TC-004 | criterion met, or criterion amended by recorded owner decision | **FAIL** `[T]` (`validation.md` §11, §13.19, §13.27) |
| TC-021 | **P10-T6/T10 E-R1** capacity: index latency at 1.5 GiB RAM on SSD-class storage; `max_points` currently provisional 250,000 | BENCH | P10/M3 | P0 | TC-001, TC-004 (O-1) | gate-eligible E-R1 report passes; CAP-8 accepts it | **BLOCKED**; FAIL on USB HDD (p95 3,083 ms; 2.8–44.5 s at 4 GiB) |
| TC-022 | **P10-T8 calibration** + `match.found` publication: needs hard-negative labelled data (1 of 877 confirmed is false); decide meaning of confidence; release publication | VALID | P10/M3 | P0 | TC-004 (O-3) | calibration status != UNAVAILABLE on agreed data; K-8/K-10 gate opens; first real `match.found` published | **BLOCKED**; publication held `[T]` |
| TC-023 | P10-T7 N-SOAK V1 end-to-end baseline (≈ 22 h) **or** formally scope M3-1 to V1-on-V2-vectors | VALID/DECISION | P10/M3 | P1 | TC-004 (O-4) | run, or waiver recorded | BLOCKED |
| TC-024 | Push/merge `p10-impl`; fix P9 `publisher republish --stream <other>` (O-6); clean `fp:` working tree (`.vscode/settings.json` modified, untracked `benchmarks/results/bench_embedding_*.json`) | IMPL/hygiene | P9/P10 | P1 | TC-004 | branch merged, tags, tree clean; P9 defect fixed or waived | local only `[T]` (git) |
| TC-025 | P10 failure matrix on **real** hosts (G-14, V-1 simulated two-host today) | TEST | P10→P14 | P1 | TC-010, TC-040 | re-run T5/T11 subsets across machines | simulated only |
| TC-026 | **Real end-to-end chain**: seed → crawl → `media.discovered` → `encode.requested` → `representation.ready` → match → `match.found` → feedback (P11) → evidence (P12) → analytics (P13), on the real stack with real fetches and the real matcher | INTEG/VALID | M4, M5 | P0 | TC-020…TC-022 | recorded run; a real `match.found` becomes a collected, finalized, exported case with verified hashes; the P11 next-cycle effect measured | **never done** — every consumer used fixtures/simulation |
| TC-027 | M4 decision: after TC-026, pass M4 or record OD-2 acceptance | VALID | M4 | P1 | TC-026/TC-008 | M4 gate table updated | open |
| TC-028 | M5 real-data check: package from TC-026 reviewed by owner; ADR-005 declared gaps still listed | VALID | M5 | P1 | TC-026, TC-003 | owner acceptance recorded | open |
| TC-029 | P13 real-data export of `crawler2_m1`, reconciliation + run time + peak memory; then source report; later campaign report on real matches | VALID | P13 | P2 | TC-009; campaign part needs TC-026 | results in `p13-analytics/validation.md` | not run |
| TC-030 | Apply V006/V007 (and any later) migrations on the target deployment keyspace; enable `CRAWLER2_FEEDBACK__ENABLED` only after TC-026 | OPS | P11/P12 | P2 | TC-026 | migration applied; flag decision recorded | dev keyspace not migrated |
| TC-031 | Re-run P8 real-stack tiers after P9 (listed as not re-run) | TEST | P8/P9 | P2 | — | `scripts/test-media*.sh` + cross-service test green | `exit.md` §6 limitation |
| TC-032 | P7: decide whether to resume (seed refresh D-15 → WHEN → M2) or declare out of V2 scope; none of WHEN/priority/scheduler/decay/S1→S2 exists | DECISION/IMPL | P7/M2 | P2 | — | recorded as in-scope backlog with owner and plan, or V3 | deferred D-22 |
| TC-033 | P6 open fetch items X-14 (429/`Retry-After` back-off), X-15 (bulk non-HTML downloads), and in-process retry for the remaining consumers (extract, admit exit on Redis read error) | IMPL | P6→P14 | P1 | — | fix + failure-injection test | decisions.md lines 83–85 |
| TC-034 | F5/F6 rebuild by replaying `urls.discovered` — procedure documented, **not automated** | IMPL/OPS | B.5 #3 | P1 | — | tool + drill | `p06 decisions.md:88` |
| TC-035 | Rebuild procedures for every derived store with an actual drill: vector index (P10 drop/rebuild PASS, single host), analytics Parquet/graph, F5/F6, media read models (`crawler2-media rebuild`) | TEST/OPS | B.5 #3 | P1 | TC-034 | one rebuild runbook + timed drill each | partial `[T]` |

### 3.4 P14 build work

| ID | Work item | Cat. | Phase/gate | Pri. | Depends on | Definition of done | Evidence / status |
|---|---|---|---|---|---|---|---|
| TC-040 | **Physical multi-host deployment** (host A crawler pools, this RTX 2050 host GPU encoder, 3 Scylla, Redis, MinIO, fingerprinter) with per-host configuration, host_ids/roles, secrets | OPS/INTEG | P14 | P0 | TC-001, TC-010 | topology documented and running; B.4 invariants verified | none exists; compose is single-machine |
| TC-041 | Process supervision/orchestration per host (ADR-001 reserved `orchestration/` for P4/P14; package absent). Systemd units or equivalent for each role | IMPL/OPS | P14 | P1 | TC-040 | restart-on-failure with backoff for every role; documented | `[I]` no such package; relay/consumers rely on external restarts |
| TC-042 | **Redis HA** (replica + Sentinel) preserving Lua atomicity / lease semantics, or documented acceptance of a single primary with AOF; failover test | IMPL/TEST | P14 | P1 | TC-040 | failover drill: no lost/dup claims; typed `FrontierUnavailable` during gap | `ADR-006:51,60`; frontier not Redis-Cluster compatible |
| TC-043 | MinIO distributed mode if ≥4 drives/hosts, else documented single-node risk and backup plan | OPS | P14 | P1 | TC-001 | decision + deployment | `[U]` |
| TC-044 | Cross-host contract compatibility check (crawler2 ↔ fp contract package versions; compat fixtures in both CIs) | TEST | P14 | P1 | TC-024 | both CIs run the same compat fixtures at pinned 1.1.0 | pin exists; fp CI status `[U]` |
| TC-045 | Verify no authoritative host-local state in **both** repos on real hosts (delete scratch/cache on each host; system recovers) | TEST | B.4 #1 | P1 | TC-040 | scripted delete-and-recover per role | simulated only |
| TC-046 | Re-verify fp 13d findings (target artifact store, Redis lock, no host-local cache) on separate machines; resolve X-4 | VALID | P9/P14 | P1 | TC-040 | fp README row 249 changed from "SIMULATED" to validated, with evidence | `[U]` |
| TC-047 | Idempotency/different-host processing proof: same event handled by consumers on different hosts after redelivery (media resolver, evidence collector, feedback, encoder) | TEST | B.4 #5 | P1 | TC-040 | multi-host test results | two-host simulated tests exist (`multihost.py`, `media_multihost.py`) |

### 3.5 Observability

| ID | Work item | Cat. | Phase/gate | Pri. | Depends on | Definition of done | Evidence / status |
|---|---|---|---|---|---|---|---|
| TC-050 | Expose Prometheus endpoints in **every** long-running process (workers, extraction, media, evidence, feedback, analytics-if-scheduled, fp encoder/matcher/relay). `Metrics.serve()` is called only from `storage` and `discovery` CLIs | OBS/IMPL | P14 | P1 | — | each role exposes `/metrics`; scrape config | `[I]` grep; `p06 decisions.md:82` |
| TC-051 | Scrape/stack: Prometheus + Grafana (or equivalent) config committed; **dashboards per component** (crawler pools, browser, Tor, Redis, Scylla, MinIO, fingerprinter) | OBS | P14 | P1 | TC-050 | dashboards as code in repo; render with real data | **none exist** `[I]`: no config in repo |
| TC-052 | Exporters for Redis, Scylla (monitoring stack), MinIO, GPU (NVML/DCGM) and host resources | OBS/OPS | P14 | P1 | TC-040 | metrics visible per host | none |
| TC-053 | Define **SLOs/SLIs** (fetch success excluding blocked, queue age, event lag, outbox pending age, encode latency per minute of video, match latency, evidence finalize latency, storage p99) | OBS/DECISION | P14 | P1 | TC-050 | doc with targets and PromQL | none |
| TC-054 | Alert rules + **documented destination** (e.g. `outbox_late_rows_total`/pending age flagged by ADR-013), stalled work, DLQ growth, Redis memory, Scylla health, disk, GPU VRAM, worker restarts | OBS | P14 | P1 | TC-053 | rules tested by firing each once; destination owned | none |
| TC-055 | **End-to-end trace** `CrawlTask → … → EvidenceRecord`: today only a log `correlation_id` context var exists; no trace id on events/tasks; no cross-host propagation (also fp side). Decide: add trace id to event envelope (additive, B.5 #5) or reconstruct via ids | OBS/IMPL | P14 | P1 | TC-053 | one task traced through logs/spans across hosts; documented query | `[I]` `core/observability/logging.py` only |
| TC-056 | "Diagnose a stuck pipeline" runbook with queries for queue depth/age, retries, timeouts, event lag, per-stage counts | DOC/OBS | P14 | P1 | TC-050 | runbook validated by an injected stall | `crawler2-media resolve status`, `show` exist; no consolidated runbook |

### 3.6 Security

| ID | Work item | Cat. | Phase/gate | Pri. | Depends on | Definition of done | Evidence / status |
|---|---|---|---|---|---|---|---|
| TC-060 | **SSRF guard on page fetching** (http, Tor, browser incl. sub-resources and redirects) in crawler2. Only `crawler2/media/ssrf.py` exists (media probe/hash); no guard in `crawlers/` | SEC/IMPL | P14 | P0 | — | every fetch path resolves and validates destination; loopback/private/link-local/CGNAT/metadata addresses refused, typed failure | `[I]` grep over `crawler2/crawlers` finds none; P8 audit R-8 |
| TC-061 | **DNS-rebinding closure**: pin the connection to the validated IP (crawler2 media probe; fp acquirer) — explicitly "not solved" in `media/ssrf.py` and `fp:acquisition/ssrf_guard.py` | SEC/IMPL | P14 | P0 | — | connect-to-validated-IP with SNI/Host preserved; test with a resolver that flips answers | open in both repos |
| TC-062 | Redirect re-validation on every hop incl. cross-service (media probe has per-hop guard; verify fp acquirer, browser navigation, HLS/DASH manifest sub-fetches) | SEC/TEST | P14 | P1 | TC-060 | adversarial redirect suite in both repos | partial `[C]` |
| TC-063 | Adversarial SSRF test suites (redirect to 127.0.0.1/169.254.169.254, IPv6-mapped, decimal/octal IPs, DNS flip, `file://`, `gopher://`) — crawler2 page path and fp | SEC/TEST | P14 | P1 | TC-060/061 | suites in CI | P8 ported a suite for media only |
| TC-064 | Remove `CRAWLER2_*ssrf_allowed_networks` risk: assert empty in non-dev environments (setting exists, "fixtures/tests only") | SEC/IMPL | P14 | P2 | — | startup refuses non-empty when `environment=prod` | `[I]` settings.py:471 |
| TC-065 | **Secrets management**: no Redis auth, no Scylla auth, app uses MinIO **root** credentials (`docker-compose.yml:44–45`); `.env.example` has a placeholder; need per-service least-privilege MinIO users, Redis ACLs, Scylla roles, secret store/injection, rotation procedure; scan repo/images/logs for secrets | SEC/OPS | P14 | P1 | TC-040 | no root creds in app env; rotation drill; secret scan clean | `[I]` |
| TC-066 | TLS/inter-node encryption for Scylla, Redis, MinIO and client→server; **inter-service authentication** between crawler2 and fp (today: shared Redis streams) | SEC | P14 | P1 | TC-040 | documented and tested | none |
| TC-067 | **Network isolation** for browser and Tor pools (no route to Redis/Scylla/MinIO admin or internal subnets except via defined ports), **egress controls**; no Tor service defined in compose (`tor.py` resolves a proxy only) | SEC/OPS | P14 | P1 | TC-040 | firewall/VLAN/compose-network rules + test that a worker cannot reach internal ranges | `[I]` |
| TC-068 | Evidence artefact safety: object lock/WORM or equivalent, access control for evidence bucket, redaction policy for personal data, safe handling of hostile HTML/media in reports (HTML report escaping tests) | SEC | P12→P14 | P1 | TC-003 | policy + tests | OD-P12-3/-6 open; HTML escaping test status `[U]` |
| TC-069 | Dependency/container scanning (pip-audit, image scan, base-image pins) — only if existing policy requires; none specified in plan | SEC/DECISION | P14 | P3 | — | policy decision; scan in CI | no policy found `[U]` |

### 3.7 Chaos, soak, backup, runbooks

| ID | Work item | Cat. | Phase/gate | Pri. | Depends on | Definition of done | Evidence / status |
|---|---|---|---|---|---|---|---|
| TC-070 | Chaos suite implementing the matrix in §4.6 (no unified suite exists; pieces are scattered across P3/P4/P7/P8/P9/P10) | TEST | P14 | P1 | TC-040 | scripted, repeatable, results committed; typed failures asserted | `[I]` |
| TC-071 | **Soak definition** (plan only says "7-day soak test"): workload, fixed seed set, campaign/target config, topology, throughput expectation, pass/fail numbers, artifact location, who may start it | DECISION/DOC | P14/M6 | P0 | TC-001, TC-026 | approved written spec before start | **no definition anywhere** `[I]` |
| TC-072 | Soak harness: resource sampling (RSS/CPU/disk/GPU VRAM/Redis memory/Scylla/queues/browser memory/restarts), lost-work and duplicate-work reconciliation (task→page→media→representation→match→evidence), event-lag and stuck-task detection | IMPL/TEST | P14 | P1 | TC-050, TC-071 | harness dry-run passes on 1 h | pieces: `benchmarks/p6-m1/monitor.py,check.py`, fp soak harness; not unified |
| TC-073 | **7-day soak run** (≥ 168 h, continuous, on the physical topology) | VALID | M6 | P1 | TC-040…TC-072; operator go-ahead | M6 soak criteria met; rerun after any code fix | never run; longest runs 24 h (P6) and 2.8 h (P9 SOAK-1000) |
| TC-074 | Backup procedures + tooling: Scylla snapshots (both keyspaces), MinIO evidence/raw buckets + versioning, Redis (AOF/RDB per recovery model), schemas/migrations/contracts/config/secrets, cross-store reference verification | OPS/IMPL | P14 | P1 | TC-040, TC-043 | commands + schedule + retention committed | **nothing exists** — grep for backup/restore tooling finds none |
| TC-075 | Define RPO/RTO and recovery order (Scylla → MinIO → Redis derived → vector index → analytics) | DECISION/DOC | P14 | P1 | TC-074 | numbers recorded | none |
| TC-076 | **Restore drill**: restore into an empty environment; verify evidence resolves to artefacts with matching SHA-256 (`SHA256SUMS`, manifest digest), E1–E9 consistent, derived stores rebuilt, pipeline resumes | VALID | M6 | P1 | TC-074/075 | drill report committed; time vs RTO | never run |
| TC-077 | Runbooks (see §4.9) | DOC/OPS | P14 | P1 | TC-040 | each validated by executing it | partial per-component operator guides in `docs/architecture/*.md`; no consolidated set |
| TC-078 | Storage capacity/retention plan (Scylla TTLs, MinIO growth for raw snapshots, Redis memory ceilings, Parquet exports) | OPS/DECISION | P14 | P2 | TC-003 | capacity model + alerts | retention "keep forever" pending ADR-005 |

### 3.8 Documentation / repository closure

| ID | Work item | Cat. | Pri. | Definition of done |
|---|---|---|---|---|
| TC-090 | Apply every correction in §6 | DOC | P2 | docs consistent |
| TC-091 | Push or otherwise preserve: crawler2 40 unpushed commits; fp `p10-impl` (30 commits) | OPS | P1 | remote holds all work; CI executes |
| TC-092 | Extend CI to run later tiers that can run without GPU/licensed data (`test-media*.sh`, `test-evidence.sh`, `test-analytics.sh`, `test-feedback.sh`, `test-intelligence.sh`) and the contract-compat suite against the fp pin | TEST | P2 | CI matrix green |
| TC-093 | Coverage (≥ 85 % new code) evidence for P11, P12, P13 and P10 (not found in their exit docs) | TEST | P2 | recorded numbers |
| TC-094 | `docs/installation.md` (crawler2), non-empty `docs/README.md`, deployment/rollback docs | DOC | P1 | exist and followed once from a clean host |
| TC-095 | Verify generated artefacts are not committed: `var/` is ignored (contains `p12-fixture-case*`, M1 data); fp untracked benchmark JSON referenced as "uncommitted" in `docs/benchmarks.md` | DOC | P3 | decision per file |

---

## 4. P14 / M6 production-hardening checklist

Status words: **implemented / tested / validated / blocked / unknown / absent**.

### 4.1 P2 RF=3 storage prerequisites

| Item | Exists? | Status | Exact missing proof |
|---|---|---|---|
| NTS RF=3, tablets disabled | Config path exists (ADR-012, `crawler2-storage migrate` flags tablet keyspaces) | **absent** on a real cluster | 3-node cluster (TC-010) |
| P2 storage suite | `tests/integration/storage` (29 tests) | **tested at RF=1 only** | pass log at RF=3 (TC-011) |
| `validate-stack.sh` P2 steps | yes, compose-only | **tested at RF=1** | parameterised run (TC-012) |
| `benchmarks/p2-storage` | `run.sh partitions|latency` | partitions PASS RF=1; **latency 10× FAIL (HDD)** | re-run at RF=3 on real network/storage (TC-013) |
| LOCAL_QUORUM read-after-write | no multi-node test | **absent** | TC-014 |
| LWT E1/E2 under node loss | RF=1 LWT tests only | **absent** | TC-015 |
| Outbox batchlog replay after coordinator failure | relay-kill + single-node restart only | **absent** | TC-016 |
| Latency targets over real network | — | **blocked** | TC-013 |

### 4.2 Physical multi-host proof

| Item | Status | Notes / missing proof |
|---|---|---|
| Code/config for roles and host_ids | **implemented, tested simulated** (`CRAWLER2_HOST_ID`, `CRAWLER2_ROLES`, worker identity; `two-host` compose; `test-media-two-host.sh`; P9 TH-1…TH-8 host processes) | simulated ≠ physical |
| Crawler pools on host A | **unknown** — no second machine | TC-001/TC-040 |
| GPU encoder on RTX 2050 host | single-host GPU **validated** (P9 G-5 6.59×, SOAK-1000) | needs cross-host run with Redis/Scylla/MinIO remote |
| 3×Scylla, Redis, MinIO | **absent** | TC-010/042/043 |
| Cross-host contract compatibility | pinned 1.1.0; compat fixtures exist | cross-host run TC-044 |
| No authoritative host-local state | **tested simulated** | TC-045 |
| Idempotency on different hosts | simulated | TC-047 |
| Resources not currently available | additional hosts (≥1 for crawler, ≥3 Scylla incl. non-HDD disks), network, ≥4 MinIO drives for distributed mode, a fast-disk volume for the vector index | see §7 |

### 4.3 Fingerprinter re-verification

| Item | Status |
|---|---|
| 13d findings (host-local target cache, Redis-authoritative targets, per-job re-embed) | P9 claims group-less intake, durable intents, runtime profiles fail-closed; **whether target artifacts moved off shared-directory/host-local is unverified (X-4)** → TC-046 |
| P9 limitations to preserve | FP16 gated (G-2 FAIL), soak criteria 3/4 vacuous, partial truncation detection, DNS rebinding, replay re-publishes every event, real multi-machine deferred, P8 real tiers not re-run (`p09 exit.md` §6) |
| P10 | single-host evidence only; real multi-host = P14 |
| Contract | consistent pin 1.1.0; must be re-checked after merging `p10-impl` |

### 4.4 Observability, SLOs, alerts, tracing

| Capability | Status |
|---|---|
| Structured JSON logs + correlation_id context var | **implemented** `[I]` |
| Prometheus metric objects (storage, relay, consumers, extraction, media, evidence…) | **implemented**; exposure via HTTP **only** in `storage` and `discovery` CLIs |
| Per-component dashboards | **absent** |
| Metrics for browser/Tor/Redis/Scylla/MinIO/fp | partial in-process metrics; **no exporters** |
| SLOs/SLIs | **absent** |
| Alert rules + destination | **absent** (ADR-013 explicitly defers alerting on `outbox_late_rows_total`) |
| Error/queue/retry/timeouts/stall/resource metrics | partially instrumented; unverified against a dashboard |
| Trace CrawlTask→EvidenceRecord | **absent** (log context only) |
| Cross-host propagation | **absent** |
| Failed/stuck diagnosis procedure | **partial** CLIs (`crawler2-media resolve status|show`, `crawler2-check`), no runbook |

### 4.5 Security

| Requirement | Implementation | Adversarial tests | Deployment config |
|---|---|---|---|
| SSRF — media probe/hash (crawler2) | present (`media/ssrf.py`, per-hop guard) | ported suite `[C]` | allowlist setting exists |
| SSRF — page fetch http/Tor/browser | **absent** | absent | — |
| SSRF — fp acquirer | present (`fp:acquisition/ssrf_guard.py`) | `test_acquisition_ssrf.py` (280 lines) | — |
| DNS rebinding / IP pinning | **absent in both** (documented limitation) | absent | — |
| Redirect validation | media: yes; page fetch: no | partial | — |
| Secrets management | placeholders only; root MinIO creds in app env; no Redis/Scylla auth | none | **absent** |
| Rotation / least privilege | **absent** | — | — |
| Tor/browser isolation, egress controls | **absent** (compose has one flat `backend` network, no Tor service) | none | — |
| Inter-service auth / TLS | **absent** | none | — |
| Evidence safety (WORM, access, redaction) | URL redaction + SHA-256 only | partial | object lock absent |
| Dependency/container scanning | no policy found | — | — |

### 4.6 Chaos and failure-recovery matrix

"Real multi-node" = ran against physical or ≥3-node deployment. **No row has run on a real multi-node deployment.**

| Scenario | Test exists? | Ran? | Environment | Expected behaviour / typed failure | Gap |
|---|---|---|---|---|---|
| Kill each critical component (http worker, browser worker, relay, resolver, probe, hash, requester, extract, admit, intel, feedback, collector, finalizer, encoder, matcher, Redis, Scylla node, MinIO) | scattered: P3 1M-claim random kills, `crash_recovery.py`; P4 browser-kill; P7 failure injection (`[C]`); P8 resolver/claims; P9 SOAK-1000 failure classes; P10 G-1…G-15, R-1…R-29 | yes `[T]` per phase | single host | claim reclaim after lease; typed `*Unavailable`; exits restarted by supervisor | no single suite, **no supervisor**, no matcher/finalizer/feedback kill in crawler2, no real hosts |
| Worker killed while holding a task | `distributed_1m.py`, `crash_recovery.py` | yes (0 lost/0 dup, reclaim 2.5 s) | single Redis | lease expiry → reclaim | at physical scale unproven |
| Redis failure and failover | none for failover | no | — | typed `FrontierUnavailable`; no lost claims after failover | needs TC-042; Redis-full stall already hit in M1 (X-16) |
| Scylla node loss | none | no | — | LOCAL_QUORUM continues; typed `StorageUnavailableError` if quorum lost | TC-014…016 |
| Coordinator failure / outbox batchlog | none (relay-kill only) | partial | single node | outbox+entity rows both present | TC-016 |
| MinIO outage | unit/integration for typed failures `[C]`; P12 finalize resumes? | `[U]` | single | `ObjectStoreUnavailable`; finalization retried, nothing half-finalized | explicit test needed |
| Network partition / cross-host loss | P9 design says "simulated only" | no | — | typed failures, resume after heal | TC-070 |
| Fingerprinter killed during encode/match | P9 SOAK-1000 + P10 R-/G- matrices | yes `[T]` | single host | request re-claimed; intents reconciled; no orphans | real hosts |
| Consumer restart / event redelivery | P2 crash step (200 obs exactly-once effect), P8/P9/P12 consumers idempotent | yes `[T]` | single host | dedupe by catalog key | multi-host |
| Evidence finalization interrupted mid-write | P12 restart-recovery integration test `[C]` | yes | single | append-only E6 LWT; content-addressed manifest | MinIO-outage and node-loss variants |
| Recovery after dependency heals | included in several tests | partially | single | full resume with no manual repair | systematic proof |
| Detection of lost/duplicate work/permanent stalls | P6 `check.py`, P9 soak harness (orphans/open intents), P8 dedup bench | yes, per phase | single | zero lost, zero duplicates | **no cross-pipeline reconciliation tool** (TC-072) |

### 4.7 Seven-day soak

| Item | Status |
|---|---|
| Test exists | **No.** Closest: P6 M1 24 h (not passed), P9 SOAK-1000 (9,077 s, GPU+CPU, fp only), synthetic harnesses |
| Command/orchestration | **absent** |
| Seed set / target config | **absent** (V1 baseline seed set `benchmarks/v1-baseline`, P6 M1 seeds exist but are not defined as the soak input) |
| Topology | must be the physical one (TC-040); laptop-only is not valid; operator note: "no multi-day runs (laptop)" |
| Metrics for memory/CPU/disk/VRAM/Redis/Scylla/queues/browser leaks/restarts | pieces in `benchmarks/p6-m1/monitor.py`; GPU in fp soak; no unified view |
| Lost/duplicate/stuck/lag detection | pieces per phase; not unified |
| Pass/fail criteria | **undefined** — plan M6: "soak passes with no leaks/lost work" |
| Rerun after fixes | **yes** — any code fix after the run invalidates it for the changed component; plan this explicitly |
| Prior runs count? | **No.** They do not satisfy P14 |
| Known pre-conditions from earlier runs | Chromium-tree RSS drift inconclusive (P6 run 2); 429 hot loop (X-14); bulk downloads (X-15) |

### 4.8 Backup and restore

| Store | Backup procedure | Restore drill | Gap |
|---|---|---|---|
| Scylla `crawler2` keyspace | **absent** | never | snapshots/Medusa-style tooling; schema + migration state |
| Scylla `fingerprinter` keyspace | **absent** | never | owned by fp; authoritative representations |
| MinIO raw + evidence objects | **absent** (content-addressed, no object lock/versioning) | never | replication or versioned bucket; metadata↔object cross-check |
| Redis | AOF on in compose; no recovery policy documented | never | decide what is rebuildable (frontier from Scylla?) |
| Config/schemas/contracts/secrets | git for code; secrets none | never | secret store backup |
| Cross-store integrity after restore | none | never | script: every E-row artefact hash resolves in MinIO |
| RPO/RTO | **undefined** | — | TC-075 |

### 4.9 Runbooks and deployment

Present: `docs/architecture/*.md` operator guides (frontier, fetch-workers, extraction, filter-discovery, media-registry, fingerprinter-encoder, match-feedback, evidence, analytics, intelligence), `docs/development.md`, fp `docs/installation.md`, `usage.md`, `p10-matcher.md`, `v2-encoder.md`.

Missing (all **absent**): initial deployment; secrets/config management; startup/shutdown ordering; health check catalogue; upgrade + migration procedure (`crawler2-storage migrate` exists, procedure not written); Redis/Scylla/MinIO/fp recovery; stuck-task recovery; queue/event-lag diagnosis; capacity and retention; backup/restore; incident response; rollback after failed deploy; evidence-package verification procedure for third parties (`sha256sum -c` is only in P12 exit doc); analytics export validation in production; known-limitation/escalation page.

### 4.10 Final M6 acceptance (measurable form of the plan's gate)

The plan's wording is: *"soak passes with no leaks/lost work; restore drill succeeds"*, plus P14 body requirements. Translated, without narrowing:

1. 7-day (≥168 h) continuous soak on the physical topology passes the TC-071 criteria, with all code frozen or the soak re-run after changes.
2. Reconciliation shows **zero unaccounted-for lost work** (task → page → media → representation → match → evidence) and a bounded, explained duplicate count.
3. No monotonic growth in RSS, FDs, threads, browser memory, Redis memory, queue depth/age, Scylla pending compactions, disk — or each is explained and bounded.
4. Chaos matrix (§4.6) run on the physical deployment; every scenario shows typed failures and no data loss.
5. Backup→restore drill succeeds; evidence hashes verify; measured RTO/RPO recorded.
6. RF=3 storage evidence (§4.1) and physical multi-host evidence (§4.2) exist, including fp 13d re-verification.
7. SSRF/DNS-rebinding, secrets, isolation, auth items either implemented and adversarially tested, or explicitly accepted by the owner as residual risk.
8. Dashboards/SLOs/alerts/tracing exist and were exercised during the chaos runs.
9. All logs, metrics, results and deviations committed under `docs/phases/p14-production-hardening/` (this directory does not exist yet).

---

## 5. Cross-phase blockers and critical path

```
TC-001 hardware inventory ──┬──► TC-010 3-node Scylla ──► RF=3 suites (TC-011…019) ──┐
                            ├──► TC-040 physical topology ───────────────────────────┤
                            └──► fast disk for E-R1 (TC-021 / O-1) ─► P10-T6/T10      │
TC-004 owner decisions O-1…O-6 ──► TC-020 (T4), TC-022 (T8: publication held) ──► P10/M3
P10/M3 ──► TC-026 real chain ──► M4 (TC-027), M5-real (TC-028), P13-campaign (TC-029)
TC-060/061 SSRF + TC-065…067 secrets/isolation, TC-050…055 observability ──► chaos TC-070
TC-071 soak spec + TC-072 harness + (TC-026) ──► TC-073 7-day soak
TC-074/075 backup + TC-076 restore drill ──► M6
```

Validated against code/docs rather than assumed:

1. **Hardware (TC-001) is the root blocker** for RF=3, physical multi-host, E-R1 (needs SSD-class index storage; the NVMe is excluded), the soak and any honest M6 evidence. Nothing in the repo shows any additional machine.
2. **Real matching is a prerequisite for the end-to-end proof.** Confirmed in code and docs: `match.found` publication is *held* until P10-T8 calibration is not UNAVAILABLE (`fp:validation.md` §11 P10-T8, K-8/K-10), so crawler2's P11/P12/P13 consumers have never seen a real event. M4-real, M5-real and campaign reports cannot start before TC-022.
3. **P10 gating needs owner decisions, not only engineering**: T4 and T8 cannot pass by tuning alone (best measured 0.953 vs 1.0; no hard-negative labels).
4. **The soak cannot be specified until the chain exists** (otherwise it soaks a pipeline whose back half is simulated) — but its *platform* items (observability, harness, backup) can be built earlier.
5. **Page-fetch SSRF (TC-060) is independent and can start immediately** and should precede any soak that touches live sites from a non-laptop network.
6. P6 Gate G, P4 gate and P7 deferrals do **not** block M6 mechanically (they are decisions), but leaving them open means V2 cannot be declared "all milestones accepted". M6 as written in the plan does not require M1–M5.
7. RF=3 tests (TC-011…019) are independent of the matcher and can run as soon as the cluster exists.

---

## 6. Documentation corrections (not performed)

| # | File | Correction |
|---|---|---|
| D-1 | `README.md`, `docs/v2-phase-plan.md` (P12 status + decisions-log row), `docs/phases/p12-evidence-collector/exit.md` §1–§3, `decisions.md` OD-P12-7 | Replace "awaiting owner review" with the recorded acceptance of the **fixture package** (date, reviewer, scope); keep "M5 real-data check pending", "ADR-005 open". Needs TC-002 first — the repo cannot be updated from `[O]` alone. |
| D-2 | `docs/v2-phase-plan.md` | Add P10 status (fp, not passed; T4/T6/T8/T10; publication held) and a decisions-log row; link `fp:docs/phases/p10-…`; add ADR-028 to `docs/adr/README.md` once TC-005 decides. |
| D-3 | plan P6 status + decisions log + `p06-…/validation.md` last paragraph | Remove "run 3 follows…" / "P14 hardening items (1),(2)" now superseded by ADR-022; state the Gate G disposition (TC-006); "M1 continues as P7 history" is stale (stopped 2026-10-05). |
| D-4 | plan Part F | #1 no longer "blocks P12 design only"; #2 hardware still open — add owners/dates. |
| D-5 | plan P9 | Reconcile "13D fix applied — target media/artifacts in MinIO" with `p09 audit A-5` and fp README (X-4); the plan block carries four stacked "Status as of" histories — keep one current block. |
| D-6 | `docs/README.md` | Empty — write an index. |
| D-7 | `docs/installation.md` | Create (listed in the audit brief; absent). |
| D-8 | `docs/development.md` / CI | Document which tiers are not in CI (X-8). |
| D-9 | `docs/phases/p00-foundations/validation.md` | "CI partially verified" — record whether CI has ever run on GitHub (40 unpushed commits suggests not recently). |
| D-10 | `docs/benchmarks.md` | Mark the superseded P9 rows clearly; add where P10 benchmarks live (`fp:benchmarks/p10-index/`). |
| D-11 | `docs/phases/p04-fetch-workers/…` §33a and plan decisions row | Record final disposition (TC-007). |
| D-12 | `docs/phases/p07-crawl-intelligence/exit.md`, plan P7 | Keep; add the TC-032 backlog decision. |
| D-13 | Missing documents | `docs/phases/p14-production-hardening/{audit,design,decisions,validation,exit}.md`; SLO doc; security model doc; runbook set (§4.9); backup/restore procedure; deployment/topology ADR (extends ADR-006); ADR for secrets/auth; ADR for tracing. |
| D-14 | Memory/status records | `p12-status` memory and `MEMORY.md` say "awaiting owner review" — update when D-1 is done. |

---

## 7. Owner decisions and external prerequisites

**Decisions required (none invented here):**

1. Which physical machines exist; roles; budget/time for ≥3 Scylla nodes + Redis + MinIO (TC-001, plan Part F #2).
2. P10: O-1 (index storage), O-2 (equivalence recall target or amended criterion), O-3 (calibration/label data and release of held publication), O-4 (N-SOAK baseline run or scope), O-5, O-6.
3. ADR-005 and OD-P12-1…6: jurisdictions, screenshots/clips/WARC and re-visiting sources, retention/WORM, signing/timestamping, partial packages, personal data.
4. P6 Gate G disposition; P4 success-metric methodology; P11 OD-2; P7 resume-or-descope; P13 OD-P13-1…5 (including approval to run the real read-only export, which the session permission policy refused).
5. Soak definition and approval to start it (project rule: no benchmark/M1/soak run without being asked).
6. Dependency/container scanning policy; secret store choice; alert destination (person/channel).
7. RPO/RTO targets and retention/capacity limits.

**External prerequisites (not currently evidenced as available):**

- ≥3 hosts with fast local disks for Scylla (the dev host is 15 GB RAM on a 5,400-rpm USB HDD; Windows NVMe is explicitly off-limits).
- A second application host (crawler pools); network between hosts; Redis host; MinIO with ≥4 drives for distributed mode.
- This RTX 2050 host (4 GB VRAM) for GPU encoding; SSD-class volume for the vector index (O-1).
- Additional labelled data (hard negatives, live-action content) with licence verification (fingerprinter corpus is 7 Blender films).
- Credentials/secret store, TLS material, network egress policy, Tor access policy.
- Elapsed time: 7 days continuous soak plus reruns; laptop cannot host it (recorded operator constraint).

---

## 8. Recommended execution order (proposed; not permission to execute)

**Stage 1 — immediate decisions and environment preparation**
TC-001 inventory → TC-004/TC-003/TC-006/TC-007/TC-008/TC-009 decisions → TC-002 record M5 fixture acceptance → TC-091 push work → TC-092/TC-093 CI/coverage hygiene.

**Stage 2 — blocking correctness and integration work (can run in parallel with Stage 1 once decided)**
TC-060…064 page-fetch SSRF + rebinding pinning (both repos) · TC-020/021/022 P10 gates per decisions · TC-024 merge `p10-impl`, fix P9 O-6 · TC-033 P6 fetch/consumer retry items · TC-035 rebuild drills · TC-050 metrics endpoints.

**Stage 3 — missing validation and benchmark runs on the single host**
TC-031 P8 rerun · TC-029 real-data export (once approved) · TC-026 real end-to-end chain (needs Stage 2 P10) → TC-027/028 · TC-030 migrations/feedback enablement decision.

**Stage 4 — physical multi-host and RF=3 proof**
TC-010 → TC-011…019 · TC-040/041/042/043 · TC-044…047 · TC-025 · TC-046.

**Stage 5 — observability, security, chaos, recovery**
TC-051…056 · TC-065…069 · TC-070 · TC-074/075 (build backup).

**Stage 6 — seven-day soak**
TC-071 spec approved → TC-072 harness dry-run → TC-073 run → fix → rerun affected scope.

**Stage 7 — final restore drill and M6 acceptance**
TC-076 → M6 gate against §4.10.

**Stage 8 — documentation and repository closure**
TC-090, TC-094, TC-095, TC-077, §6 corrections, status docs, memory/MEMORY.md.

---

## 9. Realistic readiness assessment

| Category | Items |
|---|---|
| **Locally completable (this laptop, no new hardware)** | decisions and doc corrections (needs owner input); page-fetch SSRF + rebinding pinning and adversarial tests; metrics endpoints, dashboards-as-code, alert rules (validated against local stack); trace-id design; backup/restore *tooling* and a **single-node** restore rehearsal (not the M6 drill); runbook drafts; CI extension; fp branch merge/push; P10 engineering where not data-bound |
| **Needs other physical hosts / resources** | RF=3 suite and benchmarks (TC-010…019); real multi-host (TC-040…047); E-R1 on fast storage (TC-021); redis failover (TC-042); distributed MinIO; real network latency numbers; fp 13d re-verification; network-partition chaos |
| **Needs prolonged elapsed time** | seven-day soak and any rerun; real-data export duration; N-SOAK V1 baseline (≈ 22 h); a real Gate-G-style 24 h run if the owner wants it |
| **Needs data or owner decisions, not engineering** | calibration labels (O-3), equivalence target (O-2), ADR-005, soak spec, RPO/RTO, hardware list |
| **Cannot honestly be declared complete by tomorrow** | M6, "production-ready", M3, M4-real, M5-real, RF=3 evidence, physical multi-host proof, the soak (168 h cannot be compressed or simulated), restore drill. Realistic same-day output: decisions recorded, TC-002/D-1 documentation, SSRF/observability groundwork — not milestone closure. |

---

## 10. Final acceptance checklist (use later for M6 and V2 completion)

**M6**
- [ ] 3-node Scylla RF=3 NTS, tablets off; P2 suite, `validate-stack.sh` P2 steps, `benchmarks/p2-storage` pass with logs committed
- [ ] LOCAL_QUORUM RAW, LWT under node loss, coordinator-failure batchlog replay tested
- [ ] Physical topology (host A pools, GPU host, Scylla×3, Redis, MinIO, fp) documented and running
- [ ] fp 13d re-verified across machines; contract pin consistent
- [ ] SSRF (page + media + fp), redirects, DNS-rebinding pinned; adversarial suites in CI
- [ ] Secrets least-privilege, rotated once; Tor/browser isolation and egress controls tested
- [ ] Dashboards, SLOs, alerts (tested by firing), trace of one task to `EvidenceRecord`
- [ ] Chaos matrix §4.6 executed on the physical deployment, typed failures asserted, no data loss
- [ ] 7-day soak passed; criteria from TC-071; reconciliation shows zero lost work; no unresolved leaks; rerun done if code changed
- [ ] Backup and restore drill passed; evidence hashes verified; RTO/RPO met
- [ ] Runbooks (§4.9) exist and each was executed once
- [ ] `docs/phases/p14-production-hardening/` complete with deviations listed

**Whole system**
- [ ] M3 passed or owner-amended with record (T4, T6/T10, T8); `match.found` publication released
- [ ] Real end-to-end chain run once (TC-026); M4 and M5 accepted on real output
- [ ] P13 real export and campaign report produced
- [ ] P6 Gate G, P4 gate, P7/M2 each closed or explicitly deferred by recorded decision
- [ ] ADR-005 closed or scoped; evidence gaps (screenshots/WARC/WORM/signing) accepted or built
- [ ] All repos pushed, CI green including later tiers, new-code coverage ≥ 85 % for every phase
- [ ] Documentation consistent (§6 empty)
