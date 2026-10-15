# P9 Requirement and exit-gate traceability

Status (2026-10-15): **design APPROVED (operator, 2026-10-10); implementation
complete for the approved scope; every mandatory exit gate satisfied;
accepted by the operator 2026-10-15 with the documented limitations
(decisions §5c); not production-deployed** —
G-1, G-5 PASS, **G-8 PASS** (SOAK-1000 run 5 on the corrected corpus; criteria
3/4 only vacuously; validation §8.4); plan gates: **G-3 PASS** under OF-4,
**G-2 FAIL** (C-PREC) → FP16 stays `gated`, G-6 measured. Operator decisions
OF-4, OF-5 (decisions §5b, 2026-10-12) implemented. Defects X-8/X-9 (validation
§7.7) and X-10 (§8.3) fixed. Earlier status (2026-10-12): G-3 FAIL as
implemented, G-8 FAIL (criterion 8) — superseded. Operator decisions OF-1…OF-3 (decisions §0a, 2026-10-11)
are implemented: rows C-9, C-18, C-24, C-25, I, G-5, G-8. Design sections refer to [design.md](design.md), decisions to
[decisions.md](decisions.md), tests to design §15 and to files in the
fingerprinter repository's `tests/v2/`, benchmarks to
[data-preparation.md](data-preparation.md) §5. Evidence: [validation.md](validation.md);
gates: [exit.md](exit.md). Code paths are in the fingerprinter repository,
package `fingerprinter/`.

Status terms (unchanged meanings): **AUDIT_COMPLETE**; **DESIGN_APPROVED** —
governance only; **IMPLEMENTED** — code exists, not yet verified by its
tests; **VERIFIED** — its tests ran and passed on the stated environment
(synthetic media unless noted); **TEST_BLOCKED** / **NOT MEASURED** — the
prerequisite (data, hardware) is missing; **FAIL** — ran and did not pass;
**DEFERRED**. Code existing is never enough for VERIFIED.

## 1. Audit requirements

| # | Requirement | Evidence | Status |
|---|---|---|---|
| A-1 | Audit `target/` incl. the 13D multi-host finding | audit §4.2, §5 | AUDIT_COMPLETE |
| A-2 | Audit `embedding/dinov2_engine.py` | audit §4.3 | AUDIT_COMPLETE |
| A-3 | Audit sampling and hidden identity inputs | audit §4.4 (processor backend checked) | AUDIT_COMPLETE |
| A-4 | Audit `worker/matching_handler.py` | audit A-1…A-3 | AUDIT_COMPLETE |
| A-5 | Audit GPU path | audit §8 (NVML mismatch; no GPU numbers) | AUDIT_COMPLETE |
| A-6 | CPU baseline, s per minute of video | 1.80 s/min **synthetic** 320×240 only (audit §7, result JSON) | AUDIT_COMPLETE (synthetic VERIFIED as a measurement; real-media baseline TEST_BLOCKED, row G-6) |
| A-7 | crawler2 boundary audit | audit §3 | AUDIT_COMPLETE |
| A-8 | Data-sufficiency gate | data-preparation §1: not passed | AUDIT_COMPLETE (gate result: not passed) |

## 2. Review blockers A–I (revision 2)

