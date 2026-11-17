# WP1 — NVMe and local hardware baseline (Anti-Piracy V2)

Run 2026-11-17 on the Ubuntu 24.04.5 / NVMe laptop (i5-11400H, 15.4 GiB, RTX 2050 4 GiB). Single host, single-node databases. Evidence is in this directory; raw outputs are in `raw/`.

| Document | Content |
|---|---|
| `hardware-inventory.md` | Stage A: hardware, storage, Docker, services, Python/GPU environment, Git state, missing tools |
| `nvme-benchmark.md` | Stage B: `O_DIRECT` I/O baseline, sustained-write behaviour, limits of the method |
| `system-monitoring.md` | Stage C: per-workload CPU/RAM/swap/disk/thermal/GPU/container statistics |
| `database-baseline.md` | Stage D: Scylla (P2 10× latency gate), Redis, MinIO, Qdrant (E-R1) |
| `fingerprinter-baseline.md` | Stage E: offline encoder, CPU vs GPU, VRAM |
| `storage-capacity.md` | Disk/volume consumption and supportable projections |
| `blocker-assessment.md` | Per-blocker closure table (H-01…H-09, FP-05) and the decision lists |
| `scripts/` | `wp1_monitor.py` (read-only monitor), `wp1_nvme_bench.py`, `wp1_encoder_check.py`, `wp1_analyze.py`, `wp1_cleanup_bench_bucket.py` |

## A. Executive summary

**Outcome.** WP1 produced a reproducible local baseline and a defensible blocker assessment. **No blocker is closed.** H-01, H-02 and H-05 are partially closed; H-03, H-04, H-09 need physical infrastructure; H-08 and FP-05 need owner decisions.

**Suitability.** The NVMe installation is **suitable for continued local engineering and validation**, and it removes the HDD as the bottleneck: the P2 Scylla gate "p99 at 10× expected per-host load" — **not met on the HDD (25 units/s, every target missed)** — is **met on NVMe twice** (100 units/s/host, 0 failures, all 16 p99 targets, publication-lag p99 2.55 s). The index-capacity benchmark (E-R1) now runs to a result: its HDD-era latency failures shrink by 15–220× but it **still fails** (1 copy: steady p95 10.1 ms vs ≤ 10 ms, rebuild p95 23 ms; 6 copies: Qdrant OOM-killed at its 1.5 GiB cap). The GPU encoder reproduces its October throughput (GPU 43.4 vs CPU 6.5 frames/s, 6.7×) offline.

**Major bottlenecks (measured).**
1. **Heat, not the disk or the database**: package temperature 95–100 °C within seconds of heavy load and 85–90 °C average for minutes (74 % of the 6-copy run ≥ 90 °C); GPU 87 °C with power/thermal limit flags; NVMe at its 82.8 °C `high` threshold.
2. **Memory budgets**: Qdrant (1.5 GiB) dies during a 626 k-point rebuild; MinIO ran at its 512 MiB limit; Scylla used 1.4 of 2 GiB.
3. **NVMe sustained writes**: ≈ 1.8 GB/s for ≈ 20 s (≈ 35 GiB), then ≈ 250–300 MB/s for the rest of a 100 GiB write at 78–83 °C (cache exhaustion vs thermal throttling not separated).

**Major risks.** NVMe health is **unknown** (SMART needs root; tools absent). A Windows installation shares the drive. The laptop is thermally marginal for any long unattended run (H-09). All database evidence is single-node RF = 1 — it proves nothing about replication.

**Not completed:** `fio` baseline; NVMe SMART/health; Scylla compaction metrics; MinIO object counts; dedicated Redis write benchmark; any run > 20 min; E-R1 at 2/3 copies.

## Checkpoints

| Stage | State |
|---|---|
| A inventory | Completed (no NVMe SMART data; no `fio`) |
| B NVMe baseline | **Partially complete** — Python `O_DIRECT` substitute for `fio`; health unavailable; QD32 numbers are short bursts |
| C monitoring | Completed for ≤ 19 min workloads |
| D databases/index | Completed: Scylla P2 gate, Redis (read-only), MinIO (inventory), Qdrant E-R1. Incomplete: compaction metrics, object counts |
| E fingerprinter | Completed (CPU/GPU, offline, VRAM) |
| Capacity | Completed locally; production capacity requires owner input |
| Blocker assessment | Completed |

## Deviations from the task text and limitations (all deliberate or forced; none hidden)

1. **`fio`, `smartctl`, `nvme-cli` are not installed and `sudo` needs a password**; nothing was installed (a system change requiring approval). A Python `O_DIRECT` harness was used instead; numbers are lower bounds and not comparable with `fio`.
2. The I/O harness' first attempt was **aborted by my monitor's 95 °C rule** (kept as `raw/stageB-attempt1-*`); the rule was relaxed to truncate-and-cool per case (hard disk/swap/memory aborts unchanged). One sustained-write attempt was ended early by an unexplained watcher trigger (`raw/stageB-sustained-attempt1-truncated.json`) and was repeated cleanly.
3. For the P2 benchmark, two idle containers (`app-host-1/2`, `--no-deps --no-build`) were started and later removed; no existing container was restarted.
4. Qdrant was tested through the E-R1 harness in a **dedicated container/volume** instead of an isolated collection on the dev instance (rationale in `database-baseline.md`).
5. Benchmark-created resources that I deleted, and only those: keyspace `crawler2_bench_latency` + its 118 `pre-drop-*` snapshot tags; bucket `crawler2-bench` (25,200 objects); containers/volumes `qdrant-e-r1` / `wp1-qdrant-e-r1`; I/O test directories `~/wp1-bench-tmp*`. No project keyspace, bucket, collection, Redis key or volume was altered.
6. Two of my own shell commands were killed when `pkill -f` matched their own command line; the monitor was restarted and no measurement or system state was affected.
7. The machine hostname was redacted to `<host>` in the committed result files (otherwise unmodified). The host was not quiesced (desktop session, Firefox, this session, a separate system Redis on 6379, the idle stack). `docs/benchmarks.md` (which still lists the P2 10× gate as unmet) was **not** edited.

