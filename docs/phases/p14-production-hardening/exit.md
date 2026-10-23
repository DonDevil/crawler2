# P14 exit review — production hardening (milestone M6)

Date: 2026-10-22. Evidence: [validation.md](validation.md). Open decisions: [decisions.md](decisions.md).

## 1. Status

**P14 GROUNDWORK IMPLEMENTED AND TESTED LOCALLY. P14 IS NOT COMPLETE. M6 IS NOT PASSED. THE SYSTEM IS NOT
PRODUCTION-READY.** The 7-day soak was **not** run (it cannot be started yet: §4); the harness is ready for operator
use once the prerequisites exist.

Item status words: **PASS-local** (tested on this one host/with synthetic or throwaway data; not the milestone
criterion), **FAIL**, **BLOCKED** (needs hardware/data/decision/time), **DEFERRED** (by recorded decision),
**NOT RUN**.

## 2. M6 acceptance checklist (`to_complete.md` §10)

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | 3-node Scylla RF=3, tablets off; P2 suite, `validate-stack.sh`, `benchmarks/p2-storage` pass | **BLOCKED** | no cluster (TC-010…013) |
| 2 | LOCAL_QUORUM read-after-write, LWT under node loss, coordinator-failure batchlog replay | **BLOCKED** | needs the cluster (TC-014…016) |
| 3 | Physical topology documented and running | **BLOCKED** | hardware inventory undecided (TC-001/040) |
| 4 | fp 13d re-verified across machines; contract pin consistent | **PASS-local** (pin: the fingerprinter env's `antipiracy_contracts` 1.1.0 is byte-identical to this repo's source) / **BLOCKED** (cross-machine) | TC-044/046 |
| 5 | SSRF (page + media + fp), redirects, DNS-rebinding pinned; adversarial suites in CI | **PASS-local** for direct HTTP (pinned), media, fp acquirer; **check-only** for browser (residual R-1) and Tor/proxy (R-3); CI job added, **never run remotely** | security.md §1 |
| 6 | Secrets least-privilege, rotated once; Tor/browser isolation and egress tested | Redis ACL and MinIO service user **PASS-local**; Scylla auth **not tested** (configured only); rotation **NOT RUN**; isolation **NOT RUN** (needs root + real hosts) | security.md §2, §4 |
| 7 | Dashboards, SLOs, alerts (tested by firing), trace of one task to `EvidenceRecord` | all 27 alerts fired in `promtool` unit tests (**PASS-local**); dashboards generated and consistency-tested but **never rendered in Grafana**; SLO targets **not approved**; correlation id rooted but **not propagated** past the media registry, no real-data trace (no real `match.found`) | observability.md |
| 8 | Chaos matrix on the physical deployment, typed failures asserted, no data loss | **NOT RUN / BLOCKED** | TC-070; single-host pieces from P3–P10 stand |
| 9 | 7-day soak passed (zero lost work, no leaks; rerun if code changed) | **NOT RUN / BLOCKED** | harness ready, blockers §4 |
| 10 | Backup and restore drill passed; evidence hashes verified; RTO/RPO met | **NOT RUN** (M6 drill); local single-node **rehearsal PASS-local**; RPO/RTO undecided | recovery.md |
| 11 | Runbooks exist and each was executed once | written; **not executed against injected stalls** | runbook.md |
| 12 | `docs/phases/p14-production-hardening/` complete with deviations listed | this directory; deviations are in each document's "not proven/gaps" | — |

## 3. Whole-system checklist

| Item | Status |
|---|---|
| M3 passed or amended, `match.found` released | **FAIL/BLOCKED** — T4 recall 0.760 (production rule, re-run today) / 0.933 (`equiv/v2`) vs 1.0; T6/T10/T8 blocked; owner decisions O-1…O-6 |
| Real end-to-end chain run once; M4/M5 on real output | **BLOCKED** — no real `match.found` can exist |
| P13 real export and campaign report | **NOT RUN** — export of `crawler2_m1` not approved (OD-P13-1) |
| P6 Gate G, P4 gate, P7/M2 closed or deferred by record | **OPEN** — decisions pending; stale P6 text corrected |
| ADR-005 closed or scoped | **OPEN** |
| All repos pushed, CI green incl. later tiers, coverage ≥ 85 % | **NOT DONE** — nothing pushed; later tiers pass locally; coverage see validation §7 |
| Documentation consistent | **improved** (docs index, installation, plan P10/P6/Part F, P12 acceptance wording, CI tiers, benchmarks labels); remaining D-register items depend on owner decisions |

## 4. Why the soak cannot start, and what is ready

Ready: `crawler2-soak` (prepare/start/dry-run/resume/status/stop/report), 77 unit tests + 2 short end-to-end runs,
`scripts/soak/production.example.toml`, the supervised unit, the operator procedure ([soak.md](soak.md) §6).
Blocked by: (1) no real `match.found` (P10 calibration held) — the run would be INCONCLUSIVE by design;
(2) no physical topology; (3) undecided workload and limits OD-S0…S10 and a missing approval record;
(4) observability gaps (fingerprinter latency series, correlation past the registry, evidence↔match join).

## 5. Milestone status

| | Status |
|---|---|
| M1 | **Not passed** (Gate G open; disposition undecided) |
| M2 | **Deferred, not passed** |
| M3 | **Not passed** |
| M4 | **Not passed** (simulated boundary only; OD-2 open) |
| M5 | **Not passed** (fixture-package review reported accepted by the operator, unrecorded; real-package check pending) |
| M6 | **Not passed** (groundwork only) |

## 6. Remaining work, ranked

| # | Work | Needs |
|---|---|---|
| 1 | Hardware inventory and topology decision (OD-P14-1) | owner |
| 2 | P10 owner decisions O-1…O-6 + labelled hard negatives; release or keep held `match.found` | owner, data, SSD-class storage |
| 3 | Real end-to-end chain (TC-026) → M4/M5 on real output | 2 + engineering |
| 4 | Approve the soak definition and limits; fill `[approval]` (OD-P14-2) | owner |
| 5 | Provision 3-node Scylla; run the RF=3 suites (TC-010…019) | hardware |
| 6 | Multi-host deployment on the units; chaos matrix; browser/Tor isolation applied and tested | hardware, root |
| 7 | Alert destination; approve SLOs; render dashboards against real data (OD-P14-3/4) | owner |
| 8 | Propagate the correlation id in the fingerprinter and finalizer (TR-1/TR-2); fp latency metrics (FP-1) | engineering (both repos) |
| 9 | TLS + inter-service auth design (OD-P14-9); Scylla auth test; secret rotation drill | owner + engineering |
| 10 | Backup schedule/retention/RPO/RTO; production restore drill (OD-P14-5) | owner + hardware |
| 11 | F5/F6 rebuild automation (TC-034); Redis recovery model; `OutboxRelay.replay` CLI | engineering + decision |
| 12 | 7-day soak (+ rerun after any fix) | 1–4, elapsed time |
| 13 | Push the work (crawler2 40+ commits, fp `p10-impl`) so CI runs remotely (TC-091) | owner authorization |
