# WP1 — Stage E: fingerprinter storage and GPU baseline

## Environment (the project's own, not the system Python)

- Interpreter: `fingerprinter/env/bin/python` (3.12.3). The system Python has no torch; that is irrelevant — **the project venv has torch `2.13.0+cu130`, CUDA runtime 13.0, `torch.cuda.is_available() == True`, device "NVIDIA GeForce RTX 2050" (sm_86), driver 615.71.09, 4,096 MiB.**
  No package was installed or upgraded. (Open observation: `importlib.metadata` says torch 2.14.1; see `hardware-inventory.md`.)
- Model: `facebook/dinov2-base` at the pinned revision, weights cached locally (346 MB `model.safetensors` via the HF cache symlink chain). Spec hashes in the benchmark JSON are unchanged from October (`sha256:a4df3d19…`).
- **Offline preserved**: every Stage E process ran with `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` on top of the project's own `local_files_only=True` + `hub_offline()` guard. `scripts/wp1_encoder_check.py` additionally patched `socket.connect` to record and refuse any non-loopback connection during model load and inference: **0 external connection attempts**. No protection was weakened or removed.

## 1. B-GPU-a (repo benchmark, CPU FP32 vs CUDA FP32 on identical tensors)

```
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 env/bin/python benchmarks/p9-encoder/bench_inference.py \
  --corpus ~/anti_piracy/datasets/p9/c-prec --frames 256 --reps 3 --out <wp1>/raw/stageE-b-gpu-a-nvme.json
```

| | This run (2026-11-17) | Historical `b-gpu-a-c-prec.json` (2026-10-11) |
|---|---|---|
| frames | **256**, from **2 sources** (`--frames 256`) | **504**, 8 per item from all 63 clips |
| CPU FP32 (12 threads, batch 8) | 6.49 frames/s median (6.42–6.52), 0.148 s/frame | 6.59 (6.52–6.60) |
| CUDA FP32 (batch 2, calibrated) | **43.45** frames/s (43.36–43.48), 0.0230 s/frame | 43.40 (43.35–43.46) |
| GPU / CPU | **6.7×** (threshold 5×) | 6.59× |
| calibration (`throughput/v1`) | selects **B = 2** (43.7 fps); B 1/2/4/8/16/32/64/128 = 35.4/43.8/40.7/42.0/27.2/27.6/26.5/17.2 fps; B 128 is the largest that fits; B 256 CUDA-OOM (ineligible, as documented) | identical selection |

The ratio and absolute rates reproduce the October values (CPU −1.5 %, CUDA +0.1 %), so **moving to the NVMe installation did not change inference throughput** (expected: the stage is compute-bound).
Caveat: the benchmark printed `gate_status: PASS`, but this run used a different (smaller) frame sample than the approved G-5 input, so it is a **comparable measurement, not a re-execution of the G-5 gate**; G-5 was not re-claimed.

## 2. Direct environment check (`scripts/wp1_encoder_check.py`, golden inputs, 8 × 3 × 224 × 224)

| | CPU FP32 (12 threads) | CUDA FP32 |
|---|---|---|
| `Embedder` init (weights from local cache) | 2.38 s | 0.20 s (second load in the same process, file cache warm and CUDA context already created by `is_available`; **not** a cold start) |
| first batch (2 frames, cold) | 0.43 s | 0.19 s |
| warm batch of 8, 5 passes | 1.17–1.54 s (median 6.66 frames/s) | 0.199–0.223 s (median 39.6 frames/s at batch 8; 43.4 at the calibrated batch 2) |
| output | `[8, 768]`, finite, L2 norm 0.99999994–1.0 | `[8, 768]`, finite, L2 norm 0.99999994–1.0 |
| process peak RSS | 1,198 MiB | 1,236 MiB (both models in one process) |
| VRAM | — | allocated 339 MiB, reserved 496 MiB, max allocated 428 MiB (batch ≤ 8); calibration (batch up to 128) peaked at 3,544 MiB of 4,096 per `nvidia-smi` |
| GPU during check | — | 75 °C, 99 % util, 50.8 W at the sample taken after the CUDA pass |

CPU-vs-CUDA agreement on the same inputs: **minimum cosine 0.99999994** (strict FP32). FP16 was not exercised (gated spec, outside WP1).
Not measured: CPU↔GPU transfer overhead as a separate number (inside the end-to-end latency only); cold-cache model load time from disk (page cache was warm).

## 3. GPU and thermal observations

During the benchmark the GPU reached **87 °C, 100 % utilisation, 54.9 W** with clock-event reasons including SW power cap and SW thermal slowdown (`0x24`); the CPU package spent 152 s ≥ 95 °C with 4 s at 100 °C (12 threads at ≈ 100 % for the CPU-FP32 phase).
Throughput still matched the October values, so on a ≈ 5-minute run the limiting did not reduce measured rate by more than the run-to-run spread. Longer GPU workloads were not run.

## Fingerprinter index-storage baseline

The index-storage behaviour (Qdrant on NVMe) is the E-R1 work in `database-baseline.md`. The fingerprinter's other durable stores are the shared Scylla keyspaces and MinIO buckets, whose size is in `storage-capacity.md` (`fingerprinter-targets` 1.7 GB, dev Qdrant volume 908 MB).

## Limitations

Single machine; synthetic-mini/C-PREC inputs only; no end-to-end real-media pipeline (P9 documented that a real 2 h film needs > 300 s of decode alone); no 13-day-finding reverification (that register phrase is not defined in the documents inspected, so it could not be mapped to a test); no GPU-plus-database concurrency.

## Checkpoint — Stage E: **COMPLETED** for environment compatibility, offline loading, CPU/GPU inference, VRAM, thermals. **Requires additional verification** for anything cross-host (H-05).
