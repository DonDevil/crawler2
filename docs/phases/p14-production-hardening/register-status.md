# Execution status of the outstanding-work register (`to_complete.md`, audit of 2026-10-22)

`to_complete.md` is preserved exactly as audited. This is the dated progress record against its task ids
(2026-10-22 sprint). Status words: **DONE-local** (implemented and tested on this host; scope in the note),
**PARTIAL**, **BLOCKED** (hardware / data / owner decision / elapsed time), **DECISION** (owner's), **NOT STARTED**.
"Evidence" points to [validation.md](validation.md) sections unless stated.

| Id | Status | Note |
|---|---|---|
| TC-001 hardware inventory | DECISION | OD-P14-1 |
| TC-002 record M5 fixture acceptance | PARTIAL | recorded as **operator-reported, unrecorded** in README, plan, P12 exit/decisions, architecture/evidence; a formal record needs the reviewer, date and scope |
| TC-003 ADR-005 / OD-P12-1…6 | DECISION | untouched |
| TC-004 P10 O-1…O-6 | DECISION | O-6 additionally *fixed in code* (below); the rest untouched |
| TC-005 P10-D16 location | DECISION | the plan now carries a P10 status block and log row pointing at the fp documents; the move is undecided |
| TC-006 Gate G disposition | DECISION | stale statements corrected; OD-P14-12 |
| TC-007 P4 methodology | DECISION | untouched |
| TC-008 P11 OD-2 / TC-009 P13 OD-P13-1…5 | DECISION | untouched; **no real `crawler2_m1` export was run** |
| TC-010…TC-019 RF=3 storage | BLOCKED | no cluster. TC-017: consistency levels are centralised and match ADR-012 (static guard test); behaviour at RF=3 unproven |
| TC-020 P10-T4 recall | BLOCKED/DECISION | failure **reproduced** (0.760 production rule) and its cause confirmed as documented; no defect found; O-2 |
| TC-021 E-R1 capacity, TC-022 calibration, TC-023 N-SOAK | BLOCKED | fast storage, labelled data, O-1/O-3/O-4 |
| TC-024 push/merge `p10-impl`, fix O-6, clean fp tree | PARTIAL | **O-6 fixed + test** (uncommitted); nothing pushed/merged; the fp tree still has the pre-existing `.vscode/settings.json` change and an untracked benchmark JSON (untouched) |
| TC-025 P10 failure matrix on real hosts | BLOCKED | |
| TC-026 real end-to-end chain; TC-027/028; TC-029 | BLOCKED | no real `match.found`; export not approved |
| TC-030 V006/V007 on target keyspace; enable feedback | NOT STARTED | decision-dependent |
| TC-031 P8 real-stack tiers after P9 | DONE-local | `scripts/test-media.sh` 125 passed (§2); the cross-service fp test not re-run |
| TC-032 P7 resume or descope | DECISION | |
| TC-033 X-14, X-15, in-process retry | DONE-local | back-off (`crawlers/backoff.py`), declared-size refusal, typed stream-reader errors; unit-tested, not measured over a 24 h window; per-domain *share* cap (X-15 second half) remains P7 |
| TC-034 F5/F6 rebuild by replay | NOT STARTED | manual procedure documented (recovery.md §3); automation needs a design |
| TC-035 rebuild drills | PARTIAL | table of rebuild commands and what has been drilled (recovery.md §3); no new timed drills |
| TC-040…TC-047 physical multi-host | BLOCKED | TC-044: contract package copy in the fp env is byte-identical (1.1.0) |
| TC-041 supervision | PARTIAL | systemd units + launcher, syntax-verified, never started on a host |
| TC-042 Redis HA / TC-043 MinIO distributed | DECISION/BLOCKED | |
| TC-050 metrics endpoints | DONE-local | every crawler2 long-running role; fp encoder/matcher expose `/metrics` when `metrics_port` is set (fp latency series not verified: FP-1) |
| TC-051 stack + dashboards | DONE-local | config + 3 dashboards as code; Grafana provisioning loads them; promtool-validated config; not rendered with live data |
| TC-052 exporters | PARTIAL | `crawler2-exporter` (queues, lag, outbox, dead letters, Redis memory, GPU); Redis/Scylla/MinIO/node standard exporters are *named* in the Prometheus config, not deployed |
| TC-053 SLOs/SLIs | PARTIAL | table with units/windows/queries; targets are proposals (OD-P14-3); three SLIs are gaps |
| TC-054 alerts + destination | PARTIAL | 27 rules, every one fired in a promtool test; **destination undecided** (OD-P14-4); `ScyllaPendingCompactions` metric name unverified |
| TC-055 end-to-end trace | PARTIAL | correlation id rooted at the recorder; `crawler2-trace`; not propagated past the media registry (TR-1/TR-2); no real-data trace |
| TC-056 stuck-pipeline runbook | PARTIAL | written (runbook.md); **not exercised against an injected stall** |
| TC-060 page-fetch SSRF | DONE-local | HTTP pinned; Tor/proxy literal-only; browser check-per-request incl. WebSockets (real Chromium test) |
| TC-061 DNS-rebinding closure | DONE-local | crawler2 direct paths and the fp acquirer's own session pinned; browser TOCTOU residual |
| TC-062 redirect re-validation | DONE-local | page fetch, media, fp acquirer, browser (route per hop); manifest sub-fetches go through the same transport |
| TC-063 adversarial suites | DONE-local | `test_ssrf_pagefetch.py` (77), `test_browser_ssrf.py`, `fp:test_acquisition_rebinding.py`; CI job added, not run remotely |
| TC-064 prod refuses SSRF exemptions | DONE-local | plus credential validation |
| TC-065 secrets | PARTIAL | prod validation, Redis ACL (tested), MinIO least privilege (tested), generated secrets, rotation procedure (not drilled); Scylla auth configured, **not tested** |
| TC-066 TLS / inter-service auth | NOT STARTED | OD-P14-9 |
| TC-067 network isolation | PARTIAL | systemd `IPAddressDeny` example; **not applied or tested**; no Tor service exists |
| TC-068 evidence safety | PARTIAL | report escaping tests extended; WORM/retention/redaction wait for ADR-005 |
| TC-069 dependency/container scanning | PARTIAL | `uv audit` run, certifi upgraded; image scanning and the policy undecided (OD-P14-7) |
| TC-070 chaos suite | BLOCKED | needs the physical deployment |
| TC-071 soak definition | PARTIAL | [soak.md](soak.md) specifies everything the harness can; OD-S0…S10 are the owner's |
| TC-072 soak harness | DONE-local | `crawler2.soak`: 77 tests + two short end-to-end runs |
| TC-073 7-day soak | BLOCKED | [soak.md](soak.md) §9 |
| TC-074 backup tooling | DONE-local | `crawler2-backup`; local rehearsal passed (§5); Redis only dumped |
| TC-075 RPO/RTO, TC-078 capacity | DECISION | OD-P14-5 |
| TC-076 restore drill | BLOCKED | the M6 drill; only the local rehearsal exists |
| TC-077 runbooks | PARTIAL | runbook, recovery, deployment, security, soak written; none executed once on a deployment |
| TC-090 documentation corrections | PARTIAL | done where evidence allowed (D-1…D-8, D-10, D-13); D-5 only annotated; D-9 updated; D-11/D-12/D-14 depend on decisions (D-14: memory updated) |
| TC-091 push work | NOT STARTED | **nothing staged, committed or pushed** (not authorised) |
| TC-092 CI tiers + contract compat | PARTIAL | jobs added and commands run locally; contract-compat vs the fp pin verified by byte comparison only |
| TC-093 coverage ≥ 85 % | PARTIAL | numbers for the new code in validation §7.1; P10–P13 packages measured by unit tests only (see table); no per-phase target decision |
| TC-094 installation / docs README / deploy-rollback | DONE-local | written; **not followed once from a clean host** |
| TC-095 generated artefacts | PARTIAL | `var/` stays ignored (contains M1, P12 fixture package and this sprint's soak dry-run directories); the fp untracked benchmark JSON left as found |
