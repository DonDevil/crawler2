# WP1 Run 2 — GPU qualification and resource efficiency (workstream E)

Date: 2026-11-18. Host RTX 2050 Laptop GPU (sm_86, 4 GiB), driver 615.71.09. Raw: `../raw/gpu/gpu-probe.json` (1 Hz GPU/CPU telemetry per level), `gpu-monitor.jsonl`. Script: `scripts/wp1r2_gpu_probe.py` (run from the fingerprinter repo with its own venv). Local single-host evidence only (E5).

## E1 — environment and the torch metadata discrepancy

| | Value |
|---|---|
| Runtime `torch.__version__` | **2.13.0+cu130**, CUDA 13.0, `cuda.is_available()` True, device "NVIDIA GeForce RTX 2050", capability 8.6 |
| `requirements-p9.lock` pin | `torch==2.13.0`, `torchvision==0.28.0` (the runtime **matches the lock**) |
| `importlib.metadata` | reports **two** torch distributions: `torch-2.13.0.dist-info` (2026-08-26, INSTALLER pip) and `torch-2.14.1.dist-info` (2026-10-24, INSTALLER uv) |
| Same pattern in | torchvision (0.28.0 and 0.29.1), triton (3.7.1 and 3.8.0), nvidia-cudnn-cu13 (9.20.0 and 9.24.0) (the other `nvidia_*` packages inspected had a single `dist-info`) |
| Which one owns the files? | sha256 of the installed `torch/version.py` equals the entry in **`torch-2.13.0.dist-info/RECORD`**, not 2.14.1's. The installed code is 2.13.0. |

