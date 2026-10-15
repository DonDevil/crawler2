# P9 Exit review — fingerprinter encoder

Date: 2026-10-15 (operator decisions OF-4/OF-5 and the SOAK-1000 rerun,
[validation.md](validation.md) §8; real-media validation 2026-10-12, §7;
earlier versions 2026-10-11, after the operator decisions on OF-1…OF-3,
[decisions.md](decisions.md) §0a). Design approval: 2026-10-10 ([decisions.md](decisions.md) §0).
Evidence: [validation.md](validation.md). Requirement status:
[traceability.md](traceability.md). Runbook:
[fingerprinter-encoder.md](../../architecture/fingerprinter-encoder.md).

## 1. Final status

**P9 COMPLETE — accepted by the operator on 2026-10-15 for the approved
implementation and validation scope, with the documented limitations (§5, §6;
decisions §5c).** Every mandatory exit gate is satisfied (G-1, G-5, G-8;
DoD-7). P9 is **not production-deployed**: it is verified against crawler2's
development stack and not wired into a deployed stack (DoD-10). FP32 is the
default and FP16 stays `gated`. Since 2026-10-15: the
operator decided OF-4 and OF-5 (decisions §5b); the SOAK-1000 corrupt set was
corrected and verified against the decoder; **G-8 PASS** on SOAK-1000 run 5
(8/8 criteria; 3 and 4 only vacuously); **G-3 PASS** under OF-4; **G-2 still
FAIL**, so FP16 stays `gated` (validation §8). The text below up to §2 is the
2026-10-12 status, kept as history.

*Status 2026-10-12 (superseded):* **P9 IMPLEMENTED BUT VALIDATION INCOMPLETE.** The approved design is
implemented in the fingerprinter repository (package `fingerprinter/`) and
verified on the real Scylla/Redis/MinIO stack, on the RTX 2050 GPU and against
crawler2's own P8 requester and projection. Since 2026-10-12 every data gate
has been **measured on licensed real media** (S-SRC 7 films; C-PREC,
L-PAIRS, SOAK-1000 built; validation §7):

- exit gates: **G-1 PASS**, **G-5 PASS** (GPU FP32 6.59× CPU FP32 on
  C-PREC), **G-8 FAIL** — SOAK-1000 met criteria 1–7 (3 and 4 only
  vacuously) but not criterion 8: one of the 30 expected-failure files, a
  ⅓-truncated MP4 that ffmpeg reports only as `Packet corrupt`, became READY,
  exactly as design §3.2a prescribes. That conflict between corpus and
  approved policy needs an operator decision (**OF-5**);
- plan gates: **G-2 FAIL** (FP16: 7 of 10,267 C-PREC segments below 0.999;
  L-PAIRS passes), **G-3 FAIL as implemented** (decisions identical 480/480;
  one non-match alignment differs — scope question **OF-4**), G-6 measured
  (2.37 s/min), G-4 and G-7 (simulated) PASS. FP16 stays `gated`; FP32
  remains the default.

The soak found two defects (X-8 Scylla `WriteFailure` killed a worker
thread; X-9 a service stop turned in-flight attempts into permanent
`decode_error`s); both are fixed and regression-tested. P9 is **not
complete** because the exit gate **G-8 failed** (DoD-7 not met). G-2/G-3
failing is an outcome the design allows (the FP16 spec stays `gated`, design
§3.7) — it does not by itself block the exit, but it is a measured FAIL, not
a pass.

## 2. Exit-gate table (gate groups of the implementation brief)

