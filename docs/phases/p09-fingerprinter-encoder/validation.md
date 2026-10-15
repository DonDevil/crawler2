# P9 Validation — what was run, on what, with which result

Date: 2026-10-10/11; §6 added 2026-10-11 for the operator decisions OF-1…OF-3;
§7 added 2026-10-11/12 for the real-media validation; §8 added 2026-10-12/15
for the operator decisions OF-4/OF-5; §8.7 added 2026-10-15 for the
pre-commit checks after the operator's acceptance (decisions §5c).
Gate evaluation: [exit.md](exit.md). Only results recorded here were actually
executed; nothing below is estimated.

**Authoritative latest results (2026-10-15, §8):** OF-4/OF-5 decided
(decisions §5b). **G-3 PASS** under OF-4 (decisions 480/480, matched-pair
alignment 37/37; 1 non-match alignment difference is diagnostic) (§8.1);
**G-2 still FAIL** on C-PREC → FP16 stays `gated`. SOAK-1000 corrupt set
corrected and verified against the decoder, manifest `ed3bfd8d…` (§8.2);
runs 3 and 4 aborted (host shutdown; harness killed by defect X-10, fixed)
(§8.3); **G-8 PASS** — SOAK-1000 run 5: 8/8 criteria, 1,000 READY + 30
FAILED as labelled; criteria 3 and 4 satisfied **only vacuously** (§8.4).
fingerprinter `tests/v2` **216 passed, 2 skipped**, coverage 89 % of 4,241
statements; cross-service **1 passed** (§8.5).

Superseded where §8 differs — real-media validation, 2026-10-12 (§7):
corpora S-SRC (7 films), C-PREC (63 clips), L-PAIRS (150 + / 330 −),
SOAK-1000 (1,030 items) built and verified (§7.1, data-preparation §7);
**G-5 PASS** 6.59× on C-PREC (§7.2); **G-2 FAIL** on C-PREC (7 of 10,267
segments < 0.999), PASS on L-PAIRS (§7.3–§7.4); **G-3 FAIL as implemented**
(1 non-match alignment difference; decisions 480/480 identical, OF-4);
**G-6 measured** 2.37 s/min (B-GPU-b 1.117 s/min) (§7.6); real 8 GiB and
> 5 GiB storage tests **PASS** (§7.5); **G-8 SOAK-1000 FAIL** on criterion 8
only (one truncated MP4 accepted per design §3.2a, OF-5; criteria 1–7 met, 3
and 4 vacuously) (§7.9); defects X-8/X-9 found by the soak and fixed (§7.7);
fingerprinter `tests/v2` **206 passed, 2 skipped** (the two opt-in slow tests,
run separately and passed), coverage 89 % of 4,233 statements; cross-service
**1 passed** (§7.8).

Superseded (kept as history) — post-correction results of 2026-10-11 (§6.4):
fingerprinter `tests/v2` **197 passed, 1 skipped, 0 failed, coverage 89 %**
(4,212 statements); crawler2 `make check` **774 passed, 157 skipped**;
crawler2 cross-service **1 passed** (two consecutive runs); fingerprinter V1
suite **445 passed**; synthetic soak harness **PASS** 8/8 (not SOAK-1000).
§2–§3 record the **initial implementation run** (spec `dinov2-seg/1`, before
OF-1…OF-3); its fingerprinter test count, coverage and soak result are
**superseded** and kept as history.

## 1. Environment

| | |
|---|---|
| Host | laptop, Intel i5-11400H (12 threads), 15 GiB RAM, USB HDD (`/dev/sda`) for Docker data and datasets |
| GPU | NVIDIA GeForce RTX 2050, 4 GiB, driver 615.71.09, CUDA 13.0 — the NVML mismatch of the audit (kernel 610.57.04 vs 615.71) is **resolved** (`nvidia-smi` works) |
| Stack | crawler2 compose: Scylla 6.2.3, Redis 7.4.2, MinIO (pgsty RELEASE.2026-08-04) |
| Runtime profile `rt-1` | ffmpeg 6.1.1-3ubuntu5 (buildconf sha256 `f1e6bf8a…`), Python 3.12.3, torch 2.13.0+cu130, transformers 5.15.1, numpy 2.5.2, Pillow 12.3.0, model `facebook/dinov2-base@f9e44c81…` (3 files hashed); cuda variant CUDA 13.0 / cuDNN 92000 |
| Spec | initial run: `dinov2-seg/1` FP32 `sha256:ddfc71c1ad8b…`, FP16 (gated) `sha256:4dadb2a089e3…`; latest run (§6): default `dinov2-seg/2` FP32 `sha256:a4df3d19…`, FP16 (gated) `sha256:a765d7d2…` |
| Dependencies added (D12) | scylla-driver 3.29.12, minio 7.2.15, pydantic 2.13.5, prometheus-client 0.26.0, pytest-cov 7.0.0, antipiracy-contracts 1.1.0 — installed with a constraints file of the previous `pip freeze`: **additions only, no upgrades**; lock: fingerprinter `requirements-p9.lock` |

## 2. Tests

### 2.1 Commands and results — initial implementation run (superseded; latest: §6.4)

| Command (cwd) | Services | Result |
|---|---|---|
| `. scripts/p9-env.sh && env/bin/python -m pytest -p no:cacheprovider tests/v2 -q --cov=fingerprinter` (fingerprinter) | **real** Scylla/Redis/MinIO, **real** RTX 2050, real ffmpeg/model; subprocess encoders | initial run: 161 passed, 1 skipped, 0 failed (16 m 57 s) — **superseded** by §6.4 (197 passed, 1 skipped); skipped = opt-in real 8 GiB acquisition (`RUN_P9_SLOW=1`), **not run** in either |
| `scripts/test-fingerprinter-p9.sh -q` (crawler2) | real stack; fingerprinter as separate CLI processes; crawler2's real `EncodeRequester` and `RepresentationLoop` | initial run (spec v1): 1 passed (two consecutive runs); re-run on spec v2 in §6.4 |
| `env/bin/ruff check . && env/bin/ruff format --check . && env/bin/mypy && env/bin/pytest -q` (crawler2, = `make check`) | unit/contract (integration tiers skip without the stack) | ruff clean, format clean, mypy clean (248 files), **774 passed, 157 skipped** (equal to the P8 closure baseline; same result re-confirmed in §6.4) |
| `env/bin/python -m pytest tests --ignore=tests/v2 -q` (fingerprinter V1 suite) | V1 Redis db 15 | **445 passed** (same result re-confirmed in §6.4) |
| crawler2's `ruff` on fingerprinter P9 code (`--select E,F,W,B,UP,SIM,BLE --ignore E501`) | — | **corrected 2026-10-11:** not clean as stated before — 7 style findings (SIM102 ×2, SIM105 ×5) in `encoder.py` (written in the first P9 pass; style only, no behaviour); 0 others, none in the code added for OF-1/OF-2 |

Per file in the initial run (fingerprinter `tests/v2/`; the latest run adds
36: truncation 17, calibration 11, identity/spec 3, encoder failures 5 —
calibration report/reuse, superseded-row recalibration, two calibration
failure exits, two startup-lease tests): identity/spec/runtime/preprocess unit 28,
decode golden fixtures 8, intake/gaps 17, requests/due index 18, commit/
reconciler/sweeper 15, events 6, sources 22 (1 skipped), targets 13,
backfill/lifecycle 7, encode (incl. S1/S2) 6, encoder failure classes 16
(5 on the real GPU), simulated two-host 3 (subprocesses, `kill -9`), CLI 3.
Mocks are used only for fault injection (embedder hook raising OOM/device
errors, an LWT raising "unknown outcome", a monkeypatched `torch.cuda.is_available`);
every persistence, stream and object-store assertion runs against the real
services. Real-GPU tests: CPU/CUDA artifact agreement, CUDA golden check,
real `torch.cuda.OutOfMemoryError` recovery (VRAM reserved), GPU lease
exclusivity, GPU lease loss, calibration, GPU+CPU host processes together.

