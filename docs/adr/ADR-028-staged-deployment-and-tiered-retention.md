# ADR-028 — Staged deployment, flexible GPU hosting and tiered retention

Status: Accepted (owner decisions, 2026-11-22). Scope: crawler2 and the fingerprinter (one infrastructure policy).
Blocker register: [wp1/blockers.md](../benchmarks/wp1/blockers.md) (H-01, H-05, H-08).

## Decisions

1. **Staged deployment (H-01, option E).** The system is developed and qualified on the local machine first; further hosts are
   added when measured workload justifies them. No hardware is bought or fixed ahead of need, and the architecture is not tied
   to one hardware configuration. ADR-006 (host roles by configuration, no authoritative local state) is what makes this work.
   - *Current development topology (single host):* ASUS TUF A15 — Ryzen 7 7435HS (8C/16T), ~15.4 GiB RAM, RTX 3050 Laptop 4 GiB,
     one WD PC SN740 512 GB NVMe (root `p7`, `/mnt/data` `p9`, same physical device), one USB-attached Toshiba MK3276GSX HDD
     (`/media/dhanush/Media`); all services in Docker on that host. The previously qualified ASUS TUF F15 (i5-11400H, RTX 2050,
     Samsung MZVLQ512HBLU) is a separate machine with a separate qualification record.
   - *Future production topology:* **not defined.** It is defined when expansion is needed. Multi-host Scylla RF=3 (H-03),
     physical multi-host operation (H-04), Redis HA (H-06), MinIO durability (H-07) and network isolation (H-10) stay open.
     This ADR does not claim multi-host production readiness.
2. **GPU hosting is flexible (H-05).** No machine permanently owns GPU inference. The `encoder` role (ADR-006) is assigned to
   whichever host currently has a qualified GPU (`limits.gpu_vram_mb > 0`). A host is "qualified" only after the encoder GPU
   benchmarks pass on it (see the A15 record); the encoder and spec runtime profile (rt-1) must not depend on a host name.
3. **Tiered retention (H-08, option C).** Data is placed by value, access frequency and retention:

   | Data category | Tier |
   |---|---|
   | Recent operational data (Scylla, Redis state) | SSD/NVMe |
   | Active indexes and frequently accessed data (Qdrant generations, recent snapshots) | SSD/NVMe |
   | Older snapshots | HDD / bulk |
   | Bulk historical evidence | HDD / bulk |
   | Important evidence needing longer retention | separately retained storage |
   | Temporary files, transient logs | explicit retention policy |

   This is an accepted *strategy*, not a capacity calculation. **No production capacity figure exists**; it needs a workload and
   retention model (pages/day, media/day, embeddings/day, retention days, safety factor) that the owner has not yet supplied.
   The model skeleton and measured unit sizes are in `benchmarks/p2-storage/capacity_model.py`.

## Implementation status (what exists in code today)

- Object-store layout (ADR-014) already separates raw buckets from the write-once evidence bucket (ADR-003), so a bucket can be
  placed on a different tier without a key change. **No automatic tier migration or lifecycle job exists**; moving data between
  tiers is not implemented. Retention rules for raw objects remain disabled until ADR-005 defines retention (ADR-014).
- Qdrant is derived, rebuildable state (the fingerprinter's own ADR-028, a different numbering sequence) and lives on SSD/NVMe.

## Consequences / remaining work

Workload and retention model → H-08 closure; tier-migration mechanism and HDD qualification under a real workload (the HDD
measured so far is a USB 2.5" drive, file-based fio only); multi-host topology when expansion is triggered.