| Gate | Acceptance criteria (approved design) | Result | Status | Evidence |
|---|---|---|---|---|
| 1 Contract & design integrity | decisions recorded; P8/P1 frozen contracts unchanged; identity deterministic & complete; event semantics; no `match.found` | approval in decisions §0 + ADR-025…027; no file under crawler2 `contracts/` or `crawler2/` changed; cross-service test passes with crawler2's unchanged requester/projection; identity, spec and decode tests (31 unit + 8 golden-fixture); OF-1 adds spec `dinov2-seg/2` without changing `dinov2-seg/1`'s identity (digests pinned); no `match.found` producer/consumer added | **PASS** | validation §2.1, §2.3; `git status` §8 |
| 2 Persistence & concurrency | conditional (LWT) transitions, duplicate/concurrent safety, fencing, idempotent commit, durable intents | every request/representation/target transition is an LWT; TC-1…TC-14, TD-1…TD-8, TC-10 concurrent commits, TG-4 stale CAS, TH-1…TH-3 two processes | **PASS** | test_commit, test_requests, test_twohost |
| 3 Media & target integrity | raw-byte ContentId, 8 GiB bound, staging/promotion/reuse/cleanup, crash recovery, reference safety, TR-7 executable | TS-1…TS-9; **real 8 GiB acquisition PASS** (after a test-fixture fix: the fixture server had loaded the file into RAM); TR-7 all cases incl. **real > 5 GiB multipart copy PASS** on an idle disk (one earlier run failed under disk contention: 120 s part-copy timeout) | **PASS** | validation §7.5; test_sources, test_targets |
| 4 Functional encoding | dinov2-seg/1 path; real PTS; preprocessing/config identity; CPU; reuse, backfill, targets; failures classified | end-to-end encodes with exact PTS (`[0, 61440, 122880]` @ 1/12288), golden decode/colour fixtures bitwise, CPU golden ≥ 0.9999; S1/S2; backfill/targets; §7.4 failure mapping; OF-1: truncated MP4/MKV/WebM → `decode_error` under `dinov2-seg/2` with ffmpeg exit 0, benign warnings accepted (17 tests). Latest full run encodes with the default `dinov2-seg/2`; the `dinov2-seg/1` path stays covered by the golden decode fixtures, the v1 identity tests and the v1 truncation-behaviour tests | **PASS** — synthetic media in the tests; real media since 2026-10-12: all 63 C-PREC clips READY on CPU and on CUDA (B-CPU/B-GPU-b) and all 1,000 SOAK-1000 videos READY | test_encode, test_decode_golden, test_backfill, test_truncation; validation §7.6, §7.9 |
| 5 GPU & precision | GPU availability; GPU tests; FP32/FP16 + labelled gates; FP16 stays gated if unproven | GPU functional tests as before (real CUDA OOM, leases, calibration, CPU/CUDA agreement). C-PREC: CUDA FP32 = CPU FP32 (cosine 1.0 on 10,267 segments); FP16 **G-2 FAIL** (7 segments < 0.999), L-PAIRS G-2 PASS; **G-3 PASS** under OF-4 (2026-10-12 strict reading: FAIL). FP16 spec stays `gated` (TP-4/TP-6) — G-3 passing does not ungate it | **FAIL** (FP16 precision gate G-2); GPU functional **PASS**; FP16 correctly stays gated | validation §7.3, §8.1 |
| 6 Reliability & recovery | retry/timeout/fencing/lease/reconciliation; crash/restart; no undetected false success; visible intake gaps, explicit acceptance | TX-5…TX-11, TC-*, TD-*, TG-5…TG-14, TH-5; 10 defects found by tests or the soak and fixed (X-1…X-10; X-8 raw Scylla `WriteFailure` killed a worker, X-9 service stop → permanent `decode_error`, X-10 one storage error ended the `reconcile run` loop); SOAK-1000 run 5 recovered 13 typed Scylla errors in encoder attempts and 2 in reconcile passes without loss | **PASS** | validation §4, §7.7, §8.3, §8.4 |
| 7 Data & benchmark evidence | corpus + manifest, labelled examples, real-media measurements, approved methods, 1,000-video soak | S-SRC 7 films (≥ 6; licences + SHA-256 verified); C-PREC 63 clips / 14.26 h; L-PAIRS 150 + / 330 − (provenance labels); SOAK-1000 1,030 items / 136.8 h; all measurements executed with the approved methods | **PASS** (evidence complete; the gate *results* are in rows 5 and 8) | data-preparation §7; validation §7.1 |
| 8 Soak & process stability | zero uncontrolled terminations; controlled exits handled; eligible work recovered; sweeper observable; grace > timeout | SOAK-1000 run 5 (corrected corpus `ed3bfd8d…`): **all 8 criteria met**; 3 and 4 only **vacuously** (no controlled exit / single-item OOM occurred — not exercised); 1,000 READY + 30 FAILED as labelled. History: run 2 FAIL (criterion 8, OF-5); runs 3/4 aborted (host shutdown; X-10) | **PASS** | validation §8.4; `results/soak-1000-run5.json` |
| 9 Regression | P8 contract tests; crawler2 integration; no unintended change; V1 unchanged | after OF-4/OF-5/X-10: fingerprinter `tests/v2` 216 passed / 2 skipped (opt-in slow tests, last run §7.5: PASS), cross-service 1 passed; crawler2 `make check` 774/157 and V1 suite 445 from §6.4 (no crawler2 or V1 file changed since) | **PASS** (P8 real-stack tier `test-media.sh` not re-run — §5) | validation §8.5 |
| 10 Documentation & reproducibility | docs, traceability, config/deps, commands, limitations, diff/scope checks | design §22, decisions §0/§5/§5a, ADR-025…027, traceability, validation, data-preparation §7, runbook, lock files, `git diff --check` clean | **PASS** | this directory |