### 2.2 Coverage (new package `fingerprinter/`)

Initial run: **89 %** of 4,031 statements (run 3) — superseded; latest
(§6.4): **89 %** of 4,212 statements. Lowest: `reconcile.py` 78 % (full-pass
target branches, storage-error paths), `cli.py` 82 %, `encoder.py` 86 %. DoD-3
(≥ 85 % of new code) is met in total.

### 2.3 Contract and scope checks

No file under crawler2 `contracts/` or `crawler2/` (production code) changed;
P8 behaviour is exercised unchanged by the cross-service test (READY for two
contents, FAILED on `default` for a 404 source, suppression after READY, no
re-request after FAILED, `media-registry` pending = 0). No `match.found`
producer or consumer was added. V1 fingerprinter packages are unmodified
(only new untracked paths in that repo); crawler V1 (`~/anti_piracy/crawler`)
was not touched (its pre-existing uncommitted changes are unrelated).

## 3. Benchmarks (all in fingerprinter `benchmarks/p9-encoder/results/`)

### 3.1 B-GPU-a — inference stage, synthetic inputs (gate needs C-PREC → NOT MEASURED)

256 frames decoded+preprocessed once from `synthetic-mini` (manifest sha256 in
the JSON); 1 warm-up discarded; 3 timed passes (n = 3 per mode:
`small_sample: true`).

| Mode | Batch | Frames/s (median) | Report |
|---|---|---|---|
| CPU FP32, 12 threads | 8 | 6.91 | `b-gpu-a-synthetic-mini.json` |
| CUDA FP32 | 32 | 27.7 → **4.01×** | same |
| CUDA FP16 (gated spec, not usable for the gate) | 32 | 174.6 | same |
| CPU FP32, 12 threads | 16 (best of 1/4/8/16/32 sweep) | 7.05 | `b-gpu-a-synthetic-mini-best-batch.json` |
| CUDA FP32 | 8 (best of sweep) | 40.2 → **5.70×** | same |
| CUDA FP16 (gated) | 8 | 158.2 | same |

Sweep (CUDA FP32, measured in both orders with clocks/temperature): B 4–8 ≈ 41,
B 16–64 ≈ 26–28 frames/s — a batch-size effect, not throttling (OF-2). Strict
FP32 (TF32 matmul off); enabling TF32 would be a precision change = new spec.

### 3.2 Not measured

B-CPU and B-GPU-b end-to-end on real media, B-DEC on real media, B-PREC
(precision and labelled decisions), B-SOAK on SOAK-1000: the corpora do not
exist (data-preparation §7). The audit's 1.80 s/min synthetic V1 CPU figure
remains a reference only.

### 3.3 Soak harness run (synthetic; not the gate)

