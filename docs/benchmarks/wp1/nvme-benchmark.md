# WP1 — Stage B: NVMe and filesystem baseline

## What was and was not possible

| Planned | Outcome |
|---|---|
| `fio` benchmark | **Not possible**: `fio` is not installed, `sudo` needs a password. |
| `smartctl` / `nvme smart-log` health (percentage used, spare, critical warnings, media errors, power-on hours, unsafe shutdowns, thermal-management times) | **Unavailable**: `/dev/nvme0` is `root:root 0600`, tools not installed. Only the hwmon temperature sensor and sysfs identity (model, firmware, PCIe link) were readable. **NVMe health is unverified.** |
| Substitute | `scripts/wp1_nvme_bench.py`: a bounded Python harness, **`O_DIRECT`** (page cache bypassed), one 4 GiB file in a dedicated directory `~/wp1-bench-tmp` on the root ext4 filesystem, threads = queue-depth slots. No block device is opened. |

**Limits of the substitute (read before using any number):** per-operation Python overhead (≈ 5–15 µs) is inside every latency and caps IOPS,
so IOPS are conservative lower bounds; thread count only approximates `iodepth`; the 32-thread cases saturate the CPU and
trip the harness' own 95 °C package limit within 1–4 s, so **QD32 and mixed numbers are short-burst values (≈ 200 k I/Os each),
not 20 s measurements**. These numbers are **not** comparable with `fio` output from other machines or with any HDD-era result whose method is unknown.

## Commands and parameters (exact)

```
python3 -I scripts/wp1_nvme_bench.py raw/stageB-nvme-bench.json --dir ~/wp1-bench-tmp --size-gib 4 --secs 20 --runs 3 --sustained-secs 120 --abort-flag raw/ABORT
python3 -I scripts/wp1_nvme_bench.py raw/stageB-nvme-sustained.json --dir ~/wp1-bench-tmp-sustained --size-gib 4 --sustained-secs 120 --only-sustained --abort-flag raw/ABORT
```

Cases (per run, 3 runs, 20 s target each, ≥ 3 s cooldown to ≤ 70 °C package before each): sequential write/read 1 MiB QD4;
random read 4 KiB QD1 and QD32; random write 4 KiB QD1 and QD32; mixed 70 % read / 30 % write 4 KiB QD16; then a 120 s sequential-write
sustained test and a 3,000-file create+`fsync` test (64 KiB each). Monitor (`scripts/wp1_monitor.py`, 2 s samples) ran for the whole time;
before/after snapshots (free space, NVMe/CPU temperature, memory, swap, throttle counters, GPU) are stored per case in the JSON.
The harness refuses to run if its directory already exists and removes **only** its own `wp1-bench-tmp*` directory (verified: `cleanup_dir_exists = false`).

## Results — run 2 attempt (3 runs; `raw/stageB-nvme-bench.json`)

`T` = case cut short by the harness' own CPU-temperature limit (value is a burst, not 20 s). Order of values = run 1 / run 2 / run 3.

| Case | Secs | Throughput MiB/s | IOPS | Avg latency ms | p99 ms | p99.9 ms |
|---|---|---|---|---|---|---|
| Seq write 1 MiB QD4 | 20 / 4 T / 20 | 1767 / 1806 / **1104** | 1767 / 1806 / 1104 | 2.26 / 2.21 / 3.62 | 5.39 / 5.23 / 18.05 | 10.3 / 10.3 / 68.1 |
| Seq read 1 MiB QD4 | 20 / 20 / 20 | 2213 / 2243 / **1657** | 2213 / 2243 / 1657 | 1.80 / 1.78 / 2.41 | 2.04 / 2.89 / 2.95 | 5.5 / 29.3 / 108.2 |
| Rand read 4 KiB QD1 | 20 / 5 T / 20 | 58.6 / 59.0 / 58.7 | 14,993 / 15,104 / 15,034 | 0.066 / 0.065 / 0.066 | 0.085 / 0.083 / 0.099 | 0.138 / 0.142 / 0.153 |
| Rand read 4 KiB QD32 | 1.0 T ×3 | 772 / 773 / 775 | 197.7 k / 198.0 k / 198.3 k | 0.160 / 0.159 / 0.159 | 0.49 / 0.48 / 0.50 | 0.77 / 0.70 / 0.75 |
| Rand write 4 KiB QD1 | 20 / 20 / 20 | 215 / 216 / 208 | 55,103 / 55,321 / 53,281 | 0.017 / 0.017 / 0.018 | 0.024 / 0.024 / 0.026 | 0.082 / 0.081 / 0.084 |
| Rand write 4 KiB QD32 | 2.0 T / 1.0 T / 4.1 T | 754 / 746 / 675 | 193.1 k / 191.0 k / 172.7 k | 0.164 / 0.165 / 0.183 | 0.84 / 0.86 / 0.90 | 2.24 / 2.26 / 2.00 |
| Mixed 70/30 4 KiB QD16 | 13.1 T / 11.0 T / 1.0 T | 648 / 644 / 681 | 166.0 k / 164.9 k / 174.4 k | R 0.119 / 0.120 / 0.114; W 0.036 / 0.036 / 0.033 | R 0.27 / 0.28 / 0.27; W 0.13 / 0.13 / 0.12 | R 1.11 / 1.14 / 1.06; W 0.22 / 0.30 / 0.19 |
| 3,000 small files, create + `fsync`, 64 KiB | — | — | 803 files/s | create p50 1.19 ms | p99 2.31 ms | — |

