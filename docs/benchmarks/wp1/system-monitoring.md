# WP1 — Stage C: system monitoring and thermal behaviour

## Method

`scripts/wp1_monitor.py` (read-only, stdlib Python, no system change) wrote one JSONL sample every 2 s (Docker data every 10–15 s) for **every** workload; files are in `raw/*-monitor.jsonl.gz`.
Per sample: total and per-core CPU %, iowait, load, per-core frequency (min/avg/max), package and per-core temperatures (coretemp), CPU thermal-throttle counters
(`thermal_throttle/*_count`), MemTotal/Available/Used/Dirty, swap, PSI (memory, io), `oom_kill`, NVMe temperature (hwmon), NVMe MB/s, IOPS, await (ms, per direction), utilisation, free root space, GPU (utilisation, VRAM, temperature, power, SM clock, clock-event reasons),
per-container CPU / memory / block I/O / health / restart count / OOMKilled. `scripts/wp1_analyze.py` produces `raw/summary-monitoring.json` (reproducible from the `.gz` files).
Hard-abort flag: free disk < 20 GiB, swap > 2 GiB, MemAvailable < 1.5 GiB (never raised). CPU package ≥ 95 °C is advisory only (the CPU's own limit is 100 °C); it triggers per-case truncation in the I/O harness.

## Aggregates per workload window

| Window | Dur. | CPU avg / peak % | Pkg °C avg / **peak** | Time ≥ 90 / ≥ 95 / ≥ 100 °C | Avg freq MHz (lowest avg sample) | NVMe °C avg / **peak** | MemAvail min GiB | Swap peak MiB | Free root min GiB | Restarts / OOM |
|---|---|---|---|---|---|---|---|---|---|---|
| Idle baseline (stack running) | 28 s | 3.5 / 10 | 56.6 / 70 | 0 / 0 / 0 s | 2055 (1182) | 29 / 31 | 10.7 | 0 | 59.8 | 0 / 0 |
| B: I/O matrix (3 runs × 7 cases) | 318 s | 5.1 / 18 | 68.7 / **98** | 50 / 12 / 0 s | 2700 (1307) | 74 / **82.9** | 10.5 | 0 | 55.7 | 0 / 0 |
| B: sustained 120 s seq write | 119 s | 3.0 / 8 | 62.6 / 94 | 4 / 0 / 0 s | 2337 (1333) | 77.6 / **82.9** | 11.0 | 0 | 55.8 | 0 / 0 |
| D: P2 latency 1× (10 units/s/host) | 110 s | 7.7 / 65 | 66.5 / 96 | 16 / 6 / 0 s | 2470 (1187) | 49.5 / 58.9 | 10.0 | 0 | 59.7 | 0 / 0 |
| D: P2 latency 10× run 1 | 104 s | 31.6 / 82 | 86.0 / **100** | 70 / 30 / 2 s | 3175 (1400) | 61 / 68.9 | 8.4 | 0 | 58.6 | 0 / 0 |
| D: P2 latency 10× run 2 | 104 s | 31.6 / 72 | 85.4 / 97 | 68 / 36 / 0 s | 3152 (1182) | 65 / 70.9 | 8.4 | 142 | 58.0 | 0 / 0 |
| D: E-R1 1-copy run 1 | 1136 s | 5.4 / 33 | 64.5 / 98 | 162 / 76 / 0 s | 2322 (1148) | 43.7 / 64.9 | 8.5 | 167 | 57.5 | 0 / 0 |
| D: E-R1 **6-copy** (OOM-killed) | 877 s | 20.8 / 79 | **89.8** / **100** | **650 / 336 / 6 s** | 3060 (1646) | 69 / 79.9 | **3.9** | 89 | **53.3** | 0 / **1 (the test container)** |
| D: E-R1 1-copy run 2 | 1105 s | 5.9 / 39 | 66.2 / 99 | 160 / 72 / 0 s | 2471 (1164) | 48 / 74.9 | 6.4 | 89 | 57.5 | 0 / 0 |
| E: encoder B-GPU-a + env check | 312 s | 47.0 / 99.9 | 89.4 / **100** | 230 / 152 / 4 s | 2494 (1321) | 31 / 46.9 | 7.6 | 89 | 59.2 | 0 / 0 |

Disk metrics seen by the monitor: peak 2,749 MB/s read, 1,919 MB/s write, 184 k read IOPS, 171 k write IOPS, 97 % utilisation (all in the I/O harness windows); PSI io `some` peak 72 % there, ≤ 5.2 % in every database/Qdrant window
except the 6-copy Qdrant run (50 %; peak await 335 ms read / 638 ms write during its OOM thrash). Highest MemAvailable pressure: 3.9 GiB at the 6-copy run (11.8 GiB used) — swap never exceeded 167 MiB; PSI memory `some` peak 1.26 %.
**No host OOM events**; the one `oom_kill` counted is the E-R1 test container hitting its own 1.5 GiB cgroup limit.

## Throttling and thermal interpretation

- **CPU:** package temperature reaches 95–100 °C within seconds of a single busy thread (QD1 I/O test: 58 → 98 °C in 20 s) and sits at 85–90 °C average during multi-minute multi-core loads
  (10× P2 run, E-R1 6-copy, encoder benchmark: 336 s and 152 s at ≥ 95 °C; 2–6 s at the 100 °C limit). **This is sustained thermal limitation, not a spike**: the 6-copy window spent 650 s (74 %) ≥ 90 °C.
- The kernel's `package_throttle_count` / `core_throttle_count` increase **continuously, even at idle** (+2,112 in 28 s at idle ≈ 75/s) and by ≈ 450 k per 312 s in the encoder run (≈ 1,450/s). Their exact semantics (events vs. time-slices) are not documented by the kernel interface used,
  so they are reported as raw deltas (`raw/summary-monitoring.json`) and used only as a relative indicator: thermal/power limiting engages under load on this laptop.
- **Frequency:** average core frequency under load stayed 2.3–3.2 GHz (boost to 4.4 GHz seen in idle samples); the lowest per-sample average was 1.15–1.65 GHz. Performance did not degrade measurably in the steady-state workloads: the two 1-copy E-R1 runs' steady-state p95 differ by 0.6 % (rebuild p95 by 2.1 %), the two P2 10× runs' publication-lag p99 by 0.8 %,
  and the CPU/GPU encoder rates match the October measurements within 1.5 % / 0.1 % (`fingerprinter-baseline.md`; ambient temperature of the October runs is unknown). The measurable throughput impact of heat is limited to the **NVMe** (sequential write −39 %, read −26 % in run 3; sustained-write collapse) where heat and write-cache exhaustion cannot be separated.
- **NVMe:** reached 82.85 °C twice (= `high` 82.8 °C; crit 84.8 °C was never reached). Samples ≥ 75 °C occurred only in the write-heavy windows.
- **GPU:** idle 51–58 °C; during the encoder benchmark **peak 87 °C, 100 % utilisation, 54.9 W, VRAM peak 3,544 / 4,096 MiB** (calibration probing batch 128). Clock-event reasons seen: `0x4` (SW power cap), `0x24` (SW power cap + SW thermal slowdown, per the NVML bitmask), `0x400` (not decoded here), `0x1` (idle). So **the GPU was thermally/power limited during the benchmark** and still reproduced the October throughput (43.4 fps).
- Fans ran at 3,700 RPM at idle (sensor value from `sensors`); the idle package of 54–57 °C with five containers running leaves little thermal headroom for any workload using more than ≈ 2 cores.

Peak ≠ sustained: the I/O harness' short package spikes (up to 98 °C) are **harness CPU cost** (Python), not representative of database use; the 10× P2, Qdrant optimisation and encoder runs are the representative thermal loads.

## Services during all workloads

All five project containers: **health `healthy` in every sample; restart deltas 0; `OOMKilled=false`** (re-checked with `docker inspect` at the end of WP1). Scylla peaked at 1.40–1.44 GiB of its 2 GiB limit (CPU 114 % of 2 CPUs during 10×); MinIO 512 MiB of 512 MiB at the 10× run (**at its memory limit**, no OOM, no restart); Redis 142 MiB.
The idle crawler2-app health-check burns 60–80 % of a core every 30 s; it ran during all tests (the host was not otherwise quiesced: desktop session, Firefox, the Claude session and a separate system Redis were present).

## Checkpoint — Stage C: **COMPLETED** for the workloads run. Not covered: any workload longer than ≈ 19 minutes; GPU-plus-database concurrency (deliberately not run).
