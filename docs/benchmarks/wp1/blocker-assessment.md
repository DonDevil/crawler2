# WP1 — blocker closure assessment

Authoritative source: `TDS-Blocks-gpt.odt` ("Anti-Piracy Crawler V2 — Blocker Register and Hardware Readiness Plan", found at `~/Desktop/TDS-Blocks-gpt.odt`; a near-identical older `TDS-Blocks.odt` sits beside it). Wording below is quoted from §1 of that file and its WP table (WP1 = H-01, H-02, H-08, FP-05).
Statuses: CLOSED · PARTIALLY CLOSED · OPEN — ADDITIONAL TEST REQUIRED · OPEN — OWNER DECISION REQUIRED · OPEN — PHYSICAL INFRASTRUCTURE REQUIRED · NOT APPLICABLE TO WP1. **No acceptance gate was weakened; no blocker is marked CLOSED.**
Paths are relative to `crawler2/docs/benchmarks/wp1/` unless stated.

## H-01 — Current development environment uses a single host

| Field | |
|---|---|
| Original requirement | Issue: "Current development environment uses a single host". Needed: "Inventory the available machines and document the intended deployment topology." Requirement: "Machine count, CPU, RAM, disks, GPU, network connectivity." Category: "Owner decision + hardware inventory" |
| WP1 evidence | Complete inventory of **this one machine**: 1 host, i5-11400H 6C/12T, 15.4 GiB RAM + 4 GiB swap, one 476.9 GB NVMe (shared with Windows; 147 GB ext4 root), RTX 2050 4 GiB, Wi-Fi only; Docker topology of 5 services. |
| Result | The host inventory is verified and documented. No second machine exists/was inventoried; **no intended deployment topology is documented or approved**. |
| Status | **PARTIALLY CLOSED** |
| Remaining gap | Inventory of any other available machines; the owner's intended topology (hosts per role: crawler, GPU, DB, storage). |
| Next action | Owner: list available/purchasable machines and approve a target topology. |
| Evidence | `hardware-inventory.md` |

## H-02 — Performance testing uses slow storage

| Field | |
|---|---|
| Original requirement | "Scylla latency gates and the fingerprinter index-capacity test cannot be evaluated reliably using the current USB HDD." Needed: "Install Ubuntu on the NVMe SSD, then rerun the affected benchmarks. Keep the HDD for backups and bulk storage where appropriate." Requirement: "NVMe SSD for active databases, index operations, and benchmark workloads." |
| WP1 evidence | Ubuntu root and `/var/lib/docker` are on `nvme0n1p7` (no HDD attached). **P2 Scylla latency gate rerun twice at 10×: every one of 16 p99 targets met, 100 units/s/host sustained, 0 failed units, publication lag p99 2.54–2.56 s (≤ 5 s)**; HDD: 25 units/s, all targets missed, lag p99 237 s, `sda` 100 %. NVMe disk utilisation 29 % at 10×. **E-R1 index test rerun** (3 runs): the storage cause is removed (rebuild p95 5.1 s → 23 ms; steady p95 154 ms → 10.1 ms) — pass/fail outcome is FP-05. I/O baseline: seq 1.8 GB/s write, 2.2 GB/s read; 4 KiB QD1 read 15 k IOPS / p99 0.09 ms; QD1 write 55 k IOPS. |
| Result | The affected benchmarks **can now be evaluated and were**; the Scylla gate is met on NVMe (single node, RF = 1, 60 s × 2 repeats). |
| Status | **PARTIALLY CLOSED** |
| Remaining gap | (a) **NVMe health unverified** (SMART unreadable without root; `smartctl`/`nvme-cli` missing); (b) canonical `fio` numbers not obtained (Python `O_DIRECT` substitute, lower bounds); (c) **sustained writes beyond ≈ 35 GiB collapse to ≈ 250–300 MB/s while the drive sits at 78–83 °C** — cause (cache vs thermal) not separated; (d) gate evidence is two 60 s runs on a thermally limited laptop, not hours; (e) `docs/benchmarks.md` still lists the P2 10× gate as "not met on the dev host" (documentation row not changed in WP1). |
| Next action | Approve `sudo apt install fio smartmontools nvme-cli`; run `sudo smartctl -a /dev/nvme0`, `sudo nvme smart-log /dev/nvme0` and the fio profile in `README.md`; owner updates the P2 row after review. Then H-02 can be closed. |
| Evidence | `nvme-benchmark.md`; `database-baseline.md`; `raw/stageB-*`; `benchmarks/p2-storage/results/20261117T133006Z, …T133231Z, …T133518Z`; `raw/stageD-p2-*` |