Variance: for the full-length QD1 cases the three runs agree within ≈ 4 % (read IOPS 14,993–15,104; write 53.3 k–55.3 k). The QD32 reads agree within 0.4 %.
The one real outlier is **run 3 of the sequential cases (write −39 %, read −26 % vs runs 1–2)**, see below.

Attempt 1 (`raw/stageB-attempt1-aborted-thermal-*`) was stopped by the original monitor rule "CPU package ≥ 95 °C ⇒ abort the whole run" during the 32-thread random-read case; its four completed cases
(seq write 1779, seq read 2569 MiB/s, rand read QD1 15,494 IOPS, QD32 179 k IOPS) are kept as evidence and are consistent with run 2. The abort rule was then changed to
"truncate the case, cool down, continue" (the hard abort remains for disk < 20 GiB, swap > 2 GiB, MemAvailable < 1.5 GiB). The change is visible in `scripts/` and is the reason for the `T` flags.

## Sustained sequential write (final run, `raw/stageB-nvme-sustained.json`)

Started after a 51 s cooldown to NVMe ≤ 50 °C (CPU package 57 °C). 101.9 GiB written in 120 s over the same 4 GiB file (overwrite; the drive sees the writes). MiB/s per 5 s window:

`1802 1791 1802 1715 1530 1450 1366 1138 1145 1220 1179 1036 745 284 286 253 300 261 274 245 243 258 273 275`

| Phase | Time | Throughput | Cumulative data |
|---|---|---|---|
| full speed | 0–20 s | ≈ 1.72–1.80 GB/s | ≈ 35 GiB |
| decay | 20–60 s | 1.4 → 1.0 GB/s | ≈ 35 → 75 GiB |
| collapse | ≥ 65 s | **≈ 245–300 MiB/s** (≈ 15 % of peak), stable to the end | ≈ 80–102 GiB |

NVMe temperature rose from ≤ 50 °C to ≈ 70 °C by t = 5 s and ≈ 78 °C by t = 15 s and stayed at 77.9–79.9 °C for the rest of the test; the monitor's 2 s samples peaked at **82.85 °C, i.e. at the hwmon `high` threshold (82.8 °C)**, crit is 84.8 °C.
CPU was idle (≈ 3 %, package ≈ 60 °C). **Two causes are plausible and cannot be separated with the data available**: exhaustion of the drive's write cache after ~35–75 GiB, and
firmware thermal throttling at ≈ 78 °C. The run-3 drop above started at NVMe 75–78 °C after ≈ 25 GiB of prior writes in the same minute, which fits either. A `nvme smart-log` (thermal-management times) and `fio` with controlled temperature would discriminate. An earlier sustained attempt
(`stageB-sustained-attempt1-truncated.json`, ended at ≈ 19 s by a watcher trigger that the monitor does not corroborate — CPU 60–66 °C in its samples; cause not established) showed the same step down at 1.75 → 1.05 GB/s when started from a hot drive (61 → 79 °C).

**Engineering meaning:** bursts and normal service I/O (the P2 workload peaked at 85 MB/s and 3.9 k write IOPS) are far inside the drive's capability. A workload that writes > ≈ 35 GiB continuously (large rebuilds, bulk ingest, backups) will fall to ≈ 250–300 MB/s on this drive and runs the drive at its temperature threshold.

## Per-test system state (summary; per-case before/after in the JSON, aggregates in `system-monitoring.md`)

- Free space: 59.86 GiB before, minimum 55.69 GiB during (file 4 GiB), 55.64 at end of the matrix; back to 59.8 GiB after cleanup. **Never below the 20 GiB floor.**
- MemAvailable ≥ 10.7 GiB, swap 0, PSI memory 0 throughout.
- Docker: all 5 containers healthy, 0 restarts; Scylla CPU peaked 46 % of a core only because of the idle stack.
- Errors: none from the harness.

## Checkpoint — Stage B: **PARTIALLY COMPLETE.**
Completed: throughput / IOPS / latency / mixed / small-file / sustained measurements, with monitoring and cleanup verified.
Incomplete: canonical `fio` numbers, NVMe SMART/health, a thermal-controlled separation of cache exhaustion vs throttling.
Needs approval to close: `sudo apt install fio smartmontools nvme-cli` (system change; not made), then `sudo smartctl -a /dev/nvme0`, `sudo nvme smart-log /dev/nvme0` and the fio profile in `README.md`.