## 3. Plan gates (traceability §4)

| # | Gate | Status | Note |
|---|---|---|---|
| G-1 | **Exit gate:** S1/S2 reuse (7 encodes, not 10) | **PASS** | TR-1/TR-2/TR-3 on the real stack: 7 representations, 7 objects, Σ claims = 7; also with S1 replayed and with an intake restart; crawler2 projection shows ready contents (cross-service test) |
| G-2 | FP16 precision gate (cosine ≥ 0.999 vs CPU FP32) | **FAIL** | C-PREC: 7 of 10,267 segments < 0.999 (min 0.990623); L-PAIRS: 0 of 6,220 (min 0.999275) → FP16 stays `gated` (validation §7.3–§7.4) |
| G-3 | FP16 labelled-decision gate (V1 oracle) | **PASS** (OF-4 interpretation) | L-PAIRS 480 pairs: decisions identical 480/480, alignment identical on all 37 matched pairs; the one non-match partial-alignment difference (`lp-107`) is diagnostic under OF-4 (decisions §5b) (validation §8.1). Strict reading of 2026-10-12: FAIL (§7.4). Does not ungate FP16 (G-2 FAIL) |
| G-4 | OOM recovery | **PASS** (test) | real CUDA OOM → batch halved, no lost/duplicate segments; single-item OOM → controlled exit 75, retryable state, restart completes; device error → 70. Not exercised by SOAK-1000 (no OOM occurred) |
| G-5 | **Exit gate:** GPU inference ≥ 5× CPU (FP32, C-PREC inputs) | **PASS** | C-PREC, 504 frames from all 63 items held in RAM, same tensors: CUDA FP32 43.40 f/s (calibrated B = 2) vs CPU FP32 6.59 f/s (12 threads) = **6.59×**; 3 timed passes; threshold and method unchanged (OF-3) (validation §7.2). End-to-end is 2.12× (reported, no threshold) |
| G-6 | Real-media CPU baseline | **MEASURED** (no threshold) | B-CPU on all of C-PREC: 2.37 s per minute of video, 5.06 segments/s (validation §7.6) |
| G-7 | Two-host GPU+CPU identical representations (simulated) | **PASS** (simulated) | TH-8 / TX-2: same indices and PTS, cosine ≥ 0.999; host processes, not a container (design §22 N-8). Not real multi-machine (G-9) |
| G-8 | **Exit gate:** soak, 1,000 videos | **PASS** | SOAK-1000 run 5 on the corrected corpus (manifest `ed3bfd8d…`): 8/8 criteria; **3 and 4 satisfied only vacuously** (not exercised; covered by G-4, TX-5…TX-8 tests); drain 9,076.8 s (validation §8.4). Run 2 (FAIL, criterion 8) kept as history (§7.9) |
| G-9 | Real multi-machine | **DEFERRED** to P14 | unchanged |

## 4. Definition of done (B.3)

