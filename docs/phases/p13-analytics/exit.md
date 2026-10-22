# P13 exit review — analytics layer

Date: 2026-10-22. Evidence: [validation.md](validation.md). Design:
[design.md](design.md). Decisions: [decisions.md](decisions.md). Audit:
[audit.md](audit.md). Operator guide:
[../../architecture/analytics.md](../../architecture/analytics.md).

## 1. Status

**P13 IMPLEMENTED; exit gate PASSED on synthetic fixtures and a throwaway
real-Scylla keyspace; REAL-DATA DEMONSTRATION PENDING (not run).**

| Dimension | Status |
|---|---|
| Implementation | **Complete** for the planned scope: export (17 datasets, explicit exclusions, token-range scans, reconciliation, revisioned publication, resume, gc), DuckDB views + SQL, campaign/source reports, graph, clustering prototype, CLI |
| Automated tests | **Pass**: 75 P13 unit tests (synthetic world); whole default suite see validation §1; ruff, format, mypy clean |
| Integration test | **Pass** on the real compose Scylla/MinIO/Redis (throwaway keyspace; rows written by the real repositories and the real P12 collector/finalizer): source counts = independent `count(*)`, verify-counts drift 0, unchanged re-export, changed source → revision 2 |
| Real-data validation | **Not run**: the read-only export of the real M1 history (`crawler2_m1`) was refused by this session's permission policy (OD-P13-1). No real match/evidence exists anywhere (P10 M3 not passed), so a campaign report on real matcher output is impossible regardless |
| Earlier gates | unchanged: P10/M3 NOT PASSED, P11/M4 NOT PASSED, P12/M5 NOT PASSED (awaiting owner review), P7 WHEN/M2 deferred, P6 Gate G open |

## 2. Acceptance criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| A | Export completeness: explicit schemas and sources; counts reconcile with the selected scope; duplicates/missing/rejected/excluded accounted; manifests + validation | **PASS (fixtures + real Scylla); real-data PENDING** | `test_export.py` hand-counted table for all 17 datasets; rejected/out-of-window/unknown-time/duplicate/conflict tests; integration test: `source_rows` = independent `count(*)` for every table, rejected 0, drift 0; `validate` detects tampering/missing files |
| B | Idempotency: unchanged scope → repeatable outcome; changed source → explicit policy; failed runs never mistaken for published | **PASS** | unchanged re-export (unit + real stack); changed source → rev 2, rev 1 intact, no merge; conflicting duplicates/injected outage → nothing published, `failed` recorded, resume publishes the same content id as a fresh run; crash between rename and pointer repaired; manifest-less directory never read; publish lock |
| C | Parquet + DuckDB: partitioned, readable, queryable without operational stores; schemas and partitions documented | **PASS** | schema/type test against the catalog; date partitions incl. `unknown`; reports in an interpreter where `cassandra`/`redis` cannot be imported; design §3–§4 |
| D | Reproducible reporting from Parquet alone; metrics/scope defined; fixed inputs reproducible; incomplete input reported | **PASS (synthetic inputs)** | equal `report_digest` across two independent exports; hand-counted campaign and source values; missing datasets → `complete: false` / exit 3; empty export → null rates. Real campaign data does not exist yet (P10) |
| E | Offline graph from durable data; ids and provenance preserved; repeatable; off the hot path | **PASS (synthetic + real-stack smoke)** | `test_graph.py`: edge types/direction, verdicts unchanged, no fabricated page edge for an unobserved match, undirected shared-content edge, stable ids across roots, aggregation of repeats, skipped edge types listed; built on the real-stack export without check failures; boundary test: nothing imports `crawler2.analytics` |
| F | Clustering prototype implemented and documented; outputs versioned and reviewable; no P7 runtime change | **PASS as a prototype (synthetic only)** | `test_cluster.py`: features, missing features, insufficient data, noise, determinism, content-derived ids, bound refusal, versioned manifest, `unreviewed` bootstrap file, AST check that no P7 store/advice/feedback/storage module is imported. **No claim** that meaningful clusters exist for real sites |
| G | Documentation and repository | **PASS** | audit/design/decisions/validation/exit + operator guide; canonical docs updated (architecture index, development, README, phase plan, P7 intelligence boundary); requirements re-exported; git scope in validation §6 |

The plan's exit gate ("standard campaign/source reports reproducible from
Parquet alone") is met for the implemented code on synthetic and throwaway
real-stack data. It has **not** been demonstrated on real crawl data or real
matcher output.

## 3. Outstanding work

1. **OD-P13-1:** run the read-only `crawler2_m1` export (command in
   validation §5) and record its reconciliation, run time and peak memory;
   then a source report on it. Campaign reports on real data wait for P10/M3.
2. OD-P13-2…5 (retention/access, shared location, cadence, cluster review).
3. Clustering quality is unevaluated: no labelled site-similarity set exists.
4. Performance at real volume is unmeasured (the M1 export is the first
   measurement); the clustering is O(n²) and capped at 2,000 sites.

## 4. Can P13 be considered complete?

As an implementation: yes, within the planned scope, with tests and
documentation. As a validated capability on real data: **not yet** — the
real-data export is pending an owner decision and real campaign data cannot
exist before P10/M3. P13 does not change the status of any earlier phase and
does not make the system production-ready.
