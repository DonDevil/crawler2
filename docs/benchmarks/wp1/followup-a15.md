# WP1 follow-up on the ASUS TUF A15 (2026-11-22): FP-05 payload trial, E-R1 rebenchmark, NVMe/HDD, GPU, capacity

Not a soak and not a replacement for Run 3 ([run-3](results/run-3/final_report.md) = software-suite stability only; it did **not**
qualify the NVMe, GPU, Qdrant, Scylla or MinIO). Register: [blockers.md](blockers.md). Decisions: [ADR-028](../../adr/ADR-028-staged-deployment-and-tiered-retention.md).

**Identity.** Host `Dhanush-Ubuntu` (A15), Ubuntu 24.04.5, kernel 7.0.0-43-generic, Docker 29.x. crawler2 base `b6f8c3b`,
fingerprinter base `3018d7b` plus the uncommitted/committed changes listed in the final commit. Python 3.12.3, torch 2.13.0+cu130,
transformers 5.15.1, numpy 2.5.2. Sessions: 2026-11-22 (UTC 09:22 → ~).

## 1. Storage inventory (non-destructive: `lsblk`, `findmnt`, `df`, sysfs)

| Device (physical) | Partition | FS | Mount | Size / free | Notes |
|---|---|---|---|---|---|
| **`nvme0n1` WD PC SN740 SDDPNQD-512G-1102**, fw 73113000, PCIe 16 GT/s ×4 (0000:04:00.0), 476.9 GiB | `p7` | ext4 | `/` (also bind-mounted at `/home/danush/data`) | 137 G / 28 G free (80 %) | OS, Docker (`/var/lib/docker`, 893 MB of volumes), projects |
| same physical drive | **`p9`** | ext4 | **`/mnt/data`** | 138 G / 131 G free (1 %) | the "data" volume: **same physical NVMe as `/`, not an independent device**. Empty except `40mins-clips`. Used for all benchmark files below. |
| same physical drive | p1 vfat `/boot/efi`; p3, p4, p5 ntfs (156 G, 1.1 G, 28 G, unmounted); p6 vfat; p8 swap 10.8 G | | | | untouched |
| **`sda` Toshiba MK3276GSX**, 298.1 GiB, rotational=1, **USB-attached** (xHCI, `usb4/4-1`) | `sda1` | ext4 | `/media/dhanush/Media` | 292 G / 265 G free (5 %) | bulk tier candidate; holds 13 G of personal media (`Movies-Downloaded`, not touched); mounted `errors=remount-ro` |

So the A15 has **two physical devices**: one NVMe (three-way split: `/`, `/mnt/data`, swap) and one USB HDD. Docker volumes are on
`/` (NVMe p7). Project sizes now: Scylla dev volume 561.8 MB, Qdrant dev volume 319.5 MB, Redis 12 MB, MinIO 0.25 MB.

## 2. A15 NVMe — health and fio (H-02: **measured, not qualified**)

**SMART/NVMe health: NOT READ.** `smartctl -a /dev/nvme0n1` → `Permission denied` (device is `root:disk`; user is not in `disk`);
`sudo` requires a password; `nvme-cli` is not installed. Sysfs facts that are readable without root: model/firmware above,
PCIe link 16 GT/s ×4 (= max), state `live`, composite temperature (hwmon3) 33–43 °C at idle. Kernel journal (`journalctl -k`, boot
scope): **0** I/O errors, **0** EXT4 errors, **0** NVMe resets before and after the fio run. Run 3's two correctable AER `RxErr` events
were on `0000:04:00.0` (the NVMe, A-7); this boot's 7 AER lines are on `0000:01:00.0` (the **GPU**), unrelated to the NVMe and unchanged by the fio run.
Not claimed: SSD failure, SSD health.