| Issue | Requirement | Design / decision | Implementation | Tests | Design | Impl/Test | Outstanding |
|---|---|---|---|---|---|---|---|
| A | Intake gap recovery without ID arithmetic; replay; explicit accept-gap | §6, D6, D27, ADR-025 | `intake.py`, CLI `intake …` | TG-1…TG-14 (`test_intake.py`, 17 tests), `test_cli.py` | DESIGN_APPROVED | VERIFIED | accepted gaps can lose requests permanently (limitation 1) |
| B | Partial commit recovery; one commit point; reconciler | §8, D13, D20, ADR-026 | `commit.py`, `reconcile.py` | TC-1…TC-14 (`test_commit.py`, 15 tests) | DESIGN_APPROVED | VERIFIED | N-1 narrowing (design §22) |
| C | No TTL loss of unpublished events | §8.1, §9.2, D21 | `store.py` intents (no TTL), `publish.py` | TE-1, TE-2/TC-7 (`test_events.py`, `test_commit.py`) | DESIGN_APPROVED | VERIFIED | — |
| D | Target events before consumers exist | §9.1, §10, D22, D14 | `publish.py` stream registry, `targets.py` | TE-3, TE-4 | DESIGN_APPROVED | VERIFIED | held events accumulate until P10 (K-10) |
| E | Active work discoverable beyond 30 days | §7.2–§7.3, D23, D13 | `store.py` due index, `requests.py` | TD-1…TD-8 (`test_requests.py`) | DESIGN_APPROVED | VERIFIED | — |
| F | Typed external vs internal sources | §5, D24, D10 | `sources.py`, `requests.py` | TS-1…TS-9 (`test_sources.py`) | DESIGN_APPROVED | VERIFIED; real 8 GiB run **PASS** 2026-10-11 (validation §7.5, after a test-fixture fix) | DNS rebinding P14 |
| G | OOM behaviour consistent with the soak gate | §12.3–§12.4, §13.3, D25, D16 | `encoder.py`, `model.py` | TX-6, TX-7, TX-8, TX-10, TX-11 (`test_encoder_failures.py`, incl. a real CUDA OOM) | DESIGN_APPROVED | VERIFIED (tests); SOAK-1000 ran: no OOM occurred (criteria 3/4 vacuous), gate FAIL on criterion 8 (validation §7.9) | OOM recovery exercised by tests only, not by the soak |
| H | Runtime/preprocessing determinism; colour policy | §3.2–§3.6, D4, D26, ADR-027 | `spec.py`, `runtime.py`, `media.py`, `store.runtime_check` | TI-3, TI-5, TI-6 (12 golden fixtures), TI-8 | DESIGN_APPROVED | VERIFIED | runtime-compat gate for a future `rt-2` not exercised |
| I | Accurate performance/precision gates; precision identity | §3.7, §13, D3, D17 | `benchmarks/p9-encoder/*.py`, `specs.py` gating | TP-4, TP-5, TP-6; B-GPU-a, B-PREC on C-PREC/L-PAIRS | DESIGN_APPROVED | identity/gating VERIFIED; G-5 **PASS** (6.59×); G-2 **FAIL** on C-PREC; G-3 **PASS** under OF-4 (validation §7, §8.1) | FP16 stays `gated` (G-2) |

## 3. Capabilities