**Established:** the environment contains duplicate `dist-info` directories for torch, torchvision, triton and cudnn; the *installed torch files* are 2.13.0 (hash match) and equal the lock pin; the 2.14.1 metadata is dated 2026-10-24 and was written by `uv` (the 2.13.0 metadata by pip). **Not established:** how it arose — the leading hypothesis is an interrupted or rolled-back `uv` upgrade that left its metadata behind (the 2.14.1 `torch/version.py` hash does not match the files on disk, so its files are not present). This is an **inconsistent virtual environment**, not a harmless display issue: `pip freeze` and `importlib.metadata.version("torch")` can report the wrong version, and tools that trust metadata (a lock check, `pip check`, a future `uv sync`) may behave unpredictably. Whether *other* packages' files were partly overwritten was not checked.
**Not changed.** Recommended (needs your approval because it rewrites the environment): recreate `fingerprinter/env` from `requirements-p9.lock` (or remove the stale `*-2.14.1`/newer duplicate `dist-info` directories after confirming each package's RECORD), then re-run `benchmarks/p9-encoder`. Until then, treat `torch 2.13.0+cu130` (runtime) as the authoritative version, as this run did.

## E2 — CPU/GPU numerical agreement (identical golden inputs, 8 × 3 × 224 × 224, Embedder fp32)

shape [8, 768], finite CPU True / GPU True, GPU L2 norms 0.99999994–1.0, **minimum CPU-vs-GPU cosine 0.99999994**, max absolute element difference 4.99e-07. Run 1 had min cosine 0.99999994 — identical.

## E2b — CPU vs GPU throughput (same real encoder, same golden inputs, 30 s sustained, run in the same process)

| | frames/s | batch | batch latency p50 / p95 ms | utilisation | process peak RSS | CPU pkg max |
|---|---|---|---|---|---|---|
| **CPU**, 12 threads, fp32 | **6.35** | 8 | 1,183 / 1,496 | 98.3 % mean of all CPUs | 1,983 MiB | 96 °C |
| **GPU**, calibrated batch 2 | **42.53** | 2 | 46.0 / 46.5 | — | (same process) | 97 °C |

**GPU is 6.7× the CPU path** (Run 1: 6.49 vs 43.45 → 6.7×; both sides now reproduced within 2.2 %). The CPU leg pins every core and the package at the 95–96 °C throttle region; it is not a sustainable background load on this host. (Audit note: the first pass reproduced only the GPU side; the CPU leg was added in the rerun and the GPU levels were re-measured after it, so the table below is the rerun.)

## E3 — batch-size / sustainability sweep (30 s sustained per level, real encoder, offline; rerun)

| batch | frames/s | batch latency p95 ms | GPU °C start→max (last-10 s mean) | power W | SM MHz first→last | fps 1st half / 2nd half | torch VRAM max allocated / reserved (MiB) | CPU pkg max °C | clock-throttle reasons seen |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 34.77 | 28.4 | 58→84 (83.0) | 52.4 | 1575→1567 | 34.82 / 34.73 | 350 / 408 | 86 | 0x4 |
| 2 | 42.53 | 46.5 | 63→87 (85.8) | 52.4 | 1575→1432 | 42.84 / 42.28 | 365 / 404 | 97 | 0x4 |
| 4 | 39.63 | 100.2 | 68→87 (86.0) | 52.4 | 1657→1590 | 39.44 / 39.80 | 383 / 446 | 95 | 0x4, **0x20** |
| 8 | 39.55 | 207.9 | 71→87 (86.6) | 52.0 | 1702→1597 | 38.82 / 40.16 | 427 / 496 | 95 | 0x4, 0x20, 0x404 |
| 16 | 26.01 | 617.2 | 73→84 (82.8) | 48.4 | 1702→1695 | 26.10 / 25.92 | 516 / 650 | 95 | 0x400 |

Throttle-reason bits (NVML `clocksThrottleReasons`): 0x4 = software power cap, **0x20 = software thermal slowdown**; 0x400 was reported but is **not decoded** here (no decoding claimed).

Reading:
* **Batch 2 remains the best operating point** (42.5 frames/s) and matches the project's own calibration (`throughput/v1`, Run 1: B=2). Batch 4 and 8 are 7 % slower; **batch 16 collapses to 26 frames/s** (−39 %) at 0.6 s latency.
* **The GPU does reach its thermal slowdown at larger batches:** 87 °C (the vendor target) with software thermal slowdown (0x20) active at batches 4 and 8; SM clocks drop 60–100 MHz within the level. Throughput inside a 30 s level did not decay measurably (first/second half within ≈ 3 %, batch 2: −1.3 %). In the first pass the maxima were 83–84 °C; the rerun starts each level from a GPU cooled only to ≤ 60 °C after a hot CPU leg, which is why it is warmer. **Both sets of temperatures are real; the rerun is the more conservative.** The hostguard GPU thresholds were set to WARN 87 °C / FAIL 95 °C accordingly (vendor target 87 °C, slowdown 97 °C).
* VRAM is not a constraint (≤ 0.65 GiB reserved at batch 16, 4 GiB card); process RSS ≈ 2.0 GiB (includes the CPU model used for the agreement check).
* The GPU is **not** the thermal problem: the CPU package sat at 86–97 °C during the GPU levels (CPU-side preprocessing + shared cooling), see the thermal report.
* Auto-selection: the repository already performs adaptive batch selection (`throughput/v1` calibration picks B=2 on this host and rejects B=256 for OOM). A thermal-aware variant was **not** implemented; batch 1 trades 18 % throughput for no useful temperature gain (83 vs 85.8 °C last-10 s mean). Sustainability beyond 30 s was not re-measured (Run 1 saw the GPU at 87 °C for ≈ 5 min at the same throughput).

## E4 — offline operation

All processes ran with `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, the project's own `local_files_only=True` + `hub_offline()` guard retained, and a socket-level guard that records and refuses any non-loopback `connect`: **`external_connect_attempts` = []** (zero) across weights loading (from the local HF cache, `Loading weights 223/223`), inference on CPU and CUDA, and all batch levels. No guard was removed and no network dependency was added.

## E5 — qualification boundary

Everything above is **local to this host**: one laptop, one driver/torch build, one model. It is local compatibility and sustainability evidence for H-05, not cross-host deployment qualification, contract-version verification across machines, or a network test. H-05 remains PARTIALLY CLOSED.