**Tool.** `crawler2/benchmarks/p2-storage/fio_qualify.py` (wp1-fio/1), fio 3.36, `ioengine=io_uring` (libaio cannot initialise: `fs.aio-nr`
= `fs.aio-max-nr` = 65536, held by the Scylla container — environmental finding), `direct=1`, file-based only (never a device), free space
floor 20 GiB enforced (refuses otherwise), every job bounded, test dir removed afterwards. Raw results:
`crawler2/benchmarks/p2-storage/results/wp1-fio-a15-nvme-mnt-data-p9.json` (exact command lines inside). Target: `/mnt/data` (p9, ext4,
`rw,nosuid,nodev,relatime`), 130.2 GiB free before and after, 4 GiB test file; 3 repeats × 20 s (+2 s ramp).

| Job | Block / QD×jobs | MiB/s (3 repeats) | IOPS | latency µs p50 / p99 / p99.9 (last repeat) |
|---|---|---|---|---|
| seq read | 1M / 8×1 | 4849, 1509, 1511 | 4848, 1508, 1510 | 5210 / 5931 / 9110 |
| seq write | 1M / 8×1 | 3713, 3141, 3167 | 3712, 3140, 3166 | 1909 / 8585 / 10682 |
| rand read | 4k / 32×4 | 1774, 1773, 1783 | 454k, 454k, 456k | 243 / 799 / 1237 |
| rand write | 4k / 32×4 | 1975, 1764, 1732 | 506k, 452k, 443k | 200 / 1810 / 3031 |
| QD1 read | 4k / 1×1 | 73.2, 73.4, 73.7 | 18.8k | 49 / 61 / 100 |
| QD1 write | 4k / 1×1 | 271, 269, 271 | 69k | 10 / 16 / 35 |
| sustained seq write | 1M / 8×1, **100 GiB** | median 3177 over 49 s; last quartile 612 | 2068 mean | 1925 / 16056 / 21365 |
| sustained seq read | 1M / 8×1, 32 GiB | 4670 mean (7 s) | 4670 | 1630 / 1810 / 7373 |

