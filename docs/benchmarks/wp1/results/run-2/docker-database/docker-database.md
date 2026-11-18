# WP1 Run 2 — Docker and database resource behaviour (workstream F)

Date: 2026-11-18. Raw: `../raw/docker-database/` (`container-limits.txt`, `port-bindings.txt`, `p2-cgroup.jsonl` 1 Hz cgroup memory, `p2-monitor.jsonl`, `containers-before-p2.txt`), P2 result `benchmarks/p2-storage/results/20261118T044407Z/`.
Single Scylla node, RF = 1; nothing here says anything about replication, quorum or multi-host behaviour.

## Inventory (read-only)

| Container | Memory limit (+swap) | CPUs | Restarts | OOMKilled | Health | Ports (all loopback) |
|---|---|---|---|---|---|---|
| crawler2-scylla-1 | 2 GiB (+2 GiB swap) | 2 | 0 | false | healthy | 127.0.0.1:19042 (others unpublished) |
| crawler2-minio-1 | **512 MiB (+512 MiB swap)** | 1 | 0 | false | healthy | 127.0.0.1:19000/19001 |
| crawler2-redis-1 | 1 GiB (+1 GiB swap) | 1 | 0 | false | healthy | 127.0.0.1:16379 |
| crawler2-app-1 | 512 MiB (+512 MiB swap) | 1 | 0 | false | healthy | none |
| fingerprinter-index-qdrant-1 | 1.5 GiB (no swap) | 2 | 0 | false | healthy | 127.0.0.1:16333/16334 |

All started 2026-11-18T02:08:32Z (stack brought up before this run). Host: 15.4 GiB RAM, 4 GiB swap file (18.9 MiB used); the separate system Redis on 6379 was not touched. No existing container was restarted or recreated: Scylla/MinIO start times and restart counts are identical before and after the benchmark. Sum of container limits 5.5 GiB of 15.4 GiB RAM.

## Scylla — is the local P2 result reproducible? **Yes (third time).**

`COMPOSE_PROFILES=two-host RATE=100 DURATION=60 benchmarks/p2-storage/run.sh latency` (two idle app-host containers started with `--no-deps --no-build`, two client containers of 4 CPUs each), keyspace `crawler2_bench_latency` and bucket `crawler2-bench` (both verified absent beforehand).

| | Run 1 (2 runs) | **Run 2** |
|---|---|---|
| gate: 100 units/s/host, every per-op p99 target met, publication lag p99 ≤ 5 s | met, met | **met** — 16/16 targets, 99.9–100.0 units/s, **0 failed units** |
| tightest p99 vs target | links_record 22–24 ms vs 150 | links_record 21.7 ms vs 150; point reads ≈ 8–11 ms vs 25 |
| publication lag p50 / p99 | 0.97 / 2.56 s, 0.96 / 2.54 s | **0.96 / 2.54 s** |
| NVMe max util / write await | 29.1 % / 1.7 ms, 28.9 % / 1.7 ms | **27.3 % / 2.34 ms** (iostat, corrected column check) |
| Scylla memory | docker stats 1.35–1.36 GiB | cgroup **anon peak 1,219 MiB, file 207 MiB, current peak 1,466 MiB (72 % of 2 GiB)**, `memory.max` events 0, OOM 0 |

Scylla's memory is essentially flat (the Seastar allocation, 1.4 GB configured), with 28 % headroom under the container limit. Compaction/flush metrics and anything longer than 60 s were **not** measured (`nodetool` cannot start in the container because of host `aio-max-nr`; no full compaction or rebuild was attempted, per the safety rules).
This is still only the *local single-node RF = 1* gate; it does not prove RF = 3 or multi-host production latency.

## MinIO 512 MiB — is the limit a problem? (measured with cgroup `anon`/`file`, which `docker stats` cannot separate)

During the P2 10× load (100 PUT/GET-class units/s/host, 32 KiB objects, 12,000 objects written):