| # | Item | Status |
|---|---|---|
| DoD-1 | Design approved; validation doc | approved 2026-10-10; [validation.md](validation.md) written |
| DoD-2 | Contract + integration tests green | green: latest run 216 passed, 2 skipped — the two opt-in real-scale tests, last run separately and passed (validation §8.5, §7.5); cross-service 1 passed |
| DoD-3 | New-code coverage ≥ 85 % | 89 % of 4,241 statements in the latest run (validation §8.5) |
| DoD-4 | Typed errors, no silent degradation | met: typed exit codes and failure classes; no silent CPU fallback |
| DoD-5 | Metrics | Prometheus metrics in-process; `/metrics` served when `FINGERPRINTER_METRICS_PORT` is set; not scraped/validated against alerts |
| DoD-6 | Config documented, defaults justified | runbook §4; values are the approved initial proposals (§5) |
| DoD-7 | Benchmark gates met and recorded | **met**: all recorded; exit gates G-5 PASS, G-8 PASS (run 5); plan gate G-2 FAIL recorded (FP16 gated, allowed by design §3.7) |
| DoD-8 | Multi-host invariants on the two-host profile | met (simulated, D18) |
| DoD-9 | B.5 re-checked | met: crawler2 unaffected by a stopped fingerprinter (no group on its stream), own keyspace/buckets, derived state rebuildable, no matching, no contract change |
| DoD-10 | Integrated behind config; smoke suite | cross-service test passes; not yet wired into a deployed stack |

## 5. Not done, deferred or left incomplete

- **OF-5 decided (a)** (decisions §5b): policy unchanged; corpus corrected
  and verified at build time; G-8 rerun PASS (run 5). The undetected MP4-cut
  case remains a limitation (§6 item 17).
- **G-2 FAIL** — the FP16 spec stays `gated` (design §3.7): not claimable,
  not default, separate identity. **OF-4 decided** (decisions §5b): G-3 PASS
  under the matched-pairs interpretation; this does not authorize FP16.
- Criteria 3 and 4 of the soak were met only **vacuously** (no controlled
  exit, no single-item OOM occurred in SOAK-1000 runs 2 or 5); recovery from
  those events is evidenced by the tests (G-4, TX-5…TX-8), not by the soak.
- Operator review of the closure: **done 2026-10-15** (decisions §5c). P9 was
  accepted with the vacuous soak criteria 3/4, the FP16 G-2 FAIL and the
  limitations of §6; none of them is resolved by the acceptance.
- Earlier history: OF-1…OF-3 were decided (2026-10-11) and implemented
  (validation §6). Not done: a backfill from `dinov2-seg/1` to
  `dinov2-seg/2` (no deployment holds v1 data; never automatic).
- Not executed: crawler2's P8 real-stack tier (`scripts/test-media.sh`) and
  two-host compose tier (`scripts/test-media-two-host.sh`) — P8 code is
  unchanged; the cross-service test exercises the P8 requester/projection.
- Observation (no change in P9): typed Scylla errors during commit
  completion are logged `attempt_crashed` and count one retryable failure,
  although the outcome converges to READY (validation §7.9; run 5: 13 such
  attempts, §8.4).
- Parameters needing tuning with real data: `attempt_timeout_s` (4 h),
  attempt/work/GPU lease times, `max_claims`, decode threads, calibration
  parameters (frames 64, reps 3, tolerance 5 %), queue size, `max_rss_bytes`;
  MinIO part-copy timeout 120 s vs slow disks (validation §7.5).

## 6. Limitations (required list; all still apply)

1. **Intake loss:** an accepted gap can permanently lose requests until a
   future crawler2 mechanism re-issues them (P8-D7); acceptance is explicit and
   audited, never automatic or reported as recovery.
2. **Candidate-media retention:** candidate bytes are not archived; backfill
   fails for vanished/changed sources and reports them per content
   (`source_lost`); the old representation is kept.