## H-03 — Three-node Scylla cluster does not exist

| Field | |
|---|---|
| Original requirement | "Single-node Scylla with replication factor 1 cannot prove replication, quorum behavior, or node-loss recovery." Needed: "Provision three Scylla nodes and configure RF=3 with the required topology and tablets settings." Requirement: "Three independent Scylla nodes; sufficient RAM and fast storage on each." |
| WP1 evidence | Observed: one node (`--smp 2`, 1,400 MB, 2 GiB container, developer mode), **every keyspace RF = 1**, `enable_tablets: true`, `nodetool status` cannot start in the container (host `aio-max-nr`). Single-node latency baseline only. |
| Result | Establishes the single-node baseline and tablets setting. **No replication, quorum or node-loss evidence exists or could exist here.** |
| Status | **OPEN — PHYSICAL INFRASTRUCTURE REQUIRED** |
| Remaining gap | Three independent nodes, RF = 3, quorum and node-loss tests. (Three containers on this 15 GiB laptop would not be "independent nodes with sufficient RAM"; whether such a rehearsal is acceptable is an owner decision.) |
| Next action | Owner: provide three hosts (or approve a representative topology); then provision RF = 3 and run replication/quorum/recovery tests; re-run the P2 gate on that cluster. |
| Evidence | `database-baseline.md` (Scylla section), `hardware-inventory.md` |

## H-04 — Physical multi-host topology is unverified

| Field | |
|---|---|
| Original requirement | "Separate crawler, GPU, database, and storage services have not been tested together across physical machines." Needed: "Deploy the intended services on their actual hosts and verify cross-host contracts and connectivity." Requirement: "Multiple machines or another explicitly approved, physically representative topology." |
| WP1 evidence | Everything ran on one host over Docker bridge/loopback; the two-host profile is two containers on one machine. |
| Result | None applicable (a local bridge does not exercise physical networking, latency, partitions or host loss). |
| Status | **OPEN — PHYSICAL INFRASTRUCTURE REQUIRED** |
| Remaining gap | Multiple physical machines or an explicitly approved representative topology; cross-host contract/connectivity tests. |
| Next action | Same owner decision as H-01/H-03. |
| Evidence | `hardware-inventory.md` |

## H-05 — GPU worker has not been validated across machines

| Field | |
|---|---|
| Original requirement | "The fingerprinter's deployment and contract compatibility have not been verified across hosts." Needed: "Reverify the fingerprinter's 13-day findings and contract compatibility on the actual GPU host." Requirement: "RTX 2050 host, network access, matching contract versions." |
| WP1 evidence | In the project venv: torch 2.13.0+cu130, CUDA 13.0, RTX 2050 usable; offline model load works (0 external connection attempts); CPU/CUDA embeddings `[B,768]`, finite, unit norm, cosine 0.99999994; GPU/CPU throughput 6.7× (43.4 vs 6.5 frames/s) reproducing October within 1.5 %; VRAM 0.5 GiB steady, 3.5 GiB peak during calibration; GPU reached 87 °C with SW power/thermal limit flags. |
| Result | **Local GPU compatibility of the actual encoder is verified on this host.** Nothing was verified across hosts or over a network; "13-day findings" is not defined in the documents inspected and was **not** re-verified; `nvidia-smi` detection was not used as evidence. |
| Status | **PARTIALLY CLOSED** |
| Remaining gap | Cross-host contract-version and network verification; mapping and re-verification of the "13-day findings"; confirmation that this laptop is the intended GPU host. |
| Next action | Owner confirms the GPU host and the meaning of "13-day findings"; run the contract/compat tests (`tests/v2`, P10 integration) from the crawler host against the GPU host. |
| Evidence | `fingerprinter-baseline.md`; `raw/stageE-*` |

