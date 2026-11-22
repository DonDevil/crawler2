# WP1 blocker register (infrastructure / hardware and FP-05)

**Provenance.** The original register (`TDS-Blocks-gpt.odt`) is **not present on this machine or in any repository** (searched
`/home/dhanush`, 2026-11-22). This file is the working register rebuilt from the owner decisions of 2026-11-22 and the evidence
in this repository; entries H-03, H-04, H-06, H-07, H-09, H-10 carry only the status the owner stated — their original wording and
acceptance criteria could not be consulted. **No acceptance threshold was invented or changed here.** If the `.odt` is supplied,
reconcile this file against it.

Evidence: [run-3](results/run-3/) (software-suite stability only), [followup-a15.md](followup-a15.md) (A15 NVMe/HDD, GPU, E-R1,
capacity), `fingerprinter/benchmarks/p10-index/results/` (E-R1 history), decisions in
[ADR-028](../../adr/ADR-028-staged-deployment-and-tiered-retention.md).

| ID | Status | Basis | Exact remaining actions |
|---|---|---|---|
| **H-01** deployment / hardware strategy | **Partially closed** | Owner: option E, staged deployment; local development first, hosts added when workload justifies; no permanent GPU host (ADR-028). | Define the future multi-host topology when expansion is triggered. Multi-host production readiness is **not** claimed. |
| **H-02** local SSD/NVMe qualification | **F15 qualification stays valid for its own scope. A15: measured, but no pass/fail** | A15 `nvme0n1` (WD PC SN740, fw 73113000, PCIe 16 GT/s ×4): fio on `/mnt/data` (partition p9) — 4 MiB…100 GiB file-based jobs, no I/O/ext4 errors, no new AER on the NVMe, sustained-write drop at ~28 s (see followup). **SMART/NVMe health not read: root required (`sudo` needs a password; `/dev/nvme0n1` is `root:disk`, user not in `disk`).** The repository defines **no testable H-02 criterion**, and the F15 qualification record is not in the repo. F15 results are not transferred. | (1) Someone with root runs `sudo smartctl -x /dev/nvme0n1` (SMART/health, error log, unsafe shutdowns, wear), ideally before and after re-running `fio_qualify.py`. (2) Owner states the H-02 pass criteria. Until then A15 H-02 = **PENDING**, not passed. |
| **H-03** Scylla multi-node RF=3 | **Blocked** | Needs multi-node Scylla infrastructure. Local single-node tests do not count. | Provision ≥3 Scylla nodes; run RF=3 qualification. |
| **H-04** physical multi-host deployment | **Blocked** | Needs real multiple hosts. | Deploy on ≥2 physical hosts per the topology defined under H-01. |
| **H-05** GPU hosting / qualification | **Partially closed** | Owner: GPU host is flexible (ADR-028). A15 RTX 3050 measured with the existing P9 encoder benchmarks: G-5 GPU FP32 ≥ 5× CPU FP32 → **9.02× PASS** on C-PREC frames (256 frames, 3 reps); CUDA FP32 vs CPU FP32 cosine min 0.9999998 over 10,267 segments, 0 below 0.999; calibrated batch 8; peak VRAM 3,450 MiB total incl. desktop. The FP16 gated spec was **not** accepted (7 segments < 0.999, G-2 FAIL — unchanged policy: FP16 stays a separate gated spec). Run 3's GPU metrics were desktop graphics and are not evidence. | Cross-host compatibility: run the same benchmarks on any other GPU host that will take the `encoder` role (F15 RTX 2050 results exist from 2026-10, different driver). Owner sign-off of the A15 as a qualified encoder host. L-PAIRS G-3 (labelled decisions) was not run on the A15. |
| **H-06** Redis HA | **Open** | No decision made. | Owner decision (single Redis + restore vs Sentinel/Cluster); then test. |
| **H-07** MinIO durability / deployment | **Open** | No decision made. | Owner decision (single-drive vs erasure-coded multi-drive/host); then test. |
| **H-08** storage capacity and retention | **Open** | Owner: tiered retention accepted (ADR-028). Measurements: [followup §Storage](followup-a15.md); model skeleton `benchmarks/p2-storage/capacity_model.py` (formula + measured unit sizes; every workload input undefined). **No production capacity figure exists.** | Owner supplies workload (pages/day, media/day, embeddings/day), retention days per tier and safety factor; measure per-record Scylla/MinIO sizes on a populated dev store; approve a capacity target; qualify the HDD tier under the real I/O mix (the HDD measured is a USB 2.5" drive) and implement tier migration. |
| **H-09** 168-hour production soak | **Blocked** | Needs the production-like deployment and prerequisites (H-03, H-04, H-06, H-07, H-10 decisions). Run 3 (2 h 42 m) is **not** a soak. | Complete prerequisites, then run 168 h. |
| **H-10** network isolation | **Not tested** | Policy and test outstanding. | Define policy; run the isolation test. |
| **FP-05** Qdrant query latency (E-R1 p95 ≤ 10 ms) | **Open** | Owner: reduce returned payload fields first. Implemented ([p10-matcher.md §5.1](../../../../fingerprinter/docs/p10-matcher.md)); E-R1 rebenchmark on A15: see [followup §E-R1](followup-a15.md). Gate, thresholds, `index.max_points` unchanged. | Closure only if the E-R1 gate passes at the required scales on the gate configuration (1.5 GiB container) — otherwise a new owner decision. |

Run 3 incident A-10 (system Redis `127.0.0.1:6379` db 15 `FLUSHDB`, worker stream/group operations during aborted attempts and
manual validation; contents of DB 15 not inspected afterwards, effect **unknown**): recorded in
[run-3 anomalies](results/run-3/anomalies.md). The valid Run 3 never touched 6379. Mitigation done: the fingerprinter test default
is now a throwaway Redis on port 16380 and the fixtures refuse ports 6379/16379.
