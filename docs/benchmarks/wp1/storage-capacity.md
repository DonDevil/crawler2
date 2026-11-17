# WP1 — storage capacity (local machine only)

Measured 2026-11-17 at the end of WP1 unless stated. Sizes from `df`, `docker system df -v`, `du` (container-side for Scylla/MinIO because `/var/lib/docker` is not readable without root).

## Filesystem

| Item | Value |
|---|---|
| Root `/dev/nvme0n1p7` ext4 | 157,396,975,616 B (146.6 GiB) |
| Used | ≈ 80 GB (df rounds to 80 G, 58 %) |
| **Available** | **63,604,641,792 B = 59.2 GiB** (59.8 GiB at the start of WP1; minimum 53.3 GiB during the 6-copy Qdrant run) |
| WP1 safety floor (task rule, not a production threshold) | 20 GiB free → **≈ 39 GiB usable for further local testing/growth** |

The other NVMe partitions (Windows NTFS 301 GB + 24 GB + 1.1 GB) are not available to Ubuntu and were not touched.

## Docker (all on the root filesystem)

| Object | Size | Note |
|---|---|---|
| Images (6) | 2.246 GB | all in use |
| Containers (7, 5 running) | 11 MB | |
| Build cache (21) | 1.15 GB | 420 MB reclaimable; **not pruned** (prune is forbidden in WP1) |
| Volumes (7) | 5.83 GB | table below |

| Volume | Start of WP1 | End of WP1 | Notes |
|---|---|---|---|
| `crawler2_minio-data` | 3.649 GB | 3.649 GB | 21 buckets; `fingerprinter-targets` and `fp-blast20261116-targets` ≈ 1.7 GB each; benchmark bucket added and removed (the volume transiently showed 4.49 GB while MinIO drained its internal trash) |
| `crawler2_scylla-data` | 0.612 GB | 1.219 GB | data 145 MB + commitlog **1.1 GB** (commitlog segments Scylla recycles itself; growth is from the P2 benchmark writes, not retained data). 560 older `pre-drop-*` snapshots from earlier test keyspaces remain (each ≤ 0.4 MB) |
| `fingerprinter-index-qdrant-data` | 0.908 GB | 0.908 GB | dev index, unchanged |
| `crawler2_redis-data` | 44.4 MB | 48.6 MB | AOF growth |
| `crawler2_scratch-dev-1`, `-host-1`, `-host-2` | 0 | 0 | |

## Repositories and local artefacts

| Path | Size |
|---|---|
| `~/anti_piracy/datasets` | 26 GB (largest consumer outside Docker) |
| `~/anti_piracy/fingerprinter` | 5.6 GB (incl. `env/`, caches) |
| `~/anti_piracy/crawler` | 542 MB |
| `~/anti_piracy/crawler2` | 409 MB |
| `artifacts`, `monitoring_runs`, `var-*` | 27 MB, 31 MB, ≈ 5 MB |
| `~/.cache` (of which `huggingface` 331 MB) | 8.3 GB |
| WP1 raw evidence (compressed) | < 1 MB |

## What the evidence supports for projection (and what it does not)

All inputs are from this machine and these benchmark datasets; **no production retention, workload or growth model exists in the register, so none is invented here.**

| Quantity | Measured input | Derived rate | Uncertainty |
|---|---|---|---|
| Qdrant volume per resident point (1.5 GiB-cap instance, int8 + on-disk originals, 768-d) | 6 copies: 3,334,868,386 B for 636,658 points (g1 + 10,000 media) | ≈ 5.2 KB/point | **the 1-copy runs give ≈ 11.2 KB/point (1,284,671,193 B / 114,443)** and ≈ 7.0 KB/point at the rebuild peak (1.63 GB / 234,222 points), so per-point cost is not constant; any extrapolation carries at least a ×2 range |
| Qdrant peak during a rebuild (two generations) | 1 copy: 1.63 GB at 234,222 resident points (≈ 1.7× the post-promote 0.94 GB) | — | single scale only |
| MinIO small-object overhead | benchmark: 25,200 objects of 32 KiB (≈ 0.83 GB payload) occupied ≈ 1.1 GiB on disk | ≈ 14 KB/object overhead (≈ 1.4×) | `du -sh` rounding; one run |
| Scylla | volume 612 MB → 1.22 GB, but growth was commitlog churn; table data after the P2 benchmark was 145 MB (benchmark keyspace dropped) | no per-row figure is supportable | — |

Example of a *bounded arithmetic statement, not a forecast*: at the provisional `index.max_points = 250,000` the per-generation Qdrant volume would be roughly 1.3–2.8 GB (5.2–11.2 KB/point), and about double that while a second generation exists (≈ 2.6–5.6 GB) — comfortably inside the ≈ 39 GiB local headroom, **but this says nothing about how many points the product must hold**, which is the unapproved target.

## Capacity conclusions (local)

- The machine has **≈ 59 GiB free (≈ 39 GiB above the WP1 floor)** and the currently running stack (Docker volumes ≈ 5.8 GB) uses well under 10 % of the root partition; local engineering and validation fit.
- The largest storage consumers are not the databases but datasets (26 GB) and Python environments/caches.
- Production capacity (page snapshots, media, embeddings, indexes, logs, evidence, retention) **cannot be derived**: the register's H-08 needs expected data volume and retention period from the owner. H-08 stays open.
- A single 147 GB Ubuntu partition shared with a Windows installation on one NVMe is a development arrangement, not a production capacity statement.
