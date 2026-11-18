# WP1 Run 2 — NVMe qualification (workstream B)

Date: 2026-11-18 (host: ASUS TUF Gaming F15 FX506HF, Ubuntu 24.04.5, kernel 7.0.0-43-generic, fio 3.36, nvme-cli / smartmontools 7.4).
Device: Samsung MZVLQ512HBLU-00B00 (PM991a-class, DRAM-less, Host Memory Buffer), firmware FXM7501Q, NVMe 1.4, PCIe Gen3 x4 (8 GT/s, x4: LnkSta == LnkCap), ext4 root `nvme0n1p7` (`rw,relatime,stripe=32`), scheduler `none`.
Serial number: redacted in the committed raw files (`<serial-redacted>`).
**This is a benchmark and diagnostic result, not a device qualification certificate** (see "What this does not establish").

## B1 — SMART / health (raw: `../raw/nvme/smart-before/` 08:01, `smart-rerun-boot-17-04/`, `smart-final-20-09/`; per-run deltas `smart-{before,after}-sust{A,B,C}.json`)

| Field | Value | Status |
|---|---|---|
| SMART overall (smartctl) | PASSED | read OK (root) |
| Critical warning | 0 | read, real zero |
| Available spare / threshold | 100 % / 10 % | read |
| Percentage used | 8 % at 08:01 → **9 % at 20:09** | read; ticked during this run |
| Data units read / written | 96.36 M / 54.24 M units at 08:01 → 98.18 M / 56.38 M at 20:09 | read; **≈ 1.10 TB (≈ 1,023 GiB) written and ≈ 0.93 TB read by this run's tests**, including the first-pass work that was repeated |
| Host read / write commands | 1.234 G / 0.891 G → 1.363 G / 0.991 G (final) | read |
| Controller busy time | 3,140 min | read |
| Power-on hours / power cycles | 5,755 h / 2,244 → 5,759 h / 2,246 (two clean reboots while the operator was away) | read |
| **Unsafe shutdowns** | **242** (10.8 % of power cycles) | read; not explained by this run (laptop power loss / forced shutdown / dual-boot behaviour are all possible) |
| Media and data-integrity errors | 0 | read, real zero |
| Error-log entries | 0 (64 entries inspected, all `Successful Completion`) | read |
| Warning / critical composite-temperature time | 0 / 0 | read, real zero |
| Thermal-management T1 / T2 transitions | 3,259 / 136 → 3,343 / 140 (lifetime; +84 / +4 over the day) | read |
| Thermal-management T1 / T2 total time | 2,395 s / 34 s → 2,461 s / 35 s (lifetime) | read; unit per spec = seconds |
| Temperature sensors | composite + Sensor 1 (hwmon `temp1`, `temp2`) | read |
| `nvme supported-log-pages` | **unsupported** ("Invalid Log Page 0x109") | unavailable, reported as such |
| Permission failures | none after sudo; the earlier run-1 failures (non-root) are why `--sudo` exists in the guard | — |

No SMART field is treated as healthy because it was missing: unsupported log pages are reported as unavailable.
The device passes every health indicator that exists. The 242 unsafe shutdowns are the one item worth watching; the counter did **not** change across the whole run, including the reboots that happened while the operator was away (power cycles +2, unsafe shutdowns +0 → those were clean). `percentage_used` moved 8 → 9 % during this run; this is a coarse wear indicator and the test traffic (~1 TB of deliberately heavy writes) is a plausible but unproven contributor. Endurance is **not** extrapolated.

## B2 — thermal-management investigation (read-only; nothing was changed)

* Composite thresholds (controller): **WCTEMP 83 °C, CCTEMP 85 °C**; hwmon exposes `max` 82.85 / `crit` 84.85 °C.
* **Host-controlled thermal management is enabled** (feature 0x10): **TMT1 = 81 °C, TMT2 = 83 °C**, MNTMT 45 °C, MXTMT 83 °C (`hctma` = 1). The controller therefore throttles itself starting at 81 °C regardless of the OS.
* Lifetime evidence of throttling: 3,259 T1 transitions (2,395 s in T1) and 136 T2 transitions (34 s) — **the drive has throttled thermally in its life**. Over the whole day of testing the drive logged **+84 T1 / +4 T2 transitions** (+66 s / +1 s). Per sustained-write run (B4): A′ T1 +20 / T2 +1, B′ T1 +51 / T2 +0, C T1 +0 / T2 +0.
* **The “82.85 °C” plateau seen in Run 1 and early Run 2 is the controller reaching TMT2 (83 °C, 356 K)**, i.e. the controller's own second throttle target, not a sensor artefact. The composite sensor is also very fast: +16 °C in 2 s under 2.8 GB/s sequential read and −18 °C in 2 s after stopping.
* APST (autonomous power-state transitions) is enabled; power states 0–2 operational (5.36 / 4.47 / 2.23 W), 3–4 non-operational. PCIe ASPM L1 enabled. No host-side power-management setting limits throughput.
* No setting was changed. A possible mitigation (lower the host-controlled thermal-management targets, i.e. **throttle earlier and more gently**) is a persistent controller configuration change that needs your approval; the evidence below suggests it would not help the sustained-write cliff (not thermal), so it is **not proposed**. Improving the drive's cooling is a physical matter.