Observations (measured): (a) sequential read fell from 4849 to ~1510 MiB/s in repeats 2–3 (right after write jobs) — **cause unknown**,
not investigated; the 32 GiB sustained read later ran at 4670 MiB/s. (b) **Sustained-write drop:** 1-s bandwidth log: ~3.2 GiB/s for 28 s
(≈ 89 GiB written), then ~0.6 GiB/s (571–722 MiB/s) for the remaining ~11 GiB. During the run, hwmon `temp1` (composite) peaked
71.9 °C, `temp2` 94.85 °C (rose from 57.9), `temp3` 67.9 °C. The drop coincides with a hot period **but the evidence does not distinguish
SLC-cache exhaustion from thermal limiting** (no SMART, no throttle counters) — neither is claimed. (c) No fio error (`error=0` on every job).
Total written by this run ≈ 120 GiB. (d) F15 comparison: the F15 qualification record is **not in this repository** (Run 2 evidence "not
present", [comparison-with-run-2.md](results/run-3/comparison-with-run-2.md)); nothing is transferred or compared here.

**H-02 outcome for the A15: PENDING.** The repository defines no testable H-02 pass criterion and SMART health could not be read; so the
A15 is neither passed nor failed. The measurements above are reproducible with the script.

## 3. USB HDD and tier measurements (H-08 evidence)

Same script, smaller bounds for a 80 MB/s device: 2 GiB file, 15 s jobs, 2 repeats, sustained write 10 GiB (121 s), sustained read 4 GiB.
`results/wp1-fio-a15-usb-hdd-media.json`. 264.9 GiB free before/after; 0 kernel I/O messages; **no temperature** (needs root SMART).

| Job | MiB/s | IOPS | latency p50 / p99 (ms) |
|---|---|---|---|
| seq read 1M QD8 | 84.7 | 84 | 81 / 135 |
| seq write 1M QD8 | 84.2 | 83 | 93 / 152 |
| rand read 4k QD32×4 | 0.9 | 223–229 | 489 / 1216 |
| rand write 4k QD32×4 | 0.9 | 209–218 | 514 / 1317 |
| QD1 read 4k | 0.4 | 100 | 9.9 / 15.8 |
| QD1 write 4k | 0.9 | 213–220 | 3.2 / 7.2 |
| sustained seq write (10 GiB) | 83.9; no drop (min 70 MiB/s once) | | |
| sustained seq read (4 GiB) | 83.8 | | |

SSD vs HDD (different file sizes and durations, same job shapes): sequential ≈ 36–58× faster, random 4k at QD32 ≈ 2000× faster on the
NVMe. These are **file-based, direct-I/O, USB-attached** numbers; they do not represent an internal SATA HDD, and 84 MiB/s may be a
property of the drive, the USB bridge or the port (not separated). HDD qualification under a real workload is pending.

### Preliminary capacity model (H-08) — `crawler2/benchmarks/p2-storage/capacity_model.py`

`required = unit_bytes × units_per_day × retention_days × safety_factor` (index: `points × bytes_per_point × safety_factor`). Output:
`results/wp1-capacity-model.json`. Measured unit sizes: **Qdrant ≈ 6,018 B/point** (628,502,393 B / 104,443 points, F15 run, one generation;
a rebuild holds two generations). Everything else is **not measured** (Scylla per-record, MinIO per-object, evidence, snapshots, logs) and
**all workload inputs are undefined**, so every category reports `UNDEFINED`. Tier assignment per ADR-028: operational data and active
Qdrant on SSD/NVMe, historical snapshots and bulk evidence on HDD, long-retention evidence separately retained. **No production capacity is
stated; H-08 stays open.**

## 4. A15 GPU qualification (H-05)

Existing P9 encoder benchmarks, unchanged methodology; device NVIDIA RTX 3050 Laptop 4096 MiB (3761 MiB usable for the harness), driver
595.99.02, torch 2.13.0+cu130 (`torch.cuda.is_available()` True), spec `sha256:a4df3d19…` (rt-1). Results in
`fingerprinter/benchmarks/p9-encoder/results/b-cal-rtx3050-a15.json`, `b-gpu-a-c-prec-rtx3050-a15.json`, `b-prec-c-prec-rtx3050-a15.json`.

* **Batch behaviour (B-CAL, 2 runs):** FP32 frames/s by batch 1/2/4/8/16/32/64/128 = 46/53/58/60/60/62/62/60; largest fitting batch 128;
  selected batch **8** (smallest within 5 % of the best, B=32) in both runs; repeatable.
* **G-5 (GPU FP32 ≥ 5× CPU FP32, C-PREC, 256 frames, 3 reps):** CPU (16 threads, batch 8) **6.45 frames/s**, CUDA FP32 **58.2 frames/s**
  → **9.02× — PASS**. Per-frame latency in batch: CPU 152 ms median, GPU 17.2 ms. GPU memory allocated 449 MB (reserved 520 MB).
  CUDA FP16 (a separately gated spec) 181.9 frames/s (28×) — not accepted, see next line.
* **Numerical equivalence (B-PREC, 63 C-PREC clips, 10,267 segments, threshold cosine ≥ 0.999):** CUDA FP32 vs CPU FP32 **min 0.9999998, 0 below
  threshold** (median 1.0). TP-1 alignment identical. CUDA FP16 vs CPU FP32: min 0.9906, 7 segments below → the report's **G-2 = FAIL for FP16**
  (the FP16 spec stays gated off; unchanged policy). G-3 (labelled decisions on L-PAIRS) was **not run**.
* **Resources during the benchmark/precision runs (`nvidia-smi` every 2 s, 1,644 samples; desktop session also on this GPU):** utilisation
  max 100 %, memory max 3,450 MiB (incl. ~300 MiB desktop), temperature max 84 °C, power max 60 W, throttle-reason bitmask seen: 0x1 (idle),
  0x4 (software power cap), 0x0. No CUDA errors, no CPU fallback occurred (device is chosen explicitly).
* **Not done:** a 4 GiB-boundary batch sweep beyond 128 (B_max is found by the calibration itself), end-to-end encode (B-GPU-b), soak.

**H-05: partially closed → remains partially closed.** Policy accepted (flexible host); A15 evidence for G-5 and FP32 equivalence is
positive. Remaining: owner acceptance of the A15 as encoder host, G-3, and cross-host compatibility if another GPU host is used.

## 5. FP-05 and the E-R1 rebenchmark

**Change** ([p10-matcher.md §5.1](../../../../fingerprinter/docs/p10-matcher.md)): `SearchRequest.payload_fields`; candidate search returns
`rep,cid,trep,tid,tver,seq,k,spec,lowinfo` (9 of the 11 stored keys; `t`, `lowinfo_rev` dropped); lowinfo witness search returns
`tid,trep,tver,k`. Stored payload, thresholds, `index.max_points`, collection config unchanged. Unit tests: `tests/p10/test_fp05_payload_fields.py`
(3); full fingerprinter suite after the change: **583 passed, 239 skipped** (skips = live-stack tests, see §7).

**Method.** `fingerprinter/benchmarks/p10-index/capacity_e_r1.py`, unchanged gate and phases; one new optional flag `--payload-fields`
(default = original behaviour) applied to every query (warm and steady/rebuild query threads). Same Qdrant 1.15.1 digest, container
`--memory 1536m --memory-swap 1536m --cpus 2` (the gate configuration), fresh data directory per run, gRPC batches of 1,000, 10 queries/s
concurrency 2 limit 100 rescoring, steady phase 300 s, exact counts at boundaries. **Deviations from the F15 runs:** (1) host is the A15
(different CPU/NVMe); (2) data directory is a bind mount on `/mnt/data` (ext4 p9) instead of a Docker volume on `/` (28 GiB free); (3)
runs used the same venv, run alone except idle crawler2 containers; (4) no SMART/temperature. The protocol is gRPC in the harness, REST
(`vindex.py`) in production — **not changed; a separate methodology issue** (REST+JSON payload serialisation cost is not measured by E-R1).
Phase 2 "warm" has 0 samples after the 60 s warm-up (760 queries at ≥1 per ~6 ms finish in < 60 s) — as in the F15 1× run, so the
10 ms rule is evaluated only in phases 3 and 5. Raw: `fingerprinter/benchmarks/p10-index/results/e-r1-a15-*.json` (+ `datasets/p10/runs/e-r1/a15-*.log`,
untracked samples). `e-r1-a15-1x-whole-r1` was the first run (logs `a15-1x-whole.*`).

| Run (query payload) | N points | rebuild p50/p95/p99 ms | steady p50/p95/p99 ms | anon peak GiB | OOM | errors | live-write p95 ms |
|---|---|---|---|---|---|---|---|
| 1× whole r1 | 104,443 | 9.95 / 25.5 / 39.3 | 8.86 / **11.03** / 11.6 | 0.434 | 0 | 0 | 29.8 |
| 1× whole r2 | 104,443 | 9.96 / 24.1 / 47.1 | 8.89 / **10.99** / 11.6 | 0.440 | 0 | 0 | 31.3 |
| 1× fields r1 | 104,443 | 9.20 / 21.6 / 49.2 | 8.13 / **10.14** / 10.7 | 0.422 | 0 | 0 | 30.7 |
| 1× fields r2 | 104,443 | 9.14 / 20.8 / 39.5 | 8.17 / **10.17** / 10.7 | 0.440 | 0 | 0 | 28.8 |
| 2× whole | 208,886 | 12.1 / 58.3 / 91.1 | 10.8 / **16.9** / 20.9 | 0.658 | 0 | 0 | 36.5 |
| 2× fields | 208,886 | 10.2 / 59.2 / 97.3 | 9.13 / **14.6** / 19.0 | 0.667 | 0 | 0 | 41.0 |
| 3× whole | 313,329 | 26.8 / 90.3 / 123 | 10.8 / **26.6** / 34.5 | 0.893 | 0 | 0 | 46.5 |
| 3× fields | 313,329 | 24.6 / 91.0 / 119 | 9.98 / **18.9** / 24.5 | 0.920 | 0 | 0 | 44.6 |
| 6× fields (only run; see below) | 626,658 | rebuild 201 / 312 / 382 (n=2,125) — **run aborted** | none (phase 5 not reached) | **1.431** (> 1.35) | cgroup lost | 247 | 196 |

6×: only the reduced-payload variant was run (1×–3× already fail in both modes and latency worsens with scale; a 6× whole-payload run was started
and stopped, no result kept). Phase 1 ingest 287 s, exact counts equal at boundaries 1–2; warm phase p50/p95 36.2/50.6 ms (n=506);
in phase 3 the Qdrant container's cgroup disappeared (`cgroup gone` in the per-second samples, gRPC `UNAVAILABLE: Stream removed (Socket closed)`,
247 query errors) with anon peak 1.431 GiB against the 1.5 GiB container cap, **before R_req was reached**. The container was removed by the harness
afterwards, so whether the kernel OOM-killed it is **unconfirmed** (the oom_kill counter vanished with the cgroup). Gate result: **FAIL**
(`anon_peak_le_1_35_gib`, `completed`, `phase3_reached_R_req`, p95 rules all false).

