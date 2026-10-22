# P12 exit review — evidence collector & finalizer (milestone M5)

Date: 2026-10-22. Evidence: [validation.md](validation.md). Design:
[design.md](design.md). Decisions: [decisions.md](decisions.md). Audit:
[audit.md](audit.md). Operator guide:
[../../architecture/evidence.md](../../architecture/evidence.md).

## 1. Status

**P12 IMPLEMENTED; M5 NOT PASSED — AWAITING OWNER REVIEW.**

| Dimension | Status |
|---|---|
| Implementation | **Complete** for the technical baseline: collector (consumer group, idempotent, restart-safe), provenance resolution through the registry, explicit gaps/ambiguity, artifact SHA-256 verification, validation, append-only finalization with supersession and audit, canonical manifests, campaign statistics, case package (JSON + HTML + artifacts + checksums + deterministic archive), CLI, V007 |
| Automated tests | **Pass**: 52 P12 unit tests; whole default suite 848 passed / 162 skipped; ruff, format, mypy clean |
| Integration tests | **Pass** on the real compose stack (Scylla, MinIO, Redis): 3 P12 tests incl. CLI and restart recovery; 90 regression tests (schema incl. V007, P2 evidence tables, P11 replay) |
| Real-data validation | **Not done**: all inputs are fixtures; the P10 matcher has not passed M3, no real `match.found` exists; no live site was visited |
| Not provided (by decision) | screenshots, media clips, WARC/full headers, object lock/WORM, trusted timestamps, signatures, PDF — all declared in every package |
| **M5** | **NOT PASSED** — requires the owner's review and acceptance of a full case package (plan §P12). A fixture package exists for that review; it is not evidence from a real run |

## 2. M5 gate table

| Criterion (plan §P12) | Status | Evidence |
|---|---|---|
| Collector on `match.found` captures/references raw response, timestamps, URL + redirect chain, crawl method, page/media identity, hashes | **PASS (reference)**; screenshot/clip **not collected** | validation §2 A; gaps declared |
| Finalizer validates completeness and freezes write-once | **PASS (application-level)** | §2 C; E6 LWT + content-addressed manifest; not WORM |
| Object lock / versioning | **NOT PROVIDED** | decisions D-9, OD-P12-3 |
| Optional trusted timestamp / signing | **NOT PROVIDED**, shown as `not_configured` | D-15, OD-P12-4 |
| Immutability: attempted mutation fails and is audited | **PASS** | conflicting finalize → `ConflictError` + `finalize_conflict`; real LWT test |
| Reproducibility: same inputs → same manifest hash | **PASS** | §2 D; two exports byte-identical on the real stack |
| Stats correct vs hand-counted fixture campaign | **PASS** | unit + real stack equal the same table |
| Exporters: JSON manifest + HTML report + raw artifacts | **PASS** (HTML, no PDF) | `var/p12-fixture-case/` |
| Full case package for a test campaign **reviewed and accepted by the owner** | **AWAITING OWNER REVIEW** | review `var/p12-fixture-case/report.html` + `case.json` |

## 3. What the owner needs to review

1. `var/p12-fixture-case/report.html` (open offline) and `case.json`;
   `cd var/p12-fixture-case && sha256sum -c SHA256SUMS`.
2. Whether the package shape (claims vs observed vs derived, gaps,
   partial marking, disclaimer, redaction) is acceptable as the baseline.
3. The open decisions OD-P12-1 … OD-P12-7 (decisions.md §3), in
   particular screenshots/clips (re-visiting sources) and object lock.

M5 can be recorded as passed only on that acceptance; a package from a
real crawl with the real matcher remains a separate, later check
(depends on P10/M3).

## 4. Deployment steps (not performed)

Rebuild the app image (`make up`), `crawler2-storage migrate` (applies
V006 and V007 to the deployment keyspace — additive, no data change),
start `crawler2-evidence collect run` (any number of instances). P11
feedback stays disabled unless separately enabled.

## 5. Next

P13 (analytics) may read E5/E6 and the case statistics; the evidence
backlog: screenshot/clip capture (after OD-P12-2), object lock
(OD-P12-3), RFC 3161/signing over the E6 manifest digest (OD-P12-4),
streaming export for large packages.