## Reproduction

```bash
cd ~/anti_piracy/crawler2
# monitor (any workload); stop with kill <pid>; flag file appears on hard-limit breach
python3 -I docs/benchmarks/wp1/scripts/wp1_monitor.py out.jsonl --interval 2 --abort-flag ABORT &
# NVMe substitute benchmark (≈ 6 min + 2.5 min sustained; writes 4 GiB + ~100 GiB of overwrites)
python3 -I docs/benchmarks/wp1/scripts/wp1_nvme_bench.py nvme.json --dir ~/wp1-bench-tmp --size-gib 4 --secs 20 --runs 3 --sustained-secs 120 --abort-flag ABORT
# P2 Scylla gate (needs the two idle host containers; remove them afterwards; delete crawler2_bench_latency + bucket crawler2-bench afterwards)
COMPOSE_PROFILES=two-host docker compose up -d --no-deps --no-build app-host-1 app-host-2
COMPOSE_PROFILES=two-host RATE=100 DURATION=60 benchmarks/p2-storage/run.sh latency
# E-R1 (per run: fresh container + volume, fresh random key; see database-baseline.md)
docker run -d --name qdrant-e-r1 --memory 1.5g --memory-swap 1.5g --cpus 2 -p 127.0.0.1:26333:6333 -p 127.0.0.1:26334:6334 \
  -v wp1-qdrant-e-r1:/qdrant/storage -e QDRANT__TELEMETRY_DISABLED=true -e QDRANT__SERVICE__API_KEY=<random> \
  qdrant/qdrant@sha256:d122138f76868edba68d36cb0833139c1d1761f00f09e48e61f8314196e6a4c6
cd ~/anti_piracy/fingerprinter
FINGERPRINTER_INDEX__API_KEY=<random> ~/anti_piracy/datasets/p10/venv/bin/python -I benchmarks/p10-index/capacity_e_r1.py \
  --vectors ~/anti_piracy/datasets/p10/vectors --copies 1 --container qdrant-e-r1 \
  --cgroup /sys/fs/cgroup/system.slice/docker-<container id>.scope --out <result>.json --samples <samples>.jsonl
# encoder (offline)
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 env/bin/python benchmarks/p9-encoder/bench_inference.py --corpus ~/anti_piracy/datasets/p9/c-prec --frames 256 --reps 3 --out <result>.json
PYTHONPATH=. env/bin/python ~/anti_piracy/crawler2/docs/benchmarks/wp1/scripts/wp1_encoder_check.py
# aggregate
python3 -I docs/benchmarks/wp1/scripts/wp1_analyze.py docs/benchmarks/wp1/raw/summary-monitoring.json
```

Canonical I/O baseline to run once `fio`, `smartmontools`, `nvme-cli` are approved and installed (directory on the same ext4, never a block device):

```bash
sudo smartctl -a /dev/nvme0 ; sudo nvme smart-log /dev/nvme0
mkdir -p ~/wp1-fio-tmp && cd ~/wp1-fio-tmp
fio --name=seqw --rw=write --bs=1M --iodepth=8 --ioengine=io_uring --direct=1 --size=4G --runtime=60 --time_based --filename=./t
fio --name=randr --rw=randread --bs=4k --iodepth=32 --numjobs=4 --ioengine=io_uring --direct=1 --size=4G --runtime=60 --time_based --group_reporting --filename=./t
fio --name=randw --rw=randwrite --bs=4k --iodepth=32 --numjobs=4 --ioengine=io_uring --direct=1 --size=4G --runtime=60 --time_based --group_reporting --filename=./t
fio --name=mix --rw=randrw --rwmixread=70 --bs=4k --iodepth=16 --numjobs=4 --ioengine=io_uring --direct=1 --size=4G --runtime=60 --time_based --group_reporting --lat_percentiles=1 --filename=./t
cd ~ && rm -r ~/wp1-fio-tmp
```

## I. Additional verification required (prioritised)

| # | Test / action | Needs | Where |
|---|---|---|---|
| 1 | NVMe SMART health + canonical `fio` (above) | sudo approval to install tools | this machine |
| 2 | Owner: intended topology, hosts, GPU host identity (H-01, H-05) | owner decision | — |
| 3 | Owner: FP-05 decision among larger Qdrant memory limit / originals in RAM / different latency criterion / SSD index host; approve capacity target | owner decision | then re-run E-R1 here |
| 4 | Owner: workload + retention model (H-08) | owner decision | then local measurement |
| 5 | Controlled separation of NVMe write-cache exhaustion vs thermal throttling | #1 | this machine |
| 6 | Scylla RF = 3 / quorum / node loss; multi-host contracts; 168 h soak preflight (H-03, H-04, H-09) | three+ physical hosts | other hardware |
| 7 | Local rehearsals worth considering: longer (≥ 1 h) P2 10× run and E-R1 at 2/3 copies | owner approval, thermal plan | this machine |

## J. Git summary

Initial: crawler2 `main` @ `14f69ed8…` (2 pre-existing modified files, left untouched); fingerprinter `main` @ `ab9c89ea…` (untracked `var/`, left untouched). WP1 adds files only; no push, no history rewrite. See the commits in each repository (`git log -1`) for the final hashes.