Gate rule `query p95 ≤ 10 ms in phases 3 and 5` — **FAILS in every run, whole and reduced payload.** All other rules pass at 1–3× (no OOM,
anon peak ≤ 1.35 GiB, exact counts, R_req reached). Compared with the F15 (Run 2 era) baseline at 1× — rebuild p95 5,111.6 ms / steady
p95 154.3 ms (cgroup-pressured, query timeouts) — the A15 is two orders of magnitude better *with or without* the change, which is a
**hardware/storage difference, not an effect of FP-05** (F15 and A15 cannot be compared for the payload effect).

**Effect of the change (A15, paired, same host):** steady p95 −8 % at 1× (11.01 → 10.15 ms, repeatable within 0.05 ms between reps),
−14 % at 2×, −29 % at 3×; steady p50 −8…−7 %; rebuild p95 −14 % at 1× but **unchanged at 2× and 3×** (the rebuild phase is dominated by
the concurrent build/ingest, not payload). Measured conclusion: **payload retrieval is a real but not the only/dominant contributor** —
removing 2 of 11 keys cannot show the "no payload" speedup seen in §13.20 (p95 2.5 ms vs 6.0 ms at 1×, payload fully off); the
remaining latency at scale comes from the rebuild contention and the index size.

**Is payload retrieval still the dominant contributor? (A15 `e_r1_diag.py`, 1×, 104,443 points, one client, no rebuild, 1.5 GiB cap, 300 queries,
`results/e-r1-a15-diag.json`):** whole payload p50/p95 = 5.83/6.46 ms (rescore off), 5.89/6.42 ms (rescore on); **no payload** 2.31/2.72 ms and
2.44/2.84 ms. So with an idle single client the *presence* of payload still accounts for ≈ 60 % of query time (≈ 3.5 ms), and rescoring is
negligible. Trimming 2 of 11 keys recovered only ≈ 0.7 ms (p50 under load), i.e. the cost tracks the **number of payload reads (limit 100 hits)**,
not the bytes per payload. That finding suggests the next lever is fewer/cheaper payload reads (e.g. payload not fetched for hits below the
floor, payload in RAM, or a smaller `limit`) — **each is a design change that needs a new owner decision; none was attempted.** Whether a smaller payload still helps (e.g. fewer keys,
or payload kept off the hot path) is **not tested** and would be a new decision.