## H-08 — Production storage capacity is undefined

| Field | |
|---|---|
| Original requirement | "It is not yet established how much storage is needed for page snapshots, media, embeddings, indexes, logs, and evidence." Needed: "Estimate storage growth from measured workload and establish capacity and retention limits." Requirement: "SSD capacity, HDD capacity, expected data volume, retention period." Category: "Owner decision + measurement" |
| WP1 evidence | Local headroom measured (59.2 GiB free, volumes 5.8 GB, datasets 26 GB). Measured Qdrant volume/point (5.2–11.2 KB, ≥ ×2 range), MinIO object overhead (≈ 14 KB per 32 KiB object), Scylla commitlog behaviour. |
| Result | A useful **local** capacity assessment. No expected data volume, retention period or SSD/HDD capacity target exists, so no production figure can be derived (none invented). |
| Status | **OPEN — OWNER DECISION REQUIRED** |
| Remaining gap | Approved workload model (pages/day, media volume, embeddings per item, retention, evidence retention) and the capacity limits derived from it; HDD tier sizing. |
| Next action | Owner supplies workload and retention assumptions; then compute capacity with the per-unit rates in `storage-capacity.md` and verify with a measured run. |
| Evidence | `storage-capacity.md` |

## H-09 — Seven-day test environment does not exist

| Field | |
|---|---|
| Original requirement | "The soak cannot validate the actual production topology without the required machines and services." Needed: "Prepare the physical deployment and run a preflight before starting the 168-hour soak." Requirement: "Stable network, adequate storage, memory, and continuously running hosts." |
| WP1 evidence | Longest continuous WP1 workload ≈ 19 min; CPU package at 85–90 °C average (up to 100 °C) under 2–5 minute multi-core loads; NVMe at its 82.8 °C `high` threshold during writes; Wi-Fi-only laptop; shared 147 GB partition; sustained-write collapse. |
| Result | Not a soak environment; the evidence argues **against** using this laptop for an unattended 168 h run without cooling/power evidence. No preflight was possible (no production topology). |
| Status | **OPEN — PHYSICAL INFRASTRUCTURE REQUIRED** |
| Remaining gap | The physical deployment (H-01/H-03/H-04), a preflight, then the 168 h run. |
| Next action | After topology approval: preflight checklist using this monitor; short (≥ 1 h) soak rehearsal on the target hosts. |
| Evidence | `system-monitoring.md` |

## FP-05 — Index-capacity test needs faster storage