| # | Requirement | Design / decision | Implementation | Tests | Design | Impl/Test | Outstanding |
|---|---|---|---|---|---|---|---|
| C-1 | Representation registry, authoritative, identity per PC-3 | §3.1, §4.1, §8; D2, D13, PC-3 | `db.py` schema, `commit.py`, `spec.py` | TI-1…TI-4, TI-7, TC-10 | DESIGN_APPROVED | VERIFIED | — |
| C-2 | Vectors in MinIO, never host-local | §3.8, §4.2, §4.4; D12 | `artifact.py`, `objects.py`, scratch per attempt | TC-*, TH-7 | DESIGN_APPROVED | VERIFIED | — |
| C-3 | Media-centric encoding `encode(media, spec)` | §2, §7, §12; D1 | `requests.py`, `encoder.py` | TR-1…TR-3 | DESIGN_APPROVED | VERIFIED | — |
| C-4 | Target manager on the same infrastructure | §10; D14 | `targets.py` | TR-4, TR-7, TE-3/TE-4, `test_targets.py` (13) | DESIGN_APPROVED | VERIFIED; real > 5 GiB multipart copy **PASS** on an idle disk (validation §7.5; failed once under disk contention, 120 s part-copy timeout) | N-7, N-9 |
| C-5 | Versioning, backfill, retirement | §11; D15 | `specs.py`, `backfill.py` | TR-5, TR-6, `test_backfill.py` (7) | DESIGN_APPROVED | VERIFIED | — |
| C-6 | Multi-host safety | §4, §7, §8; D12, D13, D20 | leases, fencing, intents | TH-1…TH-8 (simulated, `test_twohost.py`) | DESIGN_APPROVED | VERIFIED (simulated) | real machines P14 |
| C-7 | One GPU owner per host | §12.1; D16 | `encoder.py` GPU lease | TX-3, TX-4, TH-4 (real GPU) | DESIGN_APPROVED | VERIFIED | — |
| C-8 | CPU decode → bounded queue → device | §12.2; D16 | `encoder.py` | TX-9 | DESIGN_APPROVED | VERIFIED | thread counts untuned |
| C-9 | Adaptive batching, OOM recovery without loss/duplication | §12.3; D16, D25 | `encoder.py` + `calibration.py` (`throughput/v1`) + halving | TX-6 (injected + real CUDA OOM), `test_calibration.py` (11), real-GPU calibration test, calibration-failure exits | DESIGN_APPROVED (§12.3 per OF-2) | VERIFIED; B-CAL measured (B = 2 selected, repeatable) | — |
| C-10 | CPU fallback | §12.5; D16 | `--device cpu` | TX-2, all CPU tests | DESIGN_APPROVED | VERIFIED | — |
| C-11 | Startup when CUDA/model unavailable | §12.5; D16 | typed exits 69/78 | TX-1 (3 cases), golden check tests | DESIGN_APPROVED | VERIFIED | — |
| C-12 | Observability | §12.7 | `metrics.py`, `/metrics` via `metrics_port`, `encode_attempt_log`, exit reports | asserted indirectly (exit reports, attempt log test) | DESIGN_APPROVED | IMPLEMENTED (partly verified) | alert thresholds not validated |
| C-13 | crawler2 adapter | §16; D19 | P8 (FROZEN, unchanged) + crawler2 `tests/integration/fingerprinter/` | cross-service test | DESIGN_APPROVED | VERIFIED | — |
| C-14 | `match.found` consumption | §16; D19 | none (not in P9) | — | DESIGN_APPROVED (not in P9) | n/a (P11/P12) | — |
| C-15 | P8 handoffs: default spec, re-request, publisher vs `media-registry` | §6.10–§6.11, §9; D5, D7, D8 | `requests.py`, `publish.py` | TE-5, TE-8, cross-service (FAILED lands on `default`) | DESIGN_APPROVED | VERIFIED | — |
| C-16 | Duplicate/delayed/redelivered requests | §6.10; D8 | `requests.upsert_event` | TG-2, TR-2, concurrent upsert test | DESIGN_APPROVED | VERIFIED | — |
| C-17 | Raw identity-byte acquisition up to 8 GiB | §5; D10, D24 | `sources.py` | TS-1, TS-4, TS-6 | DESIGN_APPROVED | VERIFIED, incl. the real 8 GiB run (validation §7.5) | — |
| C-18 | Real PTS, explicit sampling and colour | §3.3; D4 | `media.py` | TI-6 golden fixtures | DESIGN_APPROVED | VERIFIED | truncation: C-24 |
| C-19 | Precision in identity; FP32 default; FP16 gated; no merge | §3.1, §3.7, §11.1; D2, D3 | `spec.py`, `specs.py`, encoder refusal | TP-4…TP-6 | DESIGN_APPROVED | VERIFIED | FP16 precision gate G-2 failed on C-PREC (G-3 passes under OF-4): FP16 stays `gated` |
| C-20 | Attempt lease renewal, accounting, poison bound, reopen on new `event_id`, target binding on reuse | §6.10, §7.2, §10; D8, D9, D13, D14 | `requests.py`, `encoder.py`, `targets.py` | TD-7, TD-8, TX-10, TE-8, TR-4 | DESIGN_APPROVED | VERIFIED | — |
| C-21 | Content-addressed target upload; `encode.failed` only for event-originated requests with monotonic `attempt`; attempt wall-clock limit; observable soak orphan check | §7.2, §7.4, §8.5, §10, §13.3; D8, D9, D14, D25 | `targets.py`, `requests.py`, `encoder.py`, `soak.py` | TR-7, TE-8, TX-11, soak criterion 7 | DESIGN_APPROVED | VERIFIED; soak criterion 7 met on SOAK-1000 (1,001 objects = 1,001 rows, 0 open intents; validation §7.9) | — |
| C-22 | Orphan grace > attempt timeout | §7.2, §8.5 | `config.py` validator | `test_orphan_grace_must_exceed_attempt_timeout` | DESIGN_APPROVED | VERIFIED | — |
| C-24 | Explicitly detected truncated input → `decode_error` under a new spec version; v1 unchanged (OF-1) | §3.2a, §7.4; decisions §0a | `spec.py` (`dinov2-seg/2`, `decode.truncation`), `media.py` (`TRUNCATION_INDICATORS`) | `test_truncation.py` (truncated MP4/MKV/WebM at ⅓ and 90 % with ffmpeg exit 0; v1 prefix behaviour; v1 = v2 on complete media; benign warnings; metadata text; corruption warnings; anchoring), identity tests (v1 digests pinned, v2 identity), soak criterion 8 | DESIGN_APPROVED (operator decision) | VERIFIED | undetectable forms listed in design §3.2a; SOAK-1000 found a ⅓-truncated MP4 reported only as `Packet corrupt` (accepted per §3.2a) — wider than §3.2a states; OF-5 (a): policy kept, documented limit; soak corpus expected failures now verified against the decoder (`test_corpus_verify.py`) |
| C-25 | Batch calibration by best repeatable measured throughput among memory-eligible B (OF-2) | §12.3; decisions §0a | `calibration.py`, `encoder._calibrated_batch`, `encoder_hosts.calibration` | `test_calibration.py`: highest fitting not auto-selected, smaller faster B selected, measurement-driven, single anomaly and lucky first round, memory-ineligible (warm-up and timed OOM), B = 1 failure, bounded cost, tie rule; encoder: stored report, reuse, superseded-row recalibration, exits 75/70 with GPU lease released; B-CAL | DESIGN_APPROVED (operator decision) | VERIFIED; measured | — |
| C-23 | Dependency set pinned, reproducible (D12) | §19; D12 | fingerprinter `requirements-p9.txt`, `requirements-p9.lock`; installed with constraints (no upgrades) | V1 suite 445 passed after install | DESIGN_APPROVED | VERIFIED | — |

