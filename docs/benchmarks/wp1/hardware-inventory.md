# WP1 — Stage A: hardware, storage, service and environment inventory

Measured 2026-11-17 (IST) on the Ubuntu NVMe installation. **[M]** = measured/read during WP1,
**[A]** = value taken from the task's initial audit and not re-measured, **[I]** = inference (stated as such).

## Hardware

| Item | Value | Source |
|---|---|---|
| Machine | ASUS TUF Gaming F15 FX506HF, on AC power during all runs | [A]; AC `online=1` [M] |
| CPU | Intel Core i5-11400H, 6 C / 12 T, governor `powersave` (not changed) | [M] `/proc/cpuinfo`, sysfs |
| RAM / swap | 15,725 MiB total; swap 4 GiB (0 used at rest) | [M] `free` |
| Available RAM at idle (stack running) | ≈ 10.9–11.3 GiB | [M] monitor idle baseline |
| NVMe | SAMSUNG MZVLQ512HBLU-00B00, firmware `FXM7501Q`, 476.9 GB, PCIe 8 GT/s ×4 (Gen3 ×4), non-rotational, scheduler `none` | [M] sysfs / `lsblk` |
| NVMe temperature limits (hwmon) | high 82.8 °C, crit 84.8 °C | [M] `sensors` |
| CPU temperature limits (hwmon) | high 100 °C, crit 100 °C | [M] `sensors` |
| GPU | NVIDIA GeForce RTX 2050, 4,096 MiB, driver 615.71.09, compute capability 8.6 | [M] `nvidia-smi`, torch |
| Network | Wi-Fi only (`wlp45s0`); no second machine reachable or inventoried | [A] |

## Storage layout [M unless noted]

`nvme0n1` = p1 EFI vfat · p2 16 M · **p3 NTFS 301 GB** · p4 NTFS 1.1 GB · **p5 NTFS 24 GB** · p6 vfat · **p7 ext4 150 GB → `/`**.
Windows partitions share the physical drive; none were touched, mounted or read. Root: 147 G (157,396,975,616 B), 80 G used,
**59.2 GiB available at the end of WP1** (59.8 GiB at start; minimum 53.3 GiB, during the E-R1 6-copy run).
No other block devices are present (the former USB HDD is not attached).

## Docker storage [M]

Docker root `/var/lib/docker` is on `/` (NVMe) [A]. Seven volumes (all pre-existing, none removed): see `storage-capacity.md` for sizes.

## Service topology (single host, Docker Compose project `crawler2`) [M]

| Service (container) | Image | Limits | Notes |
|---|---|---|---|
| Scylla (`crawler2-scylla-1`) | scylladb/scylla:6.2.3 | 2 GiB, 2 CPUs | **single node**, `--smp 2 --memory 1400M --reserve-memory 512M --overprovisioned 1 --developer-mode 1`; `enable_tablets: true`; `commitlog_sync: periodic` (10 s); **all 24 keyspaces RF=1** (SimpleStrategy / NetworkTopologyStrategy `datacenter1: 1`) |
| Redis (`crawler2-redis-1`) | redis:7.4.2-alpine | 1 GiB, 1 CPU | project instance on host port **16379** (distinct from a system Redis on 6379, which was not touched); AOF on (`everysec`), `maxmemory 768M`, `noeviction`; 18 keys in db0, 1.17 MB used |
| MinIO (`crawler2-minio-1`) | pgsty/minio RELEASE.2026-08-04 | 512 MiB, 1 CPU | single node; 21 buckets, 3.65 GB |
| Qdrant (`fingerprinter-index-qdrant-1`) | qdrant 1.15.1 (digest `d122138f…`) | 1.5 GiB, 2 CPUs | dev instance, 908 MB volume |
| App (`crawler2-app-1`) | localhost/crawler2-app:dev | 512 MiB, 1 CPU | `crawler2-check` then `sleep infinity`; its health-check runs every 30 s (≈ 60–80 % CPU spikes of one core) |

All five were healthy with **0 restarts and no OOM kills** at the start and at the end of WP1. Ports are bound to 127.0.0.1.

## Python environments

| Repo | Interpreter | Notes |
|---|---|---|
| fingerprinter | `fingerprinter/env/bin/python` 3.12.3 | torch `2.13.0+cu130` (CUDA 13.0, `cuda.is_available()` = True), transformers 5.15.1, numpy 2.5.2, redis 8.1.0; **qdrant-client not installed** (the E-R1 harness uses its own isolated venv `datasets/p10/venv`) |
| crawler2 | `crawler2/env/bin/python` | cassandra-driver 3.29.12 |

Observation: `importlib.metadata` reports torch **2.14.1** while `torch.__version__` is **2.13.0+cu130**. The cause was not
investigated (two dist-info trees are the likely explanation [I]); the benchmark JSONs record the runtime value `2.13.0+cu130`.

DINOv2 (`facebook/dinov2-base`) weights are cached locally (`~/.cache/huggingface/hub/models--facebook--dinov2-base`, 346 MB
`model.safetensors`); `du` reports ~40 KB because the blobs are symlinks. Offline loading was verified (see `fingerprinter-baseline.md`).

## Git state (before WP1)

| Repo | Branch | HEAD | Tracked modifications | Untracked | Staged |
|---|---|---|---|---|---|
| crawler2 | `main` | `14f69ed88355c136a975bcda7198304db2a738e5` | `docs/development.md`, `tests/integration/crawlers/conftest.py` (69 + / 11 −; P4 per-session throwaway keyspace/bucket work — **not WP1, left untouched**) | none | none |
| fingerprinter | `main` | `ab9c89eafffcbaeda61792892646b75f65c3246d` | none | `var/` (pre-existing) | none |

## Tools

Present: `iostat`, `mpstat`, `pidstat`, `vmstat`, `sensors`, `nvidia-smi`, `docker`, `redis-cli`, `python3`.
**Missing: `fio`, `smartctl`, `nvme-cli`.** `sudo` requires a password and `/dev/nvme0` is `root:root 0600`, so nothing could be
installed and NVMe SMART/health data could not be read. See `nvme-benchmark.md` for the consequences.

## Checkpoint — Stage A: **COMPLETED** (with two evidence gaps: no NVMe SMART data, no `fio`).