## B3 — canonical fio baseline (raw: `../raw/nvme/fio/*-r{1,2,3}.json`; summaries: `fio-summary-repeats.{json,md}`, per-run `fio-summary.{json,md}`)

Method: `scripts/wp1r2_fio_suite.sh` — file-based on ext4 (`~/wp1r2-fio-tmp`, 4 GiB file, never a block device), io_uring, `O_DIRECT`, **10 s per job (3 s ramp), 3 repeats of each of the 15 jobs**, p50/p95/p99/p99.9 completion latency. NVMe cooled to ≤ 50 °C and CPU to ≤ 65 °C before every job; free space, NVMe (4 Hz) and CPU temperature checked during every job (abort: NVMe ≥ 81 °C, CPU ≥ 95 °C for 3 polls or ≥ 100 °C); per-job max NVMe temperature and a `NEAR_THROTTLE` flag (≥ 80 °C) logged in `../raw/nvme/fio/suite.log`.
**Why this replaces the first pass:** the first pass was a single 20 s run per job with a 55 °C start bound and no repeat, so run-to-run variation was unknown and the drive could start warm. All 45 runs here are valid; the coefficient of variation is **< 7 % for every job** (< 2 % for 11 of 15).

| job | side | n | IOPS median [min–max] (CV %) | MiB/s median | p99 µs median [min–max] | p99.9 µs median | max NVMe °C per run |
|---|---|---|---|---|---|---|---|
| mixed-4k-70r30w-qd16-j4 | read | 3 | 182286 [180965–182588] (0.4) | 712.1 | 995.3 [962.6–1044.5] | 8716.3 | 74.85, 74.85, 74.85 |
| mixed-4k-70r30w-qd16-j4 | write | 3 | 78326.9 [77759.8–78450.3] (0.4) | 306 | 207.9 [193.5–209.9] | 8585.2 | 74.85, 74.85, 74.85 |
| randread-4k-qd1 | read | 3 | 11906.2 [11848.6–12197.8] (1.3) | 46.5 | 107 [103.9–109.1] | 195.6 | 69.85, 69.85, 69.85 |
| randread-4k-qd16 | read | 3 | 144528 [144501–147788] (1.1) | 564.6 | 236.5 [234.5–238.6] | 383 | 71.85, 70.85, 72.85 |
| randread-4k-qd32 | read | 3 | 209153 [207565–211181] (0.7) | 817 | 407.6 [403.5–415.7] | 8290.3 | 73.85, 73.85, 74.85 |
| randread-4k-qd32-j4 | read | 3 | 248460 [248419–250634] (0.4) | 970.6 | 1138.7 [1122.3–1138.7] | 9109.5 | 74.85, 73.85, 74.85 |
| randread-4k-qd4 | read | 3 | 46287.3 [45904–47843.6] (1.8) | 180.8 | 142.3 [138.2–146.4] | 228.4 | 69.85, 69.85, 69.85 |
| randread-4k-qd64 | read | 3 | 247986 [246974–250335] (0.6) | 968.7 | 806.9 [790.5–831.5] | 8716.3 | 74.85, 73.85, 72.85 |
| randwrite-4k-qd1 | write | 3 | 55362 [55035.8–55610.7] (0.4) | 216.3 | 20.1 [20.1–20.1] | 72.2 | 69.85, 69.85, 69.85 |
| randwrite-4k-qd16 | write | 3 | 249191 [232620–254694] (3.8) | 973.4 | 160.8 [154.6–183.3] | 2375.7 | 72.85, 72.85, 73.85 |
| randwrite-4k-qd32 | write | 3 | 252277 [235595–256287] (3.6) | 985.5 | 362.5 [350.2–897] | 4751.4 | 73.85, 73.85, 72.85 |
| randwrite-4k-qd32-j4 | write | 3 | 145423 [142795–166022] (6.9) | 568.1 | 3915.8 [3326–6193.2] | 16711.7 | 70.85, 69.85, 70.85 |
| randwrite-4k-qd4 | write | 3 | 210900 [207151–211077] (0.9) | 823.8 | 30.1 [28.3–31.6] | 264.2 | 72.85, 72.85, 72.85 |
| randwrite-4k-qd64 | write | 3 | 251291 [248247–255096] (1.1) | 981.6 | 1351.7 [1302.5–1384.4] | 9240.6 | 73.85, 72.85, 72.85 |
| seqread-1M-qd8 | read | 3 | 2665.6 [2656.6–2696.4] (0.6) | 2666.3 | 10944.5 [10944.5–11206.7] | 11599.9 | 76.85, 80.85, 78.85 |
| seqwrite-1M-qd8 | write | 3 | 1701.1 [1692.8–1716.1] (0.6) | 1701.8 | 12517.4 [12386.3–12517.4] | 15925.2 | 74.85, 74.85, 75.85 |