| | Value |
|---|---|
| anonymous memory (Go heap + buffers) peak | **284 MiB = 55 % of the limit** (idle ≈ 121 MiB) |
| page cache (`file`) peak | 215 MiB |
| `memory.current` / `memory.peak` | **512 MiB = the limit** (this is what `docker stats` showed) |
| kernel reclaim events at the limit (`memory.events max`) | **6,616**, OOM kills 0, restarts 0 |
| cgroup swap peak | 50 MiB (18 MiB resident afterwards) — cold pages only; host PSI memory 0.0, host swap ≤ 17 MiB, host memory available ≥ 8.7 GiB |
| operations during that time | all object_put/head/get p99 targets met (12.3 / 3.2–3.7 / 5.1–8.5 ms) |

**Conclusion.** The "MinIO reached 512 MiB" observation is **mostly reclaimable page cache hitting the cgroup limit**, not exhausted heap: anonymous usage was 55 % of the limit and no OOM occurred. For this workload (32 KiB objects, 200 units/s in total, each unit including object operations) the limit is sufficient. It is **not** shown to be sufficient for other workloads: MinIO's anonymous memory grows with concurrent request size and count, and no production object-size/concurrency model exists (H-08, owner input). The limit was **not** changed.
Risks worth acting on later: (a) page cache thrashing against the limit and ≈ 50 MiB of swap-out can add latency under larger working sets; (b) the right alert is on cgroup **anon** (> 80 % of limit ≈ 410 MiB) and on `memory.events max`/`oom_kill`, not on `docker stats` memory. If the owner supplies object sizes, set the budget from them; the host could afford 1 GiB (11 GiB available) but there is no evidence yet that justifies it.

## Redis, Qdrant (dev instance)

Redis: 19.6 MiB used (cgroup anon peak 126 MiB during the run, 12 % of limit), 0 restarts; volume 44.8 MB (AOF) — unchanged from Run 1 (44.4 → 48.6 MB). The relay benchmark traffic is the only write source; no separate Redis write benchmark was run. Dev Qdrant: 111 MiB of 1.5 GiB, volume 908 MB, unchanged.

## Resource pressure and disk

Host PSI memory/IO stayed 0.0 / low during the P2 run; free memory ≥ 8.7 GiB; root filesystem free space 59.4 GiB after the benchmark. Volumes after settling: MinIO 3.649 GB, Scylla 1.118 GB, Qdrant 908 MB, Redis 44.8 MB — **identical to Run 1 for MinIO/Qdrant/Redis**; Scylla 1.219 → 1.118 GB (commitlog churn). A snapshot taken minutes after the benchmark transiently showed MinIO 4.05 GB (internal trash draining after deleting 12,000 objects), which settled back (`../raw/storage/docker-system-df-v-transient-minutes-after-p2.txt` vs `…-settled.txt`).

## Cleanup (verified; only benchmark-created resources)

* Containers `crawler2-app-host-1-1`, `crawler2-app-host-2-1` removed (`docker compose rm -sf`; volumes kept, 0 B).
* Keyspace `crawler2_bench_latency` dropped; the 43 automatic `pre-drop` snapshot tags created for it were deleted through the Scylla REST API (`kn=crawler2_bench_latency`); **560 older snapshot tags left in place** (verified 560 before and after).
* Bucket `crawler2-bench` (12,000 objects) deleted via `scripts/wp1_cleanup_bench_bucket.py` (HTTP 204).
* Qdrant benchmark containers/volumes `wp1r2-*` removed. No project keyspace, bucket, collection, Redis key, volume or the system Redis was altered.

## Not done / limitations

No backpressure mechanism was implemented: no pressure condition was reproduced (no OOM, no Scylla pressure, MinIO anon at 55 %), and inventing a threshold without a reproduced failure would be speculative. Compaction/rebuild workloads, RF > 1, runs longer than 60 s, and MinIO behaviour with large objects were not tested.