**FP-05 verdict: still OPEN — the E-R1 gate is not met at 1×, 2× or 3× (and F15 6× failed before).** Thresholds, `index.max_points`
(250,000) and gate untouched. The steady-state phase at 1× is within ~1.5 % of the gate (10.14–10.17 ms vs 10 ms) but the gate is not met
and the rebuild phase fails clearly. No broader redesign was done.

## 6. Test, environment and incident notes

* **Fingerprinter environment.** `env/` (Python 3.12.3) was compared with `requirements-p9.lock` (`uv pip freeze` vs lock): all 75 pinned
  packages identical (only name-normalisation differences); `antipiracy-contracts` from `crawler2/contracts`. It was **not recreated**: a clean
  rebuild needs ~GBs of CUDA wheels that are absent from the uv cache (offline install failed on `nvidia-cublas==13.1.1.3`) for an identical result.
  Reproduce: `uv venv --python 3.12 env && uv pip install --no-deps -r requirements-p9.lock` (torch/torchvision from the cu130 index; contracts
  from `crawler2/contracts`), then `uv pip install pytest` for tests.
* **Redis safety.** The default test Redis in `fingerprinter/tests/` is now `redis://127.0.0.1:16380/15` (disposable container,
  `docker run -d --rm --name fp-test-redis -p 127.0.0.1:16380:6379 redis:7.4.2-alpine`); `tests/conftest.py` and `tests/test_target.py` abort if
  the URL is port 6379 or 16379. Every test in this session used the 16380 container; **system Redis 6379 was not contacted**. Run 3 incident
  A-10 stays recorded in [run-3 anomalies](results/run-3/anomalies.md) (DB 15 contents/effect unknown).
