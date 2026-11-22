# WP1 blocker register (infrastructure / hardware and FP-05)

**Provenance.** Reconciled on 2026-11-22 against the original register `TDS-Blocks-gpt.odt` ("Anti-Piracy Crawler V2 — Blocker Register
and Hardware Readiness Plan", §1 hardware/infrastructure blockers H-01…H-10, FP-05 in the fingerprinter table; WP1 = H-01, H-02, H-08, FP-05).
Where the original wording differs from the owner's 2026-11-22 decisions, the **owner decision supersedes** and the original is quoted. The
original register defines **no numeric pass threshold** for H-02; its acceptance is "install Ubuntu on the NVMe, then rerun the affected benchmarks"
(Scylla latency gates and the fingerprinter index-capacity test). No threshold was invented or changed here; the gates used are the existing
ones (P2 per-operation p99 targets, E-R1 p95 ≤ 10 ms).

Evidence: [run-3](results/run-3/) (software-suite stability only), [followup-a15.md](followup-a15.md) (A15 NVMe/HDD, GPU, E-R1,
capacity), `fingerprinter/benchmarks/p10-index/results/` (E-R1 history), decisions in
[ADR-028](../../adr/ADR-028-staged-deployment-and-tiered-retention.md).

| ID | Status | Basis | Exact remaining actions |
|---|---|---|---|
| **H-01** single-host environment (original: inventory machines, document intended topology) | **Partially closed** | Owner: option E, staged deployment (ADR-028). Inventory now recorded: A15 (Ryzen 7 7435HS, 15.4 GiB **single 16 GB DDR5 DIMM, rated 5600 MT/s, configured 4800 MT/s** (dmidecode), RTX 3050 4 GiB, one NVMe + one USB HDD); F15 (i5-11400H, 15.7 GB, RTX 2050 4 GB, Samsung NVMe + USB HDD). No other machines listed. | Define the future multi-host topology when expansion is triggered; multi-host readiness is **not** claimed. |
| **H-02** slow storage (original: "Performance testing uses slow storage … USB HDD") | **A15: evidence complete, proposed PASS — owner sign-off needed.** F15: see correction below | Original requirement: NVMe for active databases, index operations and benchmark workloads; rerun the affected benchmarks. A15 `nvme0n1` (WD PC SN740, fw 73113000, PCIe 16 GT/s ×4): **SMART PASSED**, critical warning 0, spare 100 %, used 1 %, media errors 0, thermal-management time 0, error log 64/64 empty, 48 unsafe shutdowns / 1,206 h (read with root, before and after ≈ 0.5 TB of fio writes: unchanged except data units/temperature). **P2 Scylla latency gate rerun on the NVMe: 16/16 operations met their p99 targets in 3/3 runs** (p99 2–21 ms) — on the F15 USB HDD the same harness met **0/16** (p99 0.7–28 s). fio file-based jobs error-free ×2 runs. **E-R1 (index capacity) on the NVMe still does not meet 10 ms p95** → that is FP-05, not H-02. No numeric H-02 criterion exists in the register. | Owner confirms that "benchmarks rerun on NVMe, Scylla gate met, health clean" satisfies H-02 for the A15. F15 remains: its NVMe boot (NitKit) has suite-stability evidence only (no stack, no SMART, no fio); its stack benchmarks ran from a USB-HDD boot. |
| **H-03** Scylla multi-node RF=3 | **Blocked** | Needs multi-node Scylla infrastructure. Local single-node tests do not count. | Provision ≥3 Scylla nodes; run RF=3 qualification. |
| **H-04** physical multi-host deployment | **Blocked** | Needs real multiple hosts. | Deploy on ≥2 physical hosts per the topology defined under H-01. |
| **H-05** GPU worker not validated across machines (original: reverify on the RTX 2050 host) | **Partially closed** | Owner: GPU host is flexible (ADR-028). A15 RTX 3050 measured with the existing P9 encoder benchmarks: G-5 GPU FP32 ≥ 5× CPU FP32 → **9.02× PASS** on C-PREC frames (256 frames, 3 reps); CUDA FP32 vs CPU FP32 cosine min 0.9999998 over 10,267 segments, 0 below 0.999; calibrated batch 8; peak VRAM 3,450 MiB total incl. desktop. The FP16 gated spec was **not** accepted (7 segments < 0.999, G-2 FAIL — unchanged policy: FP16 stays a separate gated spec). Run 3's GPU metrics were desktop graphics and are not evidence. | Cross-host compatibility: run the same benchmarks on any other GPU host that will take the `encoder` role (F15 RTX 2050 results exist from 2026-10, different driver). Owner sign-off of the A15 as a qualified encoder host. L-PAIRS G-3 (labelled decisions) was not run on the A15. |
| **H-06** Redis HA | **Open** | No decision made. | Owner decision (single Redis + restore vs Sentinel/Cluster); then test. |
| **H-07** MinIO durability / deployment | **Open** | No decision made. | Owner decision (single-drive vs erasure-coded multi-drive/host); then test. |
| **H-08** storage capacity and retention | **Open** | Owner: tiered retention accepted (ADR-028). Measurements: [followup §Storage](followup-a15.md); model skeleton `benchmarks/p2-storage/capacity_model.py` (formula + measured unit sizes; every workload input undefined). **No production capacity figure exists.** | Owner supplies workload (pages/day, media/day, embeddings/day), retention days per tier and safety factor; measure per-record Scylla/MinIO sizes on a populated dev store; approve a capacity target; qualify the HDD tier under the real I/O mix (the HDD measured is a USB 2.5" drive) and implement tier migration. |
| **H-09** 168-hour production soak | **Blocked** | Needs the production-like deployment and prerequisites (H-03, H-04, H-06, H-07, H-10 decisions). Run 3 (2 h 42 m) is **not** a soak. | Complete prerequisites, then run 168 h. |
| **H-10** network isolation | **Not tested** | Policy and test outstanding. | Define policy; run the isolation test. |
| **FP-05** Qdrant / index-capacity (original: "Index-capacity test needs faster storage — rerun the index benchmark on SSD-class storage after the storage and index requirements are approved"; owner 2026-11-22: reduce returned payload fields first) | **Open** | **All F15 E-R1 evidence was measured on a 5400 rpm USB HDD (`sda`, WD5000LPVT)** and the original validation attributes the failure to disk-bound rescoring on that HDD. On the A15 NVMe (first SSD-class run) the same E-R1 harness at 1.5 GiB: 1× steady p95 11.0 ms (whole payload) / 10.15 ms (reduced payload, FP-05 change), rebuild p95 ~21–25 ms; 2× 16.9/14.6 ms; 3× 26.6/18.9 ms; 6× aborted (cgroup lost, anon 1.43 GiB). Gate (p95 ≤ 10 ms in phases 3 and 5) **not met** at any scale. **2026-11-23 step 2** (production `score_threshold=floor` + 1 indexing thread during rebuild): 1× steady 10.34 ms / rebuild p95 12.2 ms, 2× 13.1, 3× 16.6, 6× completed (no abort) but p95 129.5 ms and anon 1.441 GiB > 1.35 → **FP-05 EXIT GATE NOT MET**, status stays Open ([followup §5.1](followup-a15.md)). Thresholds, `index.max_points` and gate unchanged. [followup §5](followup-a15.md) | Owner decision on the next lever: payload kept off Qdrant's hot path (sidecar subject map) or lower `limit`; a 6× memory design / capacity target (followup §5.1) and/or the "approved capacity target" the original FP-05 requires; rerun E-R1. Closure only on a passing gate. |

Run 3 incident A-10 (system Redis `127.0.0.1:6379` db 15 `FLUSHDB`, worker stream/group operations during aborted attempts and
manual validation; contents of DB 15 not inspected afterwards, effect **unknown**): recorded in
[run-3 anomalies](results/run-3/anomalies.md). The valid Run 3 never touched 6379. Mitigation done: the fingerprinter test default
is now a throwaway Redis on port 16380 and the fixtures refuse ports 6379/16379.

## Correction: what the F15 evidence actually is (`monitoring_runs/`, 2026-11-14)

The earlier version of these docs (and the owner brief) spoke of a "previously qualified F15 NVMe baseline / F15 local RF=1 qualification". The
raw Run 2 evidence now supplied shows two different boots of the same laptop (i5-11400H, RTX 2050):

| Run 2 half | Boot disk | What it contains | Not contained |
|---|---|---|---|
| `NitKit_20261114T110026Z` — **System A, Samsung MZVLQ512HBLU NVMe**, crawler2 `60f3f95`, fingerprinter `904af14` | NVMe | 3-h software-suite stability: crawler2 1147 passed/165 skipped (2 repeat failures, one test), fingerprinter 445 passed (no p10), ruff/mypy, synthetic soak; disk util mean 2.6 %, no swap | **No Docker access, no Scylla/Redis/MinIO/Qdrant, no application benchmark, no SMART, no fio** |
| `Ubuntu-24-04_20261114T110118Z` — **System B, WDC WD5000LPVT USB HDD** | USB HDD | stack benchmarks incl. the P2 latency gate (**0/16 operations met p99 targets**, p99 0.7–28 s, 5 repeats); the E-R1/diag runs of 2026-10/11 were also on this HDD | — |

So the F15 NVMe was never storage-qualified (no health, fio or stack data), and no "RF=1 local qualification" exists in this evidence — the only
stack latency evidence from the F15 is a **failure on the USB HDD**. The A15 NVMe results in [followup-a15.md](followup-a15.md) are the first
SSD-class stack benchmarks. If the owner holds F15 NVMe stack/fio evidence not in `monitoring_runs/`, it was not supplied.

