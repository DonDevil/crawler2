# WP1 Run 2 — storage and retention capacity (workstream G)

Date: 2026-11-18. Machine-readable: `capacity-model.json`. Raw: `../raw/storage/` (`df-*.txt`, `docker-system-df-v-*.txt`, `du-project.txt`).
Local model only. **No production retention period, workload volume or growth model exists in the register (H-08 is an owner decision), so none is invented here.** No project data, Docker volume, cache or dataset was deleted or pruned.

## Current usage (settled, end of Run 2)

| Item | Value | Change vs Run 1 end |
|---|---|---|
| Root `/dev/nvme0n1p7` ext4 | 147 GiB total, 82 GiB used, **58 GiB free** (59 %; snapshot 20:12 after all Run 2 tests, with the repeat E-R1 container running) | 59.2 → ≈ 58 GiB (the extra ≈ 1.5 GiB is Run 2 raw evidence, the P2 result dir and a still-running benchmark volume) |
| Usable above the 20 GiB floor | **≈ 38 GiB** | — |
| Docker volumes | MinIO 3.649 GB, Scylla 1.118 GB, dev Qdrant 0.908 GB, Redis 44.8 MB, scratch 0 | MinIO/Qdrant/Redis unchanged; Scylla −0.10 GB (commitlog churn) |
| Docker images / build cache | 2.246 GB / 1.15 GB (0.42 GB reclaimable, **not pruned**) | unchanged |
| `~/anti_piracy/datasets` / `fingerprinter` / `crawler` / `crawler2` | 27.9 GB / 5.8 GB / 0.60 GB / 0.40 GB (decimal) | ≈ unchanged |
| `~/.cache` | 7.8 GB | — |
| WP1 Run 2 raw evidence (before compression) | ≈ 12 MB | new |

## Measured growth and temporary-space figures

| Workload | Space | Source |
|---|---|---|
| Qdrant volume per resident point | **5.5–7.4 KB/point for ≥ 2 copies** (2×: 7.4 → 6.0 → 6.7; 3×: 5.8 → 5.5 → 5.8 KB across build / rebuild peak / after promote); Run 1's 1-copy value 11.2 KB/point is a small-N outlier | `../raw/qdrant/e-r1-{2,3}copies.json` (`du_bytes`) |
| Qdrant rebuild peak vs one generation | **≈ 1.95×** (3×: 3.65 GB peak vs 1.87 GB single generation) | same |
| Peak Qdrant volume | 1×: 1.63 GB, 2×: 2.69 GB, 3×: 3.65 GB | same |
| Arithmetic at the *unapproved* provisional `index.max_points` = 250,000 | resident ≈ 532,500 during a rebuild ⇒ **≈ 2.9–3.9 GB peak** | not a forecast of required capacity |
| MinIO small objects | ≈ 14 KB overhead per 32 KiB object (Run 1); P2 re-run wrote 12,000 objects, transient +0.4 GB while MinIO drained trash after deletion | Run 1 / `docker-database.md` |
| Benchmarks in this run | fio 4 GiB file; sustained-write 24 GiB file (both created and removed by the scripts) | `scripts/` |

## Risks and consequences of the 20 GiB floor

* Usable headroom is **39.4 GiB**. The largest bounded operation of this run (24 GiB sustained-write file) used 61 % of it; two such operations at once would breach the floor.
* A Qdrant rebuild at the provisional cap needs ≈ 3–4 GB of volume; it is not the storage risk. Simultaneous *rebuild + ingestion + a large benchmark file* is: ingestion growth is unknown (no workload model), so no safe concurrency can be stated yet.
* The local 147 GB partition shared with Windows is a development arrangement; nothing here is a production capacity statement.

## Automation (what exists now)

* **Reusable preflight:** `scripts/wp1r2_hostguard.py storage --planned-gib N` (also part of `preflight`): `FAIL` (exit 2, "REFUSE operation") if free − planned < 20 GiB, `WARN` below 30 GiB, `UNKNOWN` if the filesystem cannot be read. Tested only with simulated capacity values (`tests/unit/test_wp1r2_hostguard.py`: 60/0, 60/35, 60/40, 60/40.1, 21/0, 19.9/0 GiB; the CLI refusal path with `--planned-gib 100000`); the real disk was never filled.
* The Run 2 scripts also enforce their own floor-plus-margin (fio suite and sustained-write ≥ 28 GiB free, Qdrant runs ≥ 30 GiB at start and ≥ 24 GiB during).
* **Not implemented:** persistent space reservations, storage-growth time series, alerting before exhaustion. A minimal path: append the JSON from `storage` to a dated file per run (already machine-readable) and compare successive reports; a scheduler/alert needs your approval (no service or schedule was created).

## Limitations

Single machine and snapshot; Docker volume contents are root-owned (sizes come from `docker system df -v`); no per-bucket MinIO accounting was run; Redis growth beyond AOF size was not studied; Qdrant per-point volume comes from rotated copies of one dataset.
