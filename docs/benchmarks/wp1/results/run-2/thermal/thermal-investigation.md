# WP1 Run 2 — host thermal investigation (workstream C)

Date: 2026-11-18. Host: ASUS TUF Gaming F15 FX506HF (BIOS FX506HF.312), i5-11400H 6C/12T, RTX 2050 Laptop 4 GiB, Samsung NVMe, kernel 7.0.0-43. Raw data: `../raw/thermal/`, `../raw/gpu/`, `../raw/nvme/`. Scripts: `scripts/wp1r2_thermal_probe.py`, `wp1r2_hostguard.py`.
Nothing persistent was changed (no BIOS, fan, undervolt, power-profile or power-limit change).

## C1 — controlled CPU worker sweep

Workload: N single-threaded worker processes each looping a 768×768 float32 matrix multiplication (AVX-heavy numeric load similar to the encoder's CPU path), 60 s per level, 20 s idle baseline first, recovery measured after each level; 1 Hz telemetry. The probe aborts a level on package ≥ 99 °C for 3 consecutive polls (none did), NVMe ≥ 82 °C, or GPU ≥ 95 °C.

| workers | ops/s (768² fp32 matmul, 1 thread each) | pkg °C median / max | last-10 s median °C | mean freq MHz (last 10 s) | package throttle events during 60 s | idle baseline °C |
|---|---|---|---|---|---|---|
| 1 | 107.1 | 92 / 96 | 92 | 2350 | 5246 | 56 |
| 2 | 197.7 | 94 / 96 | 95 | 2402 | 11304 | 55 |
| 4 | 338.1 | 95 / 97 | 95 | 2555 | 10087 | 53 |
| 8 | 443.9 | 94 / 96 | 95 | 2523 | 13364 | 55 |
| 12 | 442.1 | 94 / 96 | 95 | 2484 | 18335 | 56 |

Fan speed during load 4,400–4,700 RPM (CPU and GPU fans; ≈ 3,300–3,400 RPM at 54 °C idle-ish); NVMe stayed ≤ 44 °C during these runs (no storage load); GPU ≤ 55 °C (idle).

Measured facts:
1. **One** single-threaded AVX worker already holds the package at a median of 92 °C (max 96 °C) with ≈ 5,200 package-throttle events in 60 s; two workers 94 °C; 4–12 workers 94–95 °C. **Temperature saturates at the ≈ 95 °C throttle point at any worker count ≥ 1.**
2. Throughput scales ≈ linearly to 4 workers (≈ 100 ops/s per worker), reaches **444 ops/s at 8 workers, and 12 workers (SMT) add nothing** (442). Four workers deliver 76 % of the 8-worker rate; one worker 24 %.
3. Mean frequency under load is ≈ 2.35–2.55 GHz (nominal turbo up to 4.5 GHz), i.e. the CPU is running at ≈ 55 % of its maximum frequency while pinned at the thermal limit.
4. Therefore **worker-concurrency reduction is not an effective thermal lever on this host**: it reduces throughput almost proportionally but not the temperature (up to 8 workers). The effective levers are frequency/power limits (below) and physical cooling.

## C2 — causes

| Hypothesis | Evidence | Status |
|---|---|---|
| **CPU power limits are far above the CPU's design power** | RAPL (root): package **PL1 (long-term) = 200 W, PL2 (short-term) = 83 W**, `max_power` = **45 W** (design); platform profile `powerprofilesctl` = `performance`, intel_pstate active, governor `powersave`, `no_turbo = 0`. Raw: `../raw/thermal/rapl-limits.txt` | **Confirmed (software-visible).** The firmware lets the CPU draw up to 83 W short / 200 W sustained, so it runs until the *thermal* limit stops it, not a power limit. |
| Cooling capacity inadequate (airflow, dust, thermal-interface material, heat-pipe/fan fault) | A single AVX worker reaches the throttle region (92–96 °C). Fans respond (3,300 → 4,700 RPM). Package temperature falls 30–40 °C within 2–3 s of load ending, but sustained load pins ≈ 95 °C at any count | **Plausible, not provable by software.** One busy core hitting ≈ 95 °C is unusual for a healthy laptop cooler, but it needs physical inspection (heatsink contact, dust, paste) to confirm. |
| Fan curve | Fans do ramp (3,300–4,700 RPM). Whether the curve is aggressive enough is unknown; changing fan curves is not allowed without approval | Open |
| Workload characteristic | AVX matrix math is a worst case; the Qdrant/Scylla/MinIO loads are lighter but the Run 1 and Run 2 E-R1/P2 runs still sat at 90–95 °C | Partly |
| Background processes | Idle CPU 52–57 °C with a desktop session, browser and the stack; 1-second boost excursions to 86–95 °C occur even at ≈ 3 % utilisation (0.1 s sampling: 10 consecutive samples at 86 °C, then 70 → 60 → 55 °C within 3 s). This is a very low thermal-mass response (a hot-spot sensor), not heat soak | Observed; explains why single-sample thresholds are the wrong safety rule |
| Throttling history | Kernel counters since boot: package throttle **6,311 events (23 s total)**, core throttle 68 events (before this run's heavy loads). During this run the package count rose by ≈ 5,000–18,000 per 60 s level | Measured |

Software can establish: the power-limit configuration, throttle counters, frequency behaviour, the fact that one core already reaches the throttle region. Physical inspection is needed to establish: heatsink/fan condition and thermal-interface material.
**Proposed (needs your approval, not applied):** an A/B test with a *runtime-only, reboot-reverting* package PL1 of 45 W (and/or `powerprofilesctl set balanced`) — expected effect: frequency drops, temperature falls well below 95 °C, sustained throughput falls less than proportionally. This is what would let sustained work run "automatically cooler" without a code change. It is a power-profile change, so it was not done.

## GPU thermals (from `../raw/gpu/gpu-probe.json`)

RTX 2050 Laptop: target temperature 87 °C, slowdown 97 °C, shutdown 100 °C (`nvidia-smi -q`). In the rerun (CPU leg first, so each GPU level started 58–73 °C) the GPU under the real encoder reached **84 / 87 / 87 / 87 / 84 °C at batch 1/2/4/8/16** (last-10 s mean 82.8–86.6 °C), power-capped at ≈ 52 W (reason 0x4), with **software thermal slowdown (0x20) active at batches 4 and 8** (SM clock −60 to −100 MHz) and the undecoded 0x400 at batches 8 and 16. Throughput did not decay measurably over 30 s. The CPU package stayed 86–97 °C throughout (shared heat-pipe/CPU work), so GPU runs do not relieve the CPU thermal problem. Details: `../gpu/gpu-qualification.md`.

## C3 — automatic workload protection (what was implemented and what was not)

Implemented (small, tested, standalone; no scheduler): `scripts/wp1r2_hostguard.py`
* `thermal` / `preflight`: CPU **sustained-heat** rule (FAIL: ≥ 95 °C for 3 consecutive 1 Hz polls or ≥ 100 °C once; WARN: a spike ≥ 95 or window median ≥ 90), GPU (warn 87 = vendor target, fail 95 = just under the 97 °C slowdown), NVMe (warn 75, fail 81 = controller TMT1).
* `storage --planned-gib N`: refuses an operation that would leave < 20 GiB free.
* `qdrant-build --target-points N`: refuses a rebuild whose predicted peak anon memory exceeds the 1.35 GiB rule (see Qdrant report).
* `containers`: OOM, restarts, health, memory-pressure.
Threshold classes are kept apart (safety / operational / performance gate) in `../automation/README.md`; no performance gate was touched.
How to integrate (not done): call `wp1r2_hostguard.py preflight --planned-gib N --out F` (exit code 0/1 proceed, 2 refuse, 3 treat as unknown) at the start of a benchmark or ingest launcher; poll `thermal` between work units and pause when FAIL.
Not implemented, deliberately: GPU batch auto-adaptation (the project's `throughput/v1` calibration already selects batch 2, the measured optimum), CPU worker-concurrency adaptation (shown above not to be a thermal lever), a host-wide scheduler, backpressure on Scylla/MinIO (no pressure signal was reproduced; see Docker report). Safety rule used inside the benchmark scripts: CPU sustained ≥ 95 °C for 3 polls (fio suite, sustained-write), ≥ 99 °C for 3 polls for the short worker-sweep probe (it must reach the throttle region to measure it; the hardware limit is TjMax 100 °C and run 1 already reached it), NVMe ≥ 82 °C, free < 28 GiB.

## C4 — recovery

* CPU package: back within 5 °C of the idle baseline **within ≈ 1 s** of load ending (all levels). Package temperature is therefore a poor heat-soak indicator; fans stay elevated for a while after load.
* NVMe after a sustained-write episode (peak 80.85 °C): ≤ 65 °C after 39 s, ≤ 60 °C after 4.4 min, ≤ 55 °C after 6.2 min, ≤ 50 °C after 16 min (`../raw/nvme/sustA-monitor.jsonl`). The NVMe is the slowest-recovering component; the fio suite waited for ≤ 55 °C before every job.
* GPU: the probe waited for ≤ 60 °C before each batch level; the cool-down duration was not logged, so no GPU recovery time is claimed.
No stress workload was left running; all probes terminated their workers.

## Limitations

One laptop, ambient temperature not recorded, no physical inspection, no power-limit A/B test (needs approval), loads ≤ 60 s (no multi-hour thermal behaviour; the host is **not** qualified for a 168-hour unattended soak).