3. **Delivery:** at-least-once; duplicates (same `event_id` after a crash
   between XADD and ack, re-published READY/FAILED for new request events) are
   possible; consumers must be idempotent (crawler2's are).
4. **GPU:** depends on the driver/runtime; the NVML mismatch seen at audit time
   is resolved now but a driver change is caught only by the startup golden
   check; laptop VRAM is shared with the display.
5. **FP16:** a distinct, `gated` identity; never interchangeable with FP32; not
   enabled — its precision gate failed on C-PREC (G-2); G-3 passes only
   under the OF-4 interpretation and does not change that.
6. **Dataset coverage:** real-media evidence comes from 7 openly licensed
   Blender films (3,796 s of source; one, HERO, has the weakest licence
   evidence — data-preparation §7) and corpora derived from them; animated
   films only — no live-action camera footage, no screen recordings or
   piracy-style captures; derived clips are re-encodes of one normalized reel.
7. **Soak coverage:** one passing SOAK-1000 run (run 5: 1,030 items,
   136.8 h of video, 2 h 31 min to drain, one host, one GPU + one CPU
   encoder) after a failed one (run 2); no controlled exit or OOM occurred in
   either (criteria 3/4 vacuous); no multi-day run.
8. **Multi-machine:** simulated two-host only (processes on one host); real
   machines in P14.
9. **DNS rebinding:** not closed (guard resolves, HTTP library resolves again);
   deferred to P14.
10. **NVDEC:** not implemented; decode is CPU ffmpeg.
11. **Event replay:** crawler2's replay re-publishes every event type in the
    window; the fingerprinter cannot enumerate missed event IDs.
12. **Target/representation lifecycle:** spec retirement is refused while a
    spec is default, in a backfill, guarded, or the only READY binding of an
    active target; reference media are purged only when no target version uses
    them; representations are never deleted by target retirement.
13. **Parameters:** initial approved values (§5); the GPU batch is now
    measured per host (`throughput/v1`); calibration costs ≈ 85 s per
    (host, device, spec, dtype) on the RTX 2050 and uses fixed synthetic
    inputs (a ViT's cost does not depend on pixel content).
14. **Environment vs product:** the data gates are no longer blocked by data.
    The dev host's single-node Scylla and MinIO run on a USB HDD: under load
    they produced Scylla LWT timeouts/failures (handled after X-8) and a MinIO
    part-copy timeout (§7.5); heavy jobs must run outside the terminal's
    cgroup (systemd-oomd, runbook §7).
15. **Remaining risks:** K-1 (now also inference-stage margin), K-2 FP16, K-3
    data, K-4 compositor VRAM, K-5 replay window, K-6 runtime drift, K-7 HDD,
    K-8 vanished sources, K-9 laptop interruptions, K-10 held target events,
    K-11 full-scan cost ([decisions.md](decisions.md) §6).
16. **Discovered:** `encode_attempt_log` is written per attempt but nothing
    reads it yet; metrics are not scraped; the CPU fallback and GPU run with
    strict FP32 (TF32 matmul off) — enabling TF32 would be a precision change
    and therefore a new spec.
17. **Truncation detection (OF-1) is partial:** only the two explicit demuxer
    reports of design §3.2a are detected; cuts on element boundaries or in the
    last bytes, MPEG-TS/FLV truncation and MP4 cuts that ffmpeg reports only as
    `Packet corrupt` are accepted — SOAK-1000 showed the latter also for an MP4
    cut to ⅓ of its bytes (OF-5, kept as a documented limit by the operator's
    decision (a); the soak corpus now verifies its expected failures against
    the decoder), wider than §3.2a states; `dinov2-seg/1`
    (still valid, unchanged) never rejects truncation. Indicator strings are
    tied to the pinned ffmpeg (`rt-1`).

## 7. Handoffs

P10: register the `matcher` group, then `publisher register-consumer` and
`republish`; read `representations_by_spec` + artifacts (`fp-seg-npz/v1`);
register `retire_guard` for indexed specs (index the default spec
`dinov2-seg/2`); thresholds (the V1 oracle with today's `MatcherConfig` found
37 of 150 L-PAIRS positives, 0 false positives — validation §7.4). P14: real
multi-machine, DNS rebinding. Operator: closure accepted 2026-10-15
(decisions §5c); OF-4/OF-5 are decided (decisions §5b).

## 8. Git state

See validation §5 (branch, HEAD, changed/untracked files) and §8.7. After
the operator's acceptance (2026-10-15) the P9 work was committed locally in
crawler2 and in the fingerprinter repository, in several reviewable commits.
Nothing was pushed. crawler V1 (`~/anti_piracy/crawler`) not touched; the
fingerprinter repo's V1 packages unmodified.