Reading (medians): sequential read **2,666 MiB/s**; sequential write **1,701 MiB/s** (burst, cache-assisted); 4 KiB QD1 random read **11.9 k IOPS** (p99 107 µs); QD1 random write **55.4 k IOPS** (p99 20 µs); random read 248 k IOPS at QD64 (single job). Random-write figures at QD ≥ 4 (211–252 k IOPS) are **burst figures**: in a 20 s run the same job fell to ≈ 118 k IOPS (first pass) as the write buffer drained (the 4-job QD32 variant, 145 k IOPS with the highest CV, 6.9 %, is the least stable job). Device utilisation 65–94 %; high-QD jobs cost 40–60 % system CPU (io_uring), relevant on this thermally limited CPU.
Only `seqread-1M-qd8` approached the controller's throttle zone (max 76.85 / 80.85 / 78.85 °C across its three repeats); its run-2 result (2,656 MiB/s) is not below the others, i.e. a 10 s read burst is not long enough to throttle. **Sustained** reads are different: see below.

Comparison with Run 1 (Python `O_DIRECT` lower-bound harness): sequential write 1.8 GB/s (same), sequential read 2.2 → 2.7 GB/s, QD1 read 15 k → 11.9 k IOPS (the Python figure was not a fio-comparable number), QD1 write 55 k → 55.4 k. The fio figures are the canonical baseline.

**Sustained sequential read throttles (new finding, `../raw/nvme/aborted-attempt-3-sensor-trace/`).** In a 4 Hz sensor trace, continuous 1 MiB reads at ≈ 2.8 GB/s drove the composite from 46 °C to **82.85 °C (= TMT2) in ≈ 17 s** and bandwidth fell ≈ 19 % at that point. That test was stopped by the suite's own safety bound at the throttle zone and was *not* run longer: characterising steady-state read throttling needs the drive held deliberately in the 81–84 °C zone, which is an **owner decision** (see Remaining work). Consequence: the 2.7 GB/s sequential-read figure is a ≤ 15 s burst number.

Attempt record (kept, nothing edited): `aborted-attempt-1/` (first suite, interrupted by the operator), `aborted-attempt-2/` (job stopped by the then-single-sample CPU rule at 95 °C; the rule was wrong for 1 s boost spikes and was replaced), `fio-single-pass-before-fix/` (single-pass results superseded by the triple repeat), `aborted-attempt-3-sensor-trace/`.

## B4 — sustained-write investigation (raw: `../raw/nvme/sustained-{A,B,C}.json*`, `smart-{before,after}-sust{A,B,C}.json`, `sust{A,B}-monitor.jsonl`; analysis: `sustained-write-analysis.json`; the first-pass analysis `sustained-write-analysis-pass1.json` is kept)

Question: why does sustained sequential write fall from ≈ 1.8 GB/s to ≈ 100–300 MiB/s (Run 1)?
Method (`scripts/wp1r2_sustained_write.py`, reduced by `wp1r2_sustained_analyze.py`): 1 MiB sequential `O_DIRECT` writes to a 24 GiB ext4 file, 48 GiB cumulative, 1 s throughput log, NVMe/CPU temperature at 1 Hz, thermal counters from SMART before/after (sudo, read in the foreground), cold start, then 60 s idle and a 6 GiB burst. Safety stops (NVMe ≥ 82 °C, CPU sustained, free < 28 GiB) never triggered.
**Audit note:** in the first pass, runs A and B had no SMART counters (the background process had no sudo ticket) and A/B were also started warmer than C; **A and B were therefore rerun** (A′, B′) with the same script and counters captured by the foreground reads. Run C (start 29.9 °C, counters captured) was valid and reused. The first-pass A/B (cliff 14.0 and 5.4 GiB) are kept in `superseded-pass-1/`.