`soak.py --corpus synthetic-mini --devices cuda,cpu --orphan-grace-s 240
--attempt-timeout-s 180 --attempt-lease-s 60` (shortened values recorded,
design §13.3 #7) → `soak-harness-synthetic-mini.json`; logs in
`~/anti_piracy/datasets/p9/runs/` (not in git). 6.3 min, 66 items.

| Criterion | Result |
|---|---|
| 1 zero uncontrolled terminations | met (all exits code 0, reported) |
| 2 zero lost requests after drain + 2 × lease | met (61 READY, 5 FAILED, 0 non-terminal) |
| 3 controlled exits reported/recovered | met (none occurred) |
| 4 single-item-OOM items retryable | met (none occurred) |
| 5 leases released/expired | met |
| 6 RSS/VRAM bounded, no upward trend | met (RSS ≈ 1.29 GiB flat; VRAM steady ≈ 0.52 GiB, 3.1 GiB peak during calibration) |
| 7 zero unreferenced objects, zero open intents after sweep | met (61 objects = 61 rows, 0 intents) |
| 8 expected failures for corrupt/audio-only, no crash | **not met**: `corrupt-00.webm` (truncated to a third) became READY — OF-1 |

Harness result **FAIL**; gate status **NOT MEASURED** (not SOAK-1000).
Superseded by the rerun with `dinov2-seg/2` after OF-1 (§6.3: harness PASS).

## 4. Defects found by the tests and fixed

| # | Found by | Defect | Fix |
|---|---|---|---|
| X-1 | target tests under load | an encoder could idle-exit right after its scheduler enqueued fresh work (scan slower than the idle window) | fresh scheduled work resets idleness and is claimed immediately |
| X-2 | TX-8 | on a controlled exit, a worker dropped its attempt from the in-flight set before the main thread released it → request stayed IN_PROGRESS until lease expiry | worker releases its own attempt on stop |
| X-3 | TX-7 | cancellation during decode was recorded as an attributed `timeout` failure | cancellation routes through stop/timeout/supersede handling |
| X-4 | TR-6 writing | a target registered after a default-spec change waited for a backfill (binding stuck PENDING) | target requests its own spec explicitly (design §22 N-7) |
| X-5 | full suite (TX-7) | a claim landing after the controlled-exit release sweep stayed IN_PROGRESS | such claims are released at once |
| X-6 | full suite (TX-8) | worker and main-thread releases raced; the process could exit before the release landed | per-attempt release lock; workers joined before a controlled exit returns |

| X-7 | full suite after OF-2 (TX-3, TH-4/TH-8), 2026-10-11 | the GPU lease was not renewed between acquisition and the supervisor start; a calibration longer than its TTL let a second process take the GPU | startup lease keeper; lease lost during startup → exit 75 (§6.2) |

After X-5/X-6 the controlled-exit tests passed 24/24 over 6 repetitions and
the full suite passed.

## 5. Git state at the end

- crawler2: branch `main`, HEAD `2e397c1`, ahead of `origin/main` by 29
  (unchanged, nothing pushed). Modified: `README.md`, `docs/adr/README.md`,
  `docs/architecture/README.md`, `docs/benchmarks.md`, `docs/v2-phase-plan.md`.
  New: `docs/adr/ADR-025…027`, `docs/architecture/fingerprinter-encoder.md`,
  `docs/phases/p09-fingerprinter-encoder/` (all files), `scripts/test-fingerprinter-p9.sh`,
  `tests/integration/fingerprinter/`. `git diff --check` clean.
- fingerprinter: branch `main`, HEAD `904af14` (level with origin). New,
  untracked: `fingerprinter/`, `tests/v2/`, `benchmarks/p9-encoder/`,
  `scripts/p9-env.sh`, `requirements-p9.txt`, `requirements-p9.lock`; the
  pre-existing untracked `benchmarks/results/bench_embedding_…json` is untouched.
  No V1 file modified.
- Nothing staged, committed or pushed in any repository.

## 6. Operator decisions OF-1…OF-3 — implementation and evidence (2026-10-11)

Decisions: [decisions.md](decisions.md) §0a. Design: §3.2a, §7.4, §12.3, §13.3, §22.
Only executed results are recorded.

### 6.1 OF-1 — truncated media (`dinov2-seg/2`)

- New spec version `dinov2-seg/2` (FP32 `sha256:a4df3d19252891dc…`, gated FP16
  `sha256:a765d7d2979264…`); `dinov2-seg/1` digests unchanged
  (`ddfc71c1…`, `4dadb2a0…`, pinned by `test_spec_v1_identity_unchanged_…`).
  Built-in default for new installations and for the test suite, the soak
  harness, the benchmarks and the crawler2 cross-service test: `dinov2-seg/2`.
- Indicators found by experiment with the pinned ffmpeg 6.1.1 (truncation to
  ⅓, 90 % and −100 B of MP4 faststart, MP4 moov-at-end, MKV, WebM, MPEG-TS,
  AVI, FLV): MKV/WebM print `File ended prematurely` (⅓ and 90 %, exit 0);
  MP4 prints `stream 0, offset 0x…: partial file` (⅓ and 90 %, exit 0); a cut
  of the last 100 B gives no indicator in MKV/WebM and only `Packet corrupt`
  in MP4; TS/FLV give no truncation report (corruption/concealment only);
  moov-at-end MP4 and AVI fail to open (non-zero exit / ffprobe). Hence the two
  indicators of design §3.2a and its list of undetectable forms.
- Tests: `tests/v2/test_truncation.py` — 17 passed: truncated MP4/MKV/WebM at
  ⅓ and 90 % → `decode_error` `truncated_input: <indicator>` with ffmpeg exit 0
  asserted (6); `dinov2-seg/1` still decodes the prefix (3); complete media
  decode bit-identically under v1 and v2 (3); moov-at-end truncated MP4 stays
  `unsupported_format` (1); benign `deprecated pixel format` warning accepted
  (1); indicator text in metadata accepted (1); mid-stream corruption warnings
  accepted (1); anchored matching (1). Identity: 3 new unit tests (v1 digests
  pinned, v2 identity/version, closed policy values). Decode golden fixtures
  unchanged (8 passed). First targeted run: truncation + identity + decode
  golden = **56 passed**.
- Soak criterion 8 now met (§6.3).

### 6.2 OF-2 — batch calibration `throughput/v1`

- Code: `fingerprinter/calibration.py` (new), `encoder._calibrated_batch` /
  `encoder.calibrate`, `encoder_hosts.calibration` (column added idempotently
  by `migrate`), config `calibration_frames` 64 / `calibration_reps` 3 /
  `calibration_tolerance` 0.05, metric `encoder_batch_calibrated_max`.
- Unit tests `tests/v2/test_calibration.py`: **11 passed** (fake embedder with
  exact per-B throughput, noise and OOM). Encoder tests: stored report and
  reuse on the real GPU, superseded `B_max/2` row recalibrated, calibration
  failure → exit 75 (`batch 1 does not fit`) / exit 70 (`device_error`) with
  the GPU lease released.
- **Defect found and fixed (X-7):** the full suite failed TX-3 and TH-4/TH-8
  (2 failed, 193 passed, 1 skipped, 23 m 42 s) because the GPU lease
  (30 s TTL) was renewed only by the supervisor thread, which starts after
  model load, golden check and calibration; the new calibration (≈ 85 s)
  outlived the TTL, so a second process could take the GPU. Fix: a startup
  lease keeper renews from acquisition until the supervisor runs; a lease lost
  during startup is a controlled exit 75 (`gpu_lease_lost during startup`).
  Regression tests: slow startup keeps the lease (second owner → 73) and lease
  stolen during startup → 75 without releasing the other holder's lease. TX-4's
  fault hook (which steals the lease on every `embed`) is now armed after
  startup so TX-4 keeps testing lease loss *while encoding*; the startup case
  has its own test. After the fix: TX-4 + two-host 4 passed; lease/calibration
  subset 9 passed before the TX-4 adjustment.
- B-CAL (`benchmarks/p9-encoder/calibrate.py --runs 2`, CUDA FP32, encoder
  defaults, RTX 2050 driver 615.71.09, torch 2.13.0+cu130, SM 1575–1665 MHz,
  48 → 77 °C) → `results/b-cal-rtx2050.json`:

| B | run 1 median f/s (min–max) | run 2 median f/s | peak reserved VRAM |
|---|---|---|---|
| 1 | 35.60 (35.59–35.63) | 35.46 | 388 MiB |
| **2** | **44.17 (44.00–44.35)**, 6 samples | **43.90**, 6 samples | 404 MiB |
| 4 | 40.77 | 40.51 (6 samples) | 446 MiB |
| 8 | 40.89 (39.75–41.47), 6 samples | 40.57 | 496 MiB |
| 16 | 27.00 | 26.80 | 650 MiB |
| 32 | 27.77 | 27.55 | 878 MiB |
| 64 | 26.42 | 26.12 | 1352 MiB |
| 128 | 17.20 | 17.17 | 2300 MiB |
| 256 | OOM → ineligible | OOM → ineligible | — |

  Selected **B = 2 in both runs** ("highest median throughput"); B_max = 128.
  Before (superseded rule): `B_max/2` = 64 → 26.4 / 26.1 f/s; after: 44.2 /
  43.9 f/s (+67 %). Spread of every candidate ≤ 4.2 %. ≈ 85 s per calibration
  (3 m 37 s for both runs incl. model loads).

### 6.3 OF-3 — G-5 unchanged; synthetic rerun; soak harness rerun

- `bench_inference.py` now takes the GPU batch from the encoder calibration
  (and records it) and the CPU batch from `encoder.cpu_batch`. Rerun on
  synthetic-mini (256 tensors, 3 reps) → `results/b-gpu-a-synthetic-mini-calibrated.json`:
  CPU FP32 6.77 f/s (B 8, 12 threads); CUDA FP32 43.82 f/s (B 2) → **6.47×**;
  CUDA FP16 (gated spec) 118.4 f/s — not used. **G-5: NOT MEASURED** (not
  C-PREC; threshold 5× and method unchanged). Inference-only; no end-to-end
  figure was measured (B-CPU/B-GPU-b need real media).
- Soak harness (same command and shortened values as §3.3, spec
  `dinov2-seg/2`) → `results/soak-harness-synthetic-mini-spec2.json`, 430.9 s
  (drain 189.1 s). A first rerun (also all criteria met) recorded each process
  exit several times — a harness bookkeeping bug (an already-recorded exit was
  appended again on every poll); fixed in `soak.py`, run repeated; the first
  report is kept outside git (`datasets/p9/runs/soak-harness-spec2-run1-…json`).

| Criterion | Result (run 2) |
|---|---|
| 1 zero uncontrolled terminations | met: 2 processes (cuda, cpu), both exit 0, reported |
| 2 zero lost requests after drain + 2 × lease | met: 60 READY, 6 FAILED, 0 non-terminal |
| 3 controlled exits reported/recovered | met — **vacuously** (no controlled exit occurred; 0 restarts) |
| 4 single-item-OOM items retryable | met — **vacuously** (none occurred) |
| 5 leases released/expired | met: 0 leases left |
| 6 RSS/VRAM bounded, no upward trend | met: RSS peak 1425 / 1304 MiB (cuda/cpu), Q2→Q4 median 1311→1280 / 1287→1232 MiB; VRAM peak 3544 MiB (during calibration), Q4 median 510 MiB |
| 7 zero unreferenced objects, zero open intents after sweep | met: 60 objects = 60 rows, sweep deleted 0, 0 intents; 66 events acked |
| 8 expected failures for corrupt/audio-only, no crash | **met**: `corrupt-00.webm` → `decode_error` `truncated_input: matroska_file_ended_prematurely`; 3 corrupt → `unsupported_format` (unrecognized container); 2 audio-only → `unsupported_format` (no video stream) |

Harness result **PASS**; G-8 gate status **NOT MEASURED** (the corpus is not
SOAK-1000; 66 items, 7 minutes).

### 6.4 Full runs after the corrections

| Command (cwd) | Result |
|---|---|
| `. scripts/p9-env.sh && env/bin/python -m pytest -p no:cacheprovider tests/v2 -q --cov=fingerprinter` (fingerprinter; real stack + RTX 2050), before X-7 | 2 failed (TX-3, TH-4/TH-8), 193 passed, 1 skipped — led to X-7 |
| same, after X-7 | **197 passed, 1 skipped, 0 failed** (21 m 52 s); skipped = opt-in real 8 GiB acquisition, not run; coverage **89 %** of 4,212 statements (`calibration.py` 97 %, `spec.py` 97 %, `media.py` 94 %, `encoder.py` 86 %) |
| `env/bin/ruff check . && env/bin/ruff format --check . && env/bin/mypy && env/bin/pytest -q` (crawler2) | clean; mypy 248 files clean; **774 passed, 157 skipped** (unchanged baseline) |
| `scripts/test-fingerprinter-p9.sh -q` (crawler2) | first run 1 failed: the test still expected the projected spec version `"1"`; the expectation was updated to `"2"` (the test now registers `dinov2-seg/2`; same exact-match assertion) → **1 passed** on two consecutive runs |
| `env/bin/python -m pytest tests --ignore=tests/v2 -q` (fingerprinter V1 suite) | **445 passed** (no dependency or V1 change) |

No existing assertion was relaxed. Two test-side changes are behavioural
consequences of the decisions and are explained above: TX-4 arms its
lease-stealing hook after startup (§6.2), the cross-service test expects spec
version 2. The spec v1 golden decode fixtures are unchanged.

### 6.5 Git state after the corrections

Same as §5 (no staging, no commit, no push; crawler2 `main` `2e397c1`, 29
ahead of origin; fingerprinter `main` `904af14`). Changed in this step —
crawler2 (modified/untracked as before): P9 `decisions.md`, `design.md`,
`traceability.md`, `validation.md`, `exit.md`, `data-preparation.md`,
`docs/architecture/fingerprinter-encoder.md`, `docs/benchmarks.md`,
`docs/v2-phase-plan.md`, `tests/integration/fingerprinter/test_p9_cross_service.py`.
fingerprinter (all within the untracked P9 paths): `fingerprinter/spec.py`,
`media.py`, `calibration.py` (new), `encoder.py`, `config.py`, `db.py`,
`model.py`, `metrics.py`, `cli.py`; `tests/v2/test_truncation.py` (new),
`test_calibration.py` (new), `test_identity_unit.py`,
`test_encoder_failures.py`, `conftest.py`; `benchmarks/p9-encoder/calibrate.py`
(new), `bench_inference.py`, `soak.py`, `common.py`, `bench_e2e.py`,
`precision.py`; results `b-cal-rtx2050.json`,
`b-gpu-a-synthetic-mini-calibrated.json`, `soak-harness-synthetic-mini-spec2.json`
(small JSON reports; run logs and scratch kept outside git in
`~/anti_piracy/datasets/p9/runs/`). No V1 file and no crawler V1 file changed.

## 7. Real-media validation (2026-10-11, evening)

Only executed results are recorded; sections are added as runs finish.
Heavy jobs ran as separate `systemd-run --user` units with a memory cap and a
pressure watchdog that freezes them while user-slice memory pressure is high
(earlier sessions were killed by systemd-oomd on memory pressure from such
jobs; the watchdog froze/thawed a job once, never during a timed benchmark).
Corpora, logs and scratch: `~/anti_piracy/datasets/p9/` (not in git); result
JSONs: fingerprinter `benchmarks/p9-encoder/results/`.

### 7.1 Corpora (data-preparation §7)

| Set | Manifest SHA-256 | Content |
|---|---|---|
| S-SRC | `6254a8ca…` | 7 films, 3,795.7 s; licences and evidence per item; SHA-256 of all 7 files re-verified 2026-10-11 (7/7 OK) |
| reel | `50de67d9…` (reel file) | 7 films normalized to 1920×800/24 fps H.264, 3,790.9 s |
| C-PREC | `af5826bc…` | 63 clips, 14.26 h: 54 balanced ({360p, 720p, 1080p} × {H.264, HEVC, VP9} × {24, 30, 60} fps, 2 each, 900 s) + 9 hard cases (300 s: dark, flat colour, high motion, letterbox, pillarbox, BT.601, full range, VFR, PTS offset); seed 9; 63 distinct SHA-256; 0 probe errors. Build: libx265 segfaulted twice on 1080p60 HEVC clips (`segfault at 0 … libx265.so.199`, intermittent: the same clip succeeded on resume); outputs are recorded only after completion, so no partial file entered the manifest |

### 7.2 G-5 — B-GPU-a on C-PREC (exit gate)

`env/bin/python benchmarks/p9-encoder/bench_inference.py --corpus ~/anti_piracy/datasets/p9/c-prec --per-item 8 --frames 504 --reps 3 --out benchmarks/p9-encoder/results/b-gpu-a-c-prec.json`
(fingerprinter; run alone, no other job on the host).
Inputs: 504 preprocessed frames held in RAM, the first 8 segments of each of
the 63 C-PREC items (303 MB of tensors); every mode embeds the same tensors;
1 warm-up batch discarded; 3 timed passes. i5-11400H (12 threads), RTX 2050
4 GiB, driver 615.71.09, torch 2.13.0+cu130 (CUDA 13.0), ffmpeg 6.1.1-3ubuntu5,
spec `dinov2-seg/2` FP32 `sha256:a4df3d19252891dc0d8d266d5056a226159ffc276898fdfd680472e54497d3d5`.
GPU batch from the encoder's calibration (`throughput/v1`) in the same run:
B = 2 (the CUDA OOM warnings in the log are the calibration's B = 256 probe).

| Mode | Batch | Frames/s, median of 3 (min–max) |
|---|---|---|
| CPU FP32, 12 threads (`encoder.cpu_batch`) | 8 | 6.59 (6.52–6.60) |
| CUDA FP32 (default spec) | 2 | 43.40 (43.35–43.46) |
| CUDA FP16 — gated spec `sha256:a765d7d2…`, **not** usable for the gate | 2 | 111.88 (111.44–112.95) |

**GPU FP32 / CPU FP32 = 6.59× ≥ 5× → G-5 PASS.** Inference stage only; it
says nothing about end-to-end speed (§7.4). Small-sample note: n = 3 passes
per mode (the approved minimum of 3 repetitions).

### 7.3 G-2 — FP16 precision gate on C-PREC (TP-1/TP-2)

`env/bin/python benchmarks/p9-encoder/precision.py --clips ~/anti_piracy/datasets/p9/c-prec --out benchmarks/p9-encoder/results/b-prec-c-prec.json`
(2 h 33 min; concurrent with other jobs — no timing is measured here). Every
item is decoded once with `dinov2-seg/2` and the same tensors are embedded by
CPU FP32 (12 threads), CUDA FP32 and CUDA FP16 (gated spec).

| Check | Result |
|---|---|
| TP-1 alignment | identical by construction; 63 items, **10,267 segments**, vector count = decoded segment count for every mode |
| TP-2 CUDA FP16 vs CPU FP32 | median 0.999995, p95 0.999998, **min 0.990623; 7 of 10,267 segments < 0.999** |
| (reference) CUDA FP32 vs CPU FP32 | min 1.000000, 0 below 0.999 |

**G-2 on C-PREC: FAIL** (the criterion is per segment; 7 segments miss it).
Consequence per design §3.7 / TP-4: the FP16 spec stays `gated` — not
claimable, not default; FP32 remains the default. The report keeps aggregate
statistics only; which items hold the 7 segments was not recorded.

### 7.4 G-2 on L-PAIRS and G-3 — labelled decisions (TP-1…TP-3)

`env/bin/python benchmarks/p9-encoder/precision.py --clips ~/anti_piracy/datasets/p9/l-pairs --pairs ~/anti_piracy/datasets/p9/l-pairs/l-pairs.csv --out benchmarks/p9-encoder/results/b-prec-l-pairs.json`
(1 h 50 min). L-PAIRS manifest `ab3d7711…`, `l-pairs.csv` `ff43168c…`:
**480 pairs = 150 positives + 330 negatives**; 7 targets (the S-SRC
originals), 150 film excerpts (31–562 s, 6.82 h; 5 transforms: re-encode,
360p, 10 % crop, letterbox, pillarbox; H.264/HEVC/VP9), 30 synthetic clips.
Labels are provenance only (clip cut from the target's film, recorded by the
generator; seed 150); a check of every row against the recorded source film
found 0 mismatches; no model output was used to build or check labels.

| Check | Result |
|---|---|
| TP-1 alignment | identical by construction; 187 items, 6,220 segments |
| TP-2 CUDA FP16 vs CPU FP32 | median 0.999995, **min 0.999275, 0 of 6,220 < 0.999 → G-2 on L-PAIRS PASS** (CUDA FP32 vs CPU: min 1.000000) |
| TP-3 V1 oracle `match_segments`, today's `MatcherConfig` (0.9 / 0.6 / 3 / 1 / 2), FP16 vs CPU FP32 | match decision identical on **480/480** pairs (37 matched in both); alignment identical on all 37 matches; **1 difference**: `lp-107-tears-of-steel-re-encode.webm` vs its target (label 1), *not matched* in both, best partial alignment FP32 target 400.0–415.0 s / 2 segments vs FP16 400.0–405.0 s / 1 segment |
| informative (no P9 threshold) | CPU FP32 oracle vs labels: TP 37, FN 113, FP 0, TN 330 |

**G-3: FAIL as implemented** — `precision.py` requires the whole oracle
result tuple (decision, both alignment ranges, matched-segment count) to be
equal for every pair, including non-matches; one non-match differs in its
partial alignment. Whether "matched-segment alignment" (design §13.3) is meant
to cover non-matches is an open interpretation for the operator (exit.md §5);
the criterion was not relaxed. FP16 stays `gated` either way (G-2 failed on
C-PREC, §7.3). The low oracle recall (37/150) concerns P10 thresholds, not P9.
*Superseded 2026-10-12:* the operator decided OF-4 (matched pairs only); the
rerun in §8.1 gives G-3 PASS. This strict-reading FAIL stays recorded.

### 7.5 Real-scale storage tests (opt-in `RUN_P9_SLOW=1`; real stack)

`. scripts/p9-env.sh && RUN_P9_SLOW=1 env/bin/python -m pytest -p no:cacheprovider <test> -v --basetemp=<dir under datasets/p9/runs>`

| Test | Runs | Result |
|---|---|---|
| `test_sources.py::test_ts6_real_8gib_boundary` (TS-6: sparse files over loopback, exactly 8 GiB accepted and hashed, 8 GiB + 1 refused from the declared length) | run 1: **failed** — `source_unreachable: ReadTimeout` after 60 s. Cause: the **test fixture** (`tests/v2/webserver.py`) read the whole file into RAM before sending headers (process RSS 2.9 GiB throttled at its cgroup limit; this is also what drove the memory pressure that killed earlier sessions). The Acquirer's typed failure was correct. Fix (test infrastructure only): plain/`ref` routes stream from disk in 1 MiB chunks; `gzip`/`short`/`slow` unchanged. Regression: `test_sources.py` + `test_targets.py` **34 passed, 2 skipped** (the two opt-in tests). Run 2: **PASSED** (863 s; RSS ≈ 120 MB) | **PASS** |
| `test_targets.py::test_tr7_real_multipart_copy_above_5gib` (TR-7: 5 GiB + 64 MiB of real bytes, production single-copy limit 5 GiB, 512 MiB server-side part copies, SHA-256 re-read, purge) | run 1 (concurrent with the SOAK-1000 corpus build on the same USB disk): **failed** — one `UploadPartCopy` exceeded the MinIO client read timeout (120 s) → typed `StorageUnavailableError`; teardown removed the test buckets (checked). Run 2 (no other job on the host): **PASSED** (1,648 s) | **PASS** (idle disk). Finding: each 512 MiB part copy must finish within 120 s, i.e. > ≈ 4.3 MB/s sustained copy throughput; a saturated USB HDD did not provide it. No production value was changed |

### 7.6 G-6 and B-GPU-b — end-to-end on C-PREC (each run alone on the host)

`. scripts/p9-env.sh && env/bin/python benchmarks/p9-encoder/bench_e2e.py --corpus ~/anti_piracy/datasets/p9/c-prec --device {cpu|cuda} --out benchmarks/p9-encoder/results/b-{cpu|gpu-b}-c-prec.json`
— full pipeline on the real stack: local fixture HTTP server → acquisition →
decode → preprocess → inference → artifact upload → Scylla commit →
publication; one encoder; all 63 items, 855.62 min of video; wall time
includes model load and golden check (cold start).

| Run | Result | Wall | s per minute of video | segments/s | Stage sums (s; overlapping threads) | Peak RSS / VRAM |
|---|---|---|---|---|---|---|
| **B-CPU (G-6 real-media CPU baseline)**, CPU FP32 B = 8, 12 threads | 63/63 READY, exit 0 | 2,027.6 s | **2.37** | 5.06 | decode 5,099.7, infer 1,995.0, acquire 427.1, commit 38.6 | 1,541 MiB |
| B-GPU-b (reported, no threshold), CUDA FP32 B = 2 (calibrated) | 63/63 READY, exit 0 | 955.9 s | **1.117** | 10.74 | decode 2,210.4, infer 296.3, acquire 231.2, commit 35.4 | 1,526 MiB; VRAM max reserved 3,452 MiB (calibration) |

End-to-end GPU/CPU = **2.12×** (inference stage alone: 6.59×, §7.2) — CPU
decode dominates, as design §13.3 anticipated; no end-to-end threshold exists
(P9-D17). **G-6: measured** (it has no threshold; it is the baseline the
design asked for). The B-GPU-b JSON's `gate_status` string reads "measured
(G-6 baseline)" — a labelling slip in `bench_e2e.py` (the CUDA run is B-GPU-b,
not G-6), corrected in the script after the run; the numbers are unaffected.
The 1.80 s/min synthetic figure (audit §7) is superseded as the CPU reference
by 2.37 s/min on real media.

### 7.7 SOAK-1000 run 1 (aborted) — defects X-8, X-9 found and fixed

Run 1 (`soak.py --corpus …/soak-1000 --devices cuda,cpu --attempt-timeout-s 3600
--attempt-lease-s 600 --orphan-grace-s 3900 --max-hours 48`) started
2026-10-12 01:50 and was **stopped by the operator session at 02:03** (43
READY) because it exposed a defect; logs:
`~/anti_piracy/datasets/p9/runs/soak-1000-run1-aborted/`. It is **not** a gate
result.

| # | Found by | Defect | Fix | Regression tests |
|---|---|---|---|---|
| X-8 | run 1, 01:57 | under load the single-node dev Scylla answered two LWTs with `WriteTimeout` and `WriteFailure` (code 1500, "replica failed to execute write"). `Db` mapped timeouts to typed errors but let `WriteFailure`/`ReadFailure` escape raw; in `_claim()` the raw exception **killed an encoder worker thread** (process kept running degraded; had all workers died, `run()` would have reported a *normal* exit 0) | `db.py`: `CoordinationFailure` (Read/WriteFailure) → `StorageUnavailableError`; an LWT `WriteFailure` → `LwtUnknownError` (outcome unknown, resolved by SERIAL read as for timeouts). `encoder._worker`: any other exception escaping `_claim()` is logged (`claim_crashed`) and becomes a controlled exit 75 (reported, restarted) instead of a dead thread | `test_db_errors.py` (4), `test_claim_crash_is_a_controlled_exit_not_a_dead_worker` |
| X-9 | stopping run 1 | the stop signal (like any `systemctl stop` with the default cgroup kill) also reached the ffmpeg children; the 5 in-flight attempts became **permanent `decode_error` failures of valid media** (`ffmpeg exit 255 … received signal`, `short frame read`) | `media.decode`: a decoder ended by a signal (negative exit code or ffmpeg's "received signal") → `internal_error`, retryable, attempt scope; ffprobe killed by a signal likewise. `encoder._process`: an `EncodeError` raised while the encoder is draining/stopping goes to the existing cancellation path (released, no failure counted) | `test_decoder_killed_by_signal_is_retryable_not_decode_error[SIGTERM, SIGKILL]`, `test_complete_media_still_decodes_after_signal_check`, `test_tx5_service_stop_also_signalling_ffmpeg_releases_not_fails` |

Both are within P9 code; `internal_error` already exists in the frozen
`EncodeFailure` contract enum (no contract change); decode output and spec
identities are unchanged. Targeted run: `test_db_errors.py` +
`test_truncation.py` + `test_encoder_failures.py` → 46 passed, 1 failed (the
new SIGTERM test waited for ffmpeg without draining its pipe — a test bug;
fixed: keep reading after the signal) → signal tests 3 passed.

### 7.8 Regression runs after X-8/X-9 and the fixture fix (2026-10-12)

| Command (cwd) | Result |
|---|---|
| `. scripts/p9-env.sh && env/bin/python -m pytest -p no:cacheprovider tests/v2 -q --cov=fingerprinter` (fingerprinter; real stack + RTX 2050) | **206 passed, 2 skipped, 0 failed** (22 m 44 s); 208 collected; skipped = the two opt-in real-scale tests (`RUN_P9_SLOW`), which passed separately (§7.5); coverage **89 %** of 4,233 statements |
| `scripts/test-fingerprinter-p9.sh -q` (crawler2) | **1 passed** (118 s) |
| crawler2 `ruff` on the changed fingerprinter files (`--select E,F,W,B,UP,SIM,BLE --ignore E501`) | clean, except the 7 pre-existing `encoder.py` style findings of §2.1 (unchanged count, none new) |

Not re-run (unchanged since §6.4, nothing they cover changed): crawler2
`make check` (774 passed, 157 skipped) and the fingerprinter V1 suite (445
passed) — no crawler2 file and no V1 file was modified; the fingerprinter
changes are within the untracked P9 package, its tests and benchmarks.

### 7.9 G-8 — SOAK-1000 run 2 (the gate run)

`. scripts/p9-env.sh && env/bin/python benchmarks/p9-encoder/soak.py --corpus ~/anti_piracy/datasets/p9/soak-1000 --devices cuda,cpu --attempt-timeout-s 3600 --attempt-lease-s 600 --orphan-grace-s 3900 --max-hours 48 --out benchmarks/p9-encoder/results/soak-1000.json`
(fingerprinter; code after X-8/X-9; real stack; RTX 2050 + CPU encoder
subprocesses; corpus manifest `210052fe…`, 1,030 items; `gate_eligible: true`).
Started 2026-10-12 02:43 IST; drained after **9,970.7 s** (all 1,030
requests terminal); report after 13,943.3 s (drain + 2 × lease + the
recorded `orphan_grace_s` 3,900 s, then reconcile and sweep). Shortened
values, recorded as design §13.3 #7 allows: `attempt_timeout_s` 3,600
(production 14,400), `orphan_grace_s` 3,900 (production 86,400);
`attempt_lease_s` 600 = production. The memory-pressure watchdog froze nothing
during the run. Logs: `~/anti_piracy/datasets/p9/runs/soak-1000-run2/`.

| Criterion (design §13.3) | Result |
|---|---|
| 1 zero uncontrolled terminations | **met**: 2 processes (cuda, cpu), both exit 0 `normal`, reported |
| 2 zero lost requests; none non-terminal after drain + 2 × lease | **met**: 1,030 requests = 1,001 READY + 29 FAILED, 0 PENDING / IN_PROGRESS |
| 3 controlled exits reported, restarted, remaining work completed | met — **vacuously**: no controlled exit, 0 restarts |
| 4 single-item-OOM items retryable | met — **vacuously**: no single-item OOM occurred (batch B = 2) |
| 5 GPU/work leases released or expired | **met**: 0 GPU leases left, 0 IN_PROGRESS |
| 6 RSS/VRAM bounded, no upward trend | **met**: RSS peak 1,523 / 1,434 MiB (cuda/cpu; limit 4 GiB), Q2→Q4 median 1,308→1,320 / 1,340→1,350 MiB, no upward trend; VRAM peak 3,544 MiB (calibration), Q2 = Q4 median 510 MiB |
| 7 zero unreferenced objects, zero open intents after the sweep | **met**: 1,001 objects = 1,001 representation rows; sweep kept 1,001, deleted 0, aborted 0 uploads; 0 open intents; 1,030 events acknowledged by the `media-registry` consumer |
| 8 expected failures for the 30 corrupt/audio-only files, no crash | **not met**: 29 of 30 failed with an expected class (11 truncated → `decode_error`, 8 garbled → `unsupported_format`, 10 audio-only → `unsupported_format`); **`corrupt/c-04-h264.mp4` (truncated to ⅓) became READY** — ffmpeg reports only `Packet corrupt`, which design §3.2a defines as *not* truncation evidence (OF-5, decisions §5a). No crash |

**Harness result FAIL; G-8: FAIL** (criterion 8). The product behaved as
the approved design specifies for that input; the corpus labelled an input
the approved policy accepts as an expected failure. Resolving it needs an
operator decision (OF-5); the corpus and the criterion were not changed.
All 1,000 videos ended READY.

Observed during the run (no criterion failed by it): 4 typed storage errors
from the single-node dev Scylla under load — 3 `LwtUnknownError`
(WriteTimeout code 1100) and 1 `StorageUnavailableError` (ReadTimeout code
1200) during commit completion, after the commit point. They reach the
worker's generic handler (logged `attempt_crashed`, the attempt failed as
retryable `internal_error`); the next claim reused the committed
representation and every affected request ended READY with no re-encode,
orphan or open intent. The log label overstates these (they are typed
storage errors, not crashes) and each adds one counted retryable failure to
its request — an observation, not changed in P9.
*Run 2 remains a failed gate run (historical evidence).* After OF-5 the
corpus was corrected (§8.2) and the gate was rerun: §8.3–§8.4 (run 5 PASS).

### 7.10 Git state after the real-media validation (2026-10-12)

- crawler2: branch `main`, HEAD `2e397c1`, 29 ahead of `origin/main` (nothing
  pushed); the same modified/untracked paths as §5. Changed in this step
  (docs only): P9 `validation.md`, `traceability.md`, `exit.md`,
  `decisions.md` (§5a OF-4/OF-5), `design.md` (§22 N-15/N-16),
  `data-preparation.md`; `docs/architecture/fingerprinter-encoder.md` (§7),
  `docs/benchmarks.md`, `docs/v2-phase-plan.md`, `README.md`. No crawler2
  code, test or contract changed. `git diff --check` clean; relative links
  in the changed docs resolve.
- fingerprinter: branch `main`, HEAD `904af14`; all changes inside the untracked
  P9 paths: `fingerprinter/db.py`, `media.py`, `encoder.py` (X-8/X-9);
  `tests/v2/test_db_errors.py` (new), `test_truncation.py`,
  `test_encoder_failures.py`, `webserver.py` (streaming fixture);
  `benchmarks/p9-encoder/make_real_corpora.py` (new, written by an earlier
  interrupted session and used as is), `bench_inference.py` (G-5 eligible on
  C-PREC only), `bench_e2e.py` (gate label); new small result JSONs
  (`b-gpu-a-c-prec`, `b-prec-c-prec`, `b-prec-l-pairs`, `b-cpu-c-prec`,
  `b-gpu-b-c-prec`, `soak-1000`). No V1 file modified. Run leftovers
  (`.coverage`, `var/bench-*`) removed; logs and corpora stay in
  `~/anti_piracy/datasets/p9/`.
- Nothing staged, committed or pushed. crawler V1 not touched;
  `~/Videos/Blast.mp4` not used, copied or transmitted.

## 8. Operator decisions OF-4 and OF-5 — follow-up runs (2026-10-12)

Decisions: [decisions.md](decisions.md) §5b. Runs on the same host, stack and
runtime as §7; heavy jobs again as capped `systemd-run --user` units with the
memory-pressure watchdog (log `~/anti_piracy/datasets/p9/runs/watchdog-of5.log`).

### 8.1 OF-4 — G-3 rerun on L-PAIRS

`precision.py` now classifies each pair with `tp3_compare` (decisions §5b):
a decision difference or an alignment difference on a pair classified as a
match fails TP-3; an alignment difference on a pair both precisions classify
as a non-match is listed under `non_match_alignment_differences_diagnostic`.
Labelled data and `MatcherConfig` unchanged.

`env/bin/python benchmarks/p9-encoder/precision.py --clips ~/anti_piracy/datasets/p9/l-pairs --pairs ~/anti_piracy/datasets/p9/l-pairs/l-pairs.csv --out benchmarks/p9-encoder/results/b-prec-l-pairs-of4.json`
(16:22–16:52 IST; same L-PAIRS manifest `ab3d7711…` and `l-pairs.csv`
`ff43168c…`; the earlier report `b-prec-l-pairs.json` is kept as history).

| Check | Result |
|---|---|
| TP-1 | identical by construction; 187 items, 6,220 segments |
| TP-2 CUDA FP16 vs CPU FP32 | min **0.999275**, median 0.999995, 0 of 6,220 < 0.999 → G-2 on L-PAIRS **PASS** (same values as §7.4) |
| TP-3 decisions | identical on **480/480** pairs (37 matched in both precisions) |
| TP-3 alignment of matched pairs | identical on **37/37**; failing differences: **0** |
| diagnostic (non-match alignment) | 1: `lp-107-tears-of-steel-re-encode.webm` (label 1, not matched in either precision), FP32 400.0–415.0 s / 2 segments vs FP16 400.0–405.0 s / 1 segment — the §7.4 difference |
| informative | CPU FP32 oracle vs labels TP 37, FN 113, FP 0, TN 330 (unchanged) |

**G-3: PASS** under the OF-4 interpretation. **G-2 stays FAIL** (C-PREC,
§7.3), so the FP16 spec stays `gated`: not claimable, not default, its own
digest and `RepresentationId`s (TP-4/TP-5). G-3 passing does not change that.

### 8.2 OF-5 — SOAK-1000 corrupt-media set corrected

`make_real_corpora.py soak-1000` now runs every item labelled
`FAILED:…` through the production decoder (`fingerprinter.media` probe +
full decode, spec `dinov2-seg/2`, `explicit-indicators/v1`, on the host's
pinned ffmpeg 6.1.1-3ubuntu5) and refuses to write the manifest unless each
fails **non-retryably** with a labelled class; the outcome is recorded on
the item (`verified`). `make_real_corpora.py verify-soak` re-checks a built
manifest read-only.

1. Check of the run-2 corpus (manifest `210052fe…`) —
   `runs/verify-soak-run2-corpus.log`: 29/30 as labelled;
   `corrupt/c-04-h264.mp4` → **READY** (0 failure), reproducing the run-2
   outcome without the soak. Diagnosis (ffmpeg log of a plain decode): the
   ⅓ cut lands inside a sample; the mov demuxer returns it as a short packet
   (`corrupt input packet in stream 0`, `Invalid NAL unit size (46363 >
   10033)`) and never prints `stream 0, offset …: partial file`, the
   `mov_partial_file` indicator; `c-00` (same form) does print it and fails.
2. Evidence preserved in `runs/soak-1000-run2/corpus-as-run/`: the run-2
   manifest (`210052fe…`), its `progress.jsonl`, and `c-04-h264.mp4`
   (moved out of the corpus).
3. Replacement: `corrupt/c-04-h264-moovless.mp4` — the same source window
   and variant reel, but a plain (moov-at-end) MP4 truncated to ⅓ of its
   bytes (an interrupted download whose sample index is lost); sha256
   `e404b18e…`, 652,788 bytes. Decoder outcome: `unsupported_format`
   (`ffprobe: unrecognized container`), non-retryable. The item was not
   deleted, relabelled valid or exempted; the other 1,029 items are the
   identical bytes (resumable build, checksums re-recorded).
4. Rebuild (`runs/build-soak-1000-of5.log`, S-SRC checksums re-verified 7/7):
   **all 30 expected failures verified** — 11 truncated → `decode_error`
   (`truncated_input`: 2 `mov_partial_file`, 9
   `matroska_file_ended_prematurely`), 1 truncated moov-less MP4 + 8 garbled
   headers → `unsupported_format`, 10 audio-only → `unsupported_format` (no
   video stream). New manifest **`ed3bfd8d…`**.
5. Composition (data-preparation §2.4) re-checked on the new manifest:
   1,030 items = 1,000 videos + 20 corrupt + 10 audio-only; buckets 600 /
   300 / 90 / 10; 24 VFR; codecs H.264 259, AV1 251, VP9 246, HEVC 244;
   240p–1080p (179–213 each); 24/25/30/60 fps; MKV 468, MP4 313, WebM 219;
   136.8 video hours — unchanged from run 2.

### 8.3 SOAK-1000 runs 3 and 4 — aborted, no gate evidence

Both on corpus manifest `ed3bfd8d…`, same command as §7.9 (output
`~/anti_piracy/datasets/p9/runs/soak-1000-runN.json`), as a capped
`systemd-run --user` unit (`MemoryMax` 9 G) with the pressure watchdog.

- **Run 3** (2026-10-12 16:53 IST): stopped after ~6 min by a normal host
  shutdown (GNOME end-session dialog at 16:58:48, journal of boot −1); no
  crash, no OOM, watchdog idle. No report. Logs:
  `runs/soak-1000-run3-aborted-host-shutdown/`. From run 4 on, the soak runs
  under `systemd-inhibit --what=sleep:idle:shutdown` (the power-off dialog
  lists it).
- **Run 4** (2026-10-14 18:51–21:17 IST): the **harness** process died after
  2 h 26 min: a Scylla `ReadFailure` (code 1300, LOCAL_SERIAL on
  `encode_requests`), typed as `StorageUnavailableError`, escaped
  `Reconciler.intent_pass()` (`_resolve_rep_commit` →
  `_converge_requests`), which the harness calls every 30 s in place of the
  `reconcile run` service. State at the crash: 899 READY, 128 PENDING,
  3 IN_PROGRESS of 1,030. In the same minutes (21:13–21:17) the single-node
  dev Scylla failed several LWT reads/writes; the encoder workers handled
  theirs as designed (8 attempts failed retryable `internal_error` from
  typed `LwtUnknownError` / `StorageUnavailableError`, X-8 fix; encoders kept
  running). No report; not a criterion result. Logs:
  `runs/soak-1000-run4-aborted-x10/`.

**Defect X-10 (fixed):** the production `fingerprinter tools reconcile run`
loop (`cli.py`) had the same flaw — one pass raising a typed storage error
ended the service (exit 2, labelled `refused:`). Fix: in the endless loop a
`StorageUnavailableError` ends that pass only (logged
`reconcile_pass_storage_unavailable`; the next pass after `interval_s`
retries — passes are idempotent); `--once` still surfaces it (exit 2). The
soak harness does the same and records each failed pass
(`reconcile_storage_errors`). Tests (`tests/v2/test_cli.py`, real stack):
`test_reconcile_run_survives_a_storage_failure_in_one_pass`,
`test_reconcile_run_once_still_reports_a_storage_failure`; with the 3
existing CLI tests 5 passed.

### 8.4 G-8 — SOAK-1000 run 5 (the gate run on the corrected corpus)

`. scripts/p9-env.sh && env/bin/python benchmarks/p9-encoder/soak.py --corpus ~/anti_piracy/datasets/p9/soak-1000 --devices cuda,cpu --attempt-timeout-s 3600 --attempt-lease-s 600 --orphan-grace-s 3900 --max-hours 48 --out ~/anti_piracy/datasets/p9/runs/soak-1000-run5.json`
(fingerprinter, code with X-8/X-9/X-10; report copied to
`benchmarks/p9-encoder/results/soak-1000-run5.json`; run 2's
`soak-1000.json` kept as history). Capped unit `p9-soak1000r5`
(`MemoryMax` 9 G, `MemorySwapMax` 0) under `systemd-inhibit`, pressure
watchdog running: **no freeze/thaw during the run** (watchdog log empty).
Spec `dinov2-seg/2` FP32 `sha256:a4df3d19…` (unchanged), profile `rt-1`,
RTX 2050 + CPU encoder subprocesses. Corpus manifest **`ed3bfd8d…`**,
1,030 items, `gate_eligible: true`. Started 2026-10-14 21:24 IST; drained
after **9,076.8 s** (2 h 31 min); report after 13,022.7 s (drain + 2 × lease
+ recorded `orphan_grace_s` 3,900 s, then reconcile and sweep). Shortened
values as in §7.9 (design §13.3 #7).

| Criterion (design §13.3) | Result |
|---|---|
| 1 zero uncontrolled terminations | **met**: 2 processes (cuda, cpu), both exit 0 `normal`, reported |
| 2 zero lost requests; none non-terminal after drain + 2 × lease | **met**: 1,030 requests = **1,000 READY + 30 FAILED**, 0 PENDING / IN_PROGRESS |
| 3 controlled exits reported, restarted, remaining work completed | met — **vacuously, not exercised**: no controlled exit, 0 restarts |
| 4 single-item-OOM items retryable | met — **vacuously, not exercised**: no single-item OOM (batch B = 2) |
| 5 GPU/work leases released or expired | **met**: 0 leases left, 0 IN_PROGRESS |
| 6 RSS/VRAM bounded, no upward trend | **met**: RSS peak 1,422 / 1,424 MiB (cuda/cpu; limit 4 GiB), Q2→Q4 median 1,313→1,325 / 1,338→1,351 MiB, no upward trend; VRAM peak 3,544 MiB (calibration), Q2 = Q4 median 510 MiB |
| 7 zero unreferenced objects, zero open intents after the sweep | **met**: 1,000 objects = 1,000 representation rows; the sweep kept 1,000 and **deleted 3** orphaned objects (objects without a representation row; consistent with the attempts that failed after upload — not traced per object), aborted 0 uploads; 0 open intents; 0 unreferenced objects after the sweep |
| 8 expected failures for the 30 corrupt/audio-only files, no crash | **met**: 30 of 30 FAILED with a labelled class — 11 truncated → `decode_error` (`truncated_input`), 8 garbled headers + `c-04-h264-moovless.mp4` → `unsupported_format` (`ffprobe: unrecognized container`), 10 audio-only → `unsupported_format` (`no video stream`); 0 unexpected outcomes; all 1,000 videos READY |

**Harness result PASS; G-8: PASS** (8/8 criteria; 3 and 4 satisfied
only vacuously — recovery from controlled exits and single-item OOM is
evidenced by the tests G-4, TX-5…TX-8, not by this soak).

Observed during the run (no criterion failed by it): the dev Scylla again
returned typed storage errors under load — 13 encoder attempts ended
`attempt_crashed` (10 `LwtUnknownError` from WriteTimeout 1100 /
WriteFailure 1500, 3 `StorageUnavailableError` from WriteTimeout), each
retried; 5 READY requests carry ≥ 1 counted retryable failure. **2 reconcile
passes failed with a storage error and the loop continued (X-10 path
exercised)**. 1,031 events were acknowledged for 1,030 terminal requests:
one more event than requests, consistent with a duplicate publication allowed by the at-least-once contract (exit.md limitation 3); not traced further.

### 8.5 Regression runs after OF-4 / OF-5 / X-10 (2026-10-15)

| Command (cwd) | Result |
|---|---|
| `env/bin/python -m pytest -q -p no:cacheprovider tests/v2/test_corpus_verify.py tests/v2/test_truncation.py tests/v2/test_precision_tp3.py` (fingerprinter; targeted builder/decoder/TP-3 tests) | **28 passed** |
| `. scripts/p9-env.sh && env/bin/python -m pytest -q -p no:cacheprovider tests/v2/test_cli.py` (X-10, real stack) | **5 passed** |
| `. scripts/p9-env.sh && env/bin/python -m pytest -p no:cacheprovider tests/v2 -q --cov=fingerprinter` (fingerprinter; real stack + RTX 2050; capped unit) | **216 passed, 2 skipped, 0 failed** (23 m 13 s); skipped = the two opt-in real-scale tests (`RUN_P9_SLOW`, last run §7.5; not re-run — no storage code changed); coverage **89 %** of 4,241 statements |
| `scripts/test-fingerprinter-p9.sh -q` (crawler2) | **1 passed** (69 s) |
| crawler2 `ruff` (`--select E,F,W,B,UP,SIM,BLE --ignore E501`) on `cli.py`, `soak.py`, `precision.py`, `make_real_corpora.py` and the three changed/new test files | clean |

Not re-run (nothing they cover changed): crawler2 `make check` (774 passed,
157 skipped, §6.4), the fingerprinter V1 suite (445 passed, §6.4) and
crawler2's P8 real-stack tiers — no crawler2 code, contract or V1 file was
modified. Logs: `~/anti_piracy/datasets/p9/runs/tests-v2-full-after-of4-of5-x10.log`,
`cross-service-after-of4-of5-x10.log`.

### 8.6 Git state after the OF-4/OF-5 follow-up (2026-10-15)

- crawler2: branch `main`, HEAD `2e397c1`; the same modified/untracked paths
  as at the start. Changed in this step (docs only): P9 `validation.md`
  (header, §8), `decisions.md` (§5a intro, §5b), `design.md` (§13.3 G-3
  wording, §15.8 TP-3, §22 N-16 note), `exit.md`, `traceability.md`,
  `data-preparation.md`; `docs/v2-phase-plan.md` (one row) and `README.md`
  (P9 sentence). No crawler2 code, test or contract changed.
- fingerprinter: branch `main`, HEAD `904af14`; all changes inside the
  untracked P9 paths: `fingerprinter/cli.py` (X-10);
  `benchmarks/p9-encoder/make_real_corpora.py` (expected-failure
  verification, `verify-soak`, c-04 replacement), `precision.py` (OF-4
  `tp3_compare`), `soak.py` (X-10 handling, `reconcile_storage_errors`);
  tests `tests/v2/test_precision_tp3.py`, `test_corpus_verify.py` (new),
  `test_cli.py` (2 tests); results `b-prec-l-pairs-of4.json`,
  `soak-1000-run5.json` (new, small JSON). No V1 file modified.
- Datasets, corpora, logs and run directories stay in
  `~/anti_piracy/datasets/p9/` (outside Git). Nothing staged, committed or
  pushed. `~/Videos/Blast.mp4` not used, copied or transmitted.

### 8.7 Pre-commit checks after the operator's acceptance (2026-10-15)

The operator accepted P9 for the approved scope (decisions §5c). Before the
work was committed, only documentation changed in either repository:

- **crawler2:** status lines and §5c, plus a pointer in the runbook;
- **fingerprinter:** a new `docs/v2-encoder.md` and a README section.

No code changed after §8.5. Checks run on that tree:

| Command | Result |
|---|---|
| `. scripts/p9-env.sh && env/bin/python -m pytest -p no:cacheprovider tests/v2 -q --cov=fingerprinter` (fingerprinter, real stack + RTX 2050) | **216 passed, 2 skipped** (the opt-in `RUN_P9_SLOW` tests; last run §7.5: PASS), coverage **89 %** of 4,241 statements, 27 min 53 s |
| `scripts/test-fingerprinter-p9.sh -q` (crawler2 cross-service) | **1 passed** |
| `make check` (crawler2) | ruff clean, format clean, mypy clean (248 files), **774 passed, 157 skipped** |
| `env/bin/python -m pytest tests --ignore=tests/v2 -q` (fingerprinter V1 suite) | **445 passed** |
| crawler2 `ruff check --select E,F,W,B,UP,SIM,BLE --ignore E501` on fingerprinter `fingerprinter/`, `tests/v2/`, `benchmarks/p9-encoder/` | 8 style findings, no behaviour impact: the 7 known `encoder.py` findings of §2.1, plus SIM113 in `benchmarks/p9-encoder/common.py`, which §2.1 did not cover (it linted only the package) |

The fingerprinter repository has no linter or formatter configuration of its
own.

Not re-run:

- SOAK-1000, whose latest passing result is run 5 (§8.4);
- the opt-in 8 GiB and > 5 GiB tests (no storage or acquisition code changed);
- crawler2's P8 real-stack tier and two-host compose tier (exit.md §5).

After these checks the work was committed locally in several commits in both
repositories. Nothing was pushed.