* **Not run / not available:** SMART (root); F15 comparison (record absent); G-3 on L-PAIRS; 168 h soak; Scylla/MinIO workload qualification;
  multi-host tests; HDD internal-SATA measurement (only a USB HDD exists).

## 7. Test and check results (this session)

| Check | Result |
|---|---|
| crawler2 `pytest` (default tier, no live stack) | **1151 passed, 165 skipped** (same as Run 3 clean run); skips = need `RUN_INTEGRATION_TESTS=1` compose stack |
| crawler2 `ruff check .` / `ruff format --check .` / `mypy` (strict, 333 files incl. the two new `p2-storage` scripts) | pass / pass (526 files) / pass |
| fingerprinter `pytest tests` without the stack | **583 passed, 239 skipped** (after the FP-05 change; throwaway Redis :16380) |
| fingerprinter `pytest tests` **with the live P9/P10 stack** (`scripts/p10-env.sh`; Scylla/MinIO dev stack, dev Qdrant :16333, **Redis redirected to the throwaway :16380**) | **820 passed, 2 skipped, 0 failed** (33 min) — includes all P10 integration tests through the new `with_payload` field lists. A first attempt (wrapper bug: an extra `FINGERPRINTER_TEST_REDIS_URL` export tripped the settings validator) errored 237 tests at configuration time and was discarded |
| FP-05 unit tests | `tests/p10/test_fp05_payload_fields.py` — 3 passed |
| fingerprinter ruff/mypy | **not applicable as a gate**: the fingerprinter has no ruff/mypy configuration and does not use them (mypy strict on the touched files reports 40 pre-existing errors across 12 files; none introduced by the change was analysed individually) |
| Documentation links | spot-checked manually: ADR-028, ADR index, blockers.md, followup-a15.md, p10-matcher §5.1 |

Not run: crawler2 live-stack tier (`RUN_INTEGRATION_TESTS=1`), 7-day soak, 6× whole-payload E-R1, L-PAIRS G-3, F15-vs-A15 NVMe comparison, SMART.