| | A′: unthrottled | B′: capped at 500 MiB/s | C: unthrottled, after hours idle |
|---|---|---|---|
| plateau before the drop | 1,724 MiB/s | 500 MiB/s (the cap) | ≈ 1,342 MiB/s (mean over the pre-drop window) |
| first sustained drop | t = 7 s, **10.2 GiB**, NVMe **61.85 °C** | t = 30 s, **14.2 GiB**, NVMe 74.85 °C | t = 6 s, **8.4 GiB**, NVMe **51.85 °C** |
| margin to TMT1 (81 °C) at the drop | −19.1 °C | −6.2 °C | **−29.1 °C** |
| mean rate 20–80 s after the drop | 211 MiB/s | 201 MiB/s | 114 MiB/s |
| NVMe max / CPU max | 80.85 / 98 °C | 80.85 / 95 °C | 77.85 / 93 °C |
| thermal counters over the run (SMART) | **T1 +20, T2 +1** (18 s in T1) | **T1 +51, T2 +0** (40 s in T1) | **T1 +0, T2 +0**, warn/crit time +0 |
| first ≥ 80 °C | t = 101 s | t = 79 s | never |
| burst after 60 s idle | 1,292 MiB/s (from 74.85 °C) | rate-capped phase, 1,490 | 1,320 MiB/s (from 69.85 °C) |

Findings (measured):
1. **The cliff is not thermal.** In A′ the cliff is at 61.85 °C (t = 7 s) and the first ≥ 80 °C reading comes 94 s *later*; in C it is at 51.85 °C with **zero** controller thermal events over the entire run (29 °C margin). In B′ the cliff (t = 30 s) also precedes the first ≥ 80 °C reading (t = 79 s) by 49 s.
2. **Heat adds a modest, later loss on top of the cliff**: B′ after the cliff ≈ 219 → 189 MiB/s once the drive was in the T1 zone; A′ showed no such step. Both runs logged T1 transitions only well after the cliff.
3. **Recovery follows idle time, not temperature:** after 60 s idle a 6 GiB burst runs at 1.3–1.5 GB/s while the drive is still at 70–75 °C.
4. **The volume before the cliff varies: 8.4–14.2 GiB across the three valid runs (5.4–14.0 GiB including the superseded first pass).** The size is not explained by start temperature (the coolest start gave a mid value) nor by idle time before the run. **Cause unidentified.**

Interpretation. **Confirmed:** thermal throttling is not the cause of the sustained-write collapse in the tested conditions. The evidence is **consistent with** exhaustion of a dynamically sized pseudo-SLC write buffer (typical of DRAM-less TLC drives of this class) followed by direct-to-TLC writes at ≈ 110–300 MiB/s. **Not proven:** that it is SLC cache specifically (not exposed by the drive), what sets its size, the floor rate (114–211 MiB/s across runs), and anything above 81 °C held steadily (no run was deliberately held there). Filesystem effects were not isolated (direct I/O, fixed file, no TRIM between runs).

## Operational consequence (engineering, not a threshold change)

Sustained ingest to this drive should be modelled at **≈ 110–300 MiB/s after the first ≈ 8–14 GiB written without idle gaps** (5 GiB is the conservative planning figure), not at 1.8 GB/s. Any workload that writes more than ≈ 5 GiB in a burst (Qdrant 6-copy builds, index rebuilds, large MinIO ingests) should be paced/backpressured. The guard module reports NVMe temperature and new thermal events; it cannot see the cache state. A pacing mechanism is a design item, listed in the report.

## B5 — automated monitoring

`scripts/wp1r2_hostguard.py nvme [--sudo] [--baseline PREV.json]` (read-only, JSON, exit code 0/1/2/3 = OK/WARN/FAIL/UNKNOWN). It distinguishes zero from unsupported/unreadable and reports `permission_denied` as UNKNOWN. 33 unit tests (`tests/unit/test_wp1r2_hostguard.py`). Evidence of use on the live device: `../raw/automation/nvme-final.json` (all 7 health checks OK at 20:09, with `--sudo`), earlier `nvme-health-*.json`. Without `--sudo` the SMART-derived checks are reported UNKNOWN, never OK (`../raw/automation/preflight-final.json`, exit 3). No service or schedule was created.

## What this does not establish

A single device, one boot, one filesystem layout, short runs, and sustained reads/writes in the 81–84 °C zone were deliberately not tested (owner decision). Not a qualification of the drive for multi-day unattended writes; not an endurance statement (9 % used is read, not extrapolated); not a statement about the Windows partitions (never touched); no firmware change was or should be inferred from this.