| Field | |
|---|---|
| Original requirement | "The existing HDD environment cannot establish the intended index-capacity performance." Needed: "Rerun the index benchmark on SSD-class storage after the storage and index requirements are approved." Requirement: "NVMe or equivalent SSD, approved capacity target." Category: "Hardware + engineering + owner decision" |
| WP1 evidence | E-R1 harness rerun on NVMe in a dedicated 1.5 GiB / 2-CPU Qdrant container: **1 copy ×2 — FAIL** (steady p95 10.111 / 10.171 ms vs ≤ 10 ms; rebuild p95 23.3 / 22.8 ms; memory/count/completion rules pass; 0 errors); **6 copies — FAIL** (anon 1.44 GiB > 1.35, container OOM-killed in rebuild, 1,265 errors, warm p95 67 ms). HDD → NVMe gains: ingest 3–7×, rebuild p95 ≈ 220×, steady p95 15×. |
| Result | The "faster storage" part of the requirement is **met and the benchmark now runs to a result**; the index-capacity **acceptance criteria are not met** (marginally at 1 copy steady state, clearly in rebuild and at 6 copies where memory is the limit). The "approved capacity target" is the unapproved provisional 250,000 (`index.max_points`; CAP-8 forbids raising it without a passing gate-eligible E-R1 report). |
| Status | **OPEN — OWNER DECISION REQUIRED** |
| Remaining gap | A passing, gate-eligible E-R1 report at an approved target. The design lists the options as separate operator decisions, none applied: larger memory limit (D14), originals in RAM for small roles (D2 change), a different latency criterion, an SSD index host. |
| Next action | Owner decides among those options and approves the capacity target; then rerun E-R1 (same harness, fresh container per run). Re-examining the 2/3-copy scales is an owner call. |
| Evidence | `database-baseline.md` (Qdrant section); `raw/stageD-er1-*`; `fingerprinter/benchmarks/p10-index/results/e-r1-1copy-nvme.json`, `…-nvme-run2.json`, `e-r1-6copies-nvme.json` |

## Register items outside WP1 (touched only by observation)

H-06 (Redis HA), H-07 (MinIO durability), H-10 (network isolation): **NOT APPLICABLE TO WP1**. Observed only: Redis and MinIO are single-instance; all Docker ports are loopback-bound; no failover/recovery or egress test was run.

## Decision summary

1. **Blockers we can close now:** none (no blocker's complete requirement is satisfied by WP1 evidence).
2. **Close after additional local verification:**
   - **H-02** — after sudo-approved `smartctl`/`nvme smart-log` (acceptance: no critical warning, no media/data-integrity errors, plausible percentage used and spare) and a canonical `fio` run on the same file system (acceptance: owner-approved NVMe floor; nothing numeric is defined in the register), plus the owner's note on sustained-write behaviour.
   - **H-05 (local part)** — if the owner confirms this laptop is the GPU host: run the contract/compat suites on it and map the "13-day findings"; acceptance: all pass, findings reproduced.
   - **H-01** — after the owner documents the intended topology and any additional machines.
3. **Cannot be closed on this machine:** **H-03, H-04, H-09** (need physical hosts/topology), **H-08** (needs owner workload/retention model), **FP-05** (needs owner decisions on capacity target, memory limit/design and latency criterion before a rerun can pass).

Recommended next action within WP1: obtain approval to install `fio smartmontools nvme-cli`, capture SMART health and the canonical fio baseline, and update the P2 benchmark row; WP2 is not started.


## Run 2 update (2026-11-18) — host baseline completion

Full evidence: `results/run-2/run-2-report.md`. No acceptance gate was weakened.

| Item | Status after Run 2 | Basis |
|---|---|---|
| H-01 | PARTIALLY_CLOSED | single host fully inventoried; no topology/other machines (owner) |
| H-02 | **CLOSED for the stated scope** (local, single-node, RF = 1, this NVMe); owner to confirm | SMART clean; canonical fio ×3; sustained-write cliff characterised (not thermal); P2 10× passed 3×; automated NVMe monitoring. Not closed for RF=3/multi-host/long-duration |
| H-03, H-04, H-09 | BLOCKED | need physical hosts / topology / 7-day environment |
| H-05 | PARTIALLY_CLOSED | local GPU compatibility, numerics, offline operation proven; cross-machine validation absent |
| H-08 | BLOCKED | needs owner workload/retention model; local storage model supplied |
| FP-05 | OPEN | E-R1 fails at 1×/2×/3× (latency) and 6× (OOM); memory predictable; payload retrieval limits latency; no gate-passing scale; `index.max_points` unchanged |
| H-06, H-07, H-10 | NOT_TESTED | outside WP1 |

Owner decisions needed: bounded 81–84 °C test; PL1/power-profile A/B; fingerprinter env rebuild; E-R1 measurement method (gRPC vs REST); payload-field product change; workload/retention model; topology.