## 4. Plan tests and exit gates

| # | Requirement | Tests / benchmark | Design | Impl/Test | Blocked by / note |
|---|---|---|---|---|---|
| G-1 | **Exit gate:** S1/S2 reuse (7 encodes, not 10) | TR-1…TR-3 | DESIGN_APPROVED | **PASS** | — |
| G-2 | FP16 precision gate (cosine ≥ 0.999 vs CPU FP32) | TP-1, TP-2, B-PREC (`precision.py`) | DESIGN_APPROVED | **FAIL** — C-PREC: 7 of 10,267 segments < 0.999 (min 0.9906); L-PAIRS: PASS (0 of 6,220; min 0.99928) (validation §7.3–§7.4) | FP16 stays `gated` (TP-4) |
| G-3 | FP16 labelled-decision gate | TP-3 (`precision.py` `tp3_compare`, `test_precision_tp3.py`) | DESIGN_APPROVED; OF-4 interpretation (decisions §5b) | **PASS** — decisions identical 480/480, alignment identical on all 37 matched pairs; 1 non-match partial-alignment difference reported as diagnostic (validation §8.1). Strict reading 2026-10-12: FAIL (§7.4) | FP16 stays gated (G-2 FAIL) |
| G-4 | OOM recovery | TX-6, TX-7, B-OOM | DESIGN_APPROVED | **PASS** (tests incl. real CUDA OOM) | — |
| G-5 | **Exit gate:** GPU inference ≥ 5× CPU (FP32) | B-GPU-a | DESIGN_APPROVED | **PASS** — C-PREC (504 frames from all 63 items): CUDA FP32 43.40 f/s (B = 2, calibrated) vs CPU FP32 6.59 f/s (12 threads) = **6.59×** (validation §7.2); earlier synthetic figures superseded | 5× threshold unchanged (OF-3) |
| G-6 | Real-media CPU baseline | B-CPU | DESIGN_APPROVED | **MEASURED** (no threshold): 2.37 s per minute of video on all of C-PREC; B-GPU-b 1.117 s/min (2.12× end-to-end, reported) (validation §7.6) | — |
| G-7 | Two-host GPU+CPU identical representations (simulated) | TH-8, TX-2 | DESIGN_APPROVED | **PASS** (simulated) | — |
| G-8 | **Exit gate:** soak, 1,000 videos | B-SOAK (`soak.py`) | DESIGN_APPROVED | **PASS** — SOAK-1000 run 5 (corrected corpus `ed3bfd8d…`, 1,030 items, 2 h 31 min to drain): 8/8 criteria; 3 and 4 satisfied **only vacuously** (no controlled exit, no single-item OOM); 1,000 READY + 30 FAILED as labelled (validation §8.4). History: run 1 aborted (X-8/X-9, §7.7); run 2 FAIL, criterion 8 (`c-04-h264.mp4` READY per design §3.2a, §7.9); run 3 aborted (host shutdown), run 4 aborted (X-10) (§8.3) | criteria 3/4 evidenced by tests, not the soak |
| G-9 | Real multi-machine | — | DEFERRED to P14 | — | plan Part F #2 |

## 5. Definition of done (B.3)

See [exit.md](exit.md) §4: DoD-1, 2, 3, 4, 6, 7, 8, 9 met (DoD-7 since
2026-10-15: G-5 and G-8 PASS); DoD-5 partial; DoD-10 cross-service only.

## 6. Summary

Design: all decisions APPROVED (2026-10-10). Implementation: every capability
implemented; all review blockers A–I and capabilities C-1…C-23 VERIFIED by
tests on the real stack, except C-12 (partly). OF-1…OF-3 decided (2026-10-11)
and implemented: C-24, C-25 VERIFIED. Real-media validation (2026-10-11/12)
and the OF-4/OF-5 follow-up (2026-10-12/15): exit gates G-1, G-5, **G-8
PASS** (run 5; criteria 3/4 vacuous); plan gates G-4, G-7 (simulated), **G-3
PASS** (OF-4), G-6 measured, **G-2 FAIL** (FP16 gated); G-9 DEFERRED. The soak
found and the fixes closed X-8/X-9/X-10. P9: **every mandatory exit gate
satisfied; accepted by the operator 2026-10-15 for the approved scope with
the documented limitations** (decisions §5c, exit.md §1). Not
production-deployed; FP16 gated.
