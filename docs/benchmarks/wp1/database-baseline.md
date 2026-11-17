# WP1 — Stage D: database and index baseline

**Scope statement.** Everything here is **one Scylla node, RF = 1** (all 24 keyspaces), one Redis, one MinIO, one Qdrant, on one laptop.
It measures local service behaviour on NVMe. It says **nothing** about replication, quorum, node loss, cross-host networking or RF = 3.

## Safety design

| Service | Isolation used | Existing data touched? |
|---|---|---|
| Scylla + MinIO + Redis relay | the repository's own P2 benchmark (`benchmarks/p2-storage/run.sh latency`), which creates keyspace `crawler2_bench_latency` and bucket `crawler2-bench` — **neither existed before** (checked) | No |
| Qdrant | the repository's own E-R1 harness against a **dedicated container `qdrant-e-r1` + volume `wp1-qdrant-e-r1`** (same image digest and caps as the dev index, fresh throw-away API key); the dev instance (`fingerprinter-index-qdrant-1`) was only read for inventory | No |
| Redis | read-only: `INFO`, `CONFIG GET`, `redis-cli --latency`; no writes, no benchmark on port 16379; the system Redis on 6379 was not touched | No |

A generic "temporary collection on the dev Qdrant" test was **not** run: the E-R1 harness is the project's approved FP-05 method and a separate container avoids sharing 1.5 GiB with live data.
To run the P2 benchmark, only `app-host-1` and `app-host-2` were started (`docker compose up -d --no-deps --no-build`, profile `two-host`; the two idle `sleep infinity` containers use volumes `crawler2_scratch-host-1/2`, both 0 B). No existing container was recreated or restarted (Scylla start time and restart count unchanged). The two containers were removed afterwards (`docker compose rm -sf`, volumes kept).

## ScyllaDB (single node, RF = 1) — P2 latency gate "p99 at 10× expected per-host load"

Command (all runs; 60 s, two simulated hosts at once, open-loop, client containers 4 CPUs each):
`COMPOSE_PROFILES=two-host RATE=<10|100> DURATION=60 benchmarks/p2-storage/run.sh latency`. Results: `benchmarks/p2-storage/results/20261117T133006Z` (1×), `…T133231Z` and `…T133518Z` (10×); logs in `raw/stageD-p2-latency-*.log`.
Gate definition (docs/phases/p02-storage/benchmarks.md §2): 100 units/s/host sustained with every per-operation p99 target met and outbox publication lag p99 ≤ 5 s.

| Operation | p99 target (ms) | 1× p50 / p99 (ms) | **10× run 1** p50 / p99 | **10× run 2** p50 / p99 | Met (3 runs) |
|---|---|---|---|---|---|
| latest_read | 25 | 0.9 / 1.63 | 0.94 / 9.91 | 0.97 / 9.02 | yes |
| url_state_read | 25 | 0.53 / 1.45 | 0.7 / 8.4 | 0.72 / 8.05 | yes |
| observation_get | 25 | 0.71 / 3.37 | 0.94 / 8.38 | 0.94 / 11.9 | yes |
| representation_lookup | 25 | 0.41 / 1.39 | 0.66 / 8.31 | 0.65 / 9.05 | yes |
| dedupe_check | 25 | 0.41 / 1.36 | 0.56 / 8.23 | 0.56 / 8.33 | yes |
| dedupe_mark | 25 | 0.28 / 1.33 | 0.41 / 7.04 | 0.41 / 6.24 | yes |
| outbox_append | 25 | 0.42 / 1.06 | 0.62 / 7.5 | 0.61 / 9.02 | yes |
| content_lookup | 50 | 1.59 / 3.34 | 2.43 / 13.81 | 2.44 / 11.51 | yes |
| discovered_record | 50 | 1.01 / 2.53 | 1.52 / 11.86 | 1.54 / 10.66 | yes |
| attempt_record | 50 | 1.23 / 3.18 | 1.59 / 16.32 | 1.63 / 14.57 | yes |
| observation_record | 100 | 2.0 / 4.85 | 2.8 / 20.68 | 2.83 / 19.91 | yes |
| media_record | 100 | 1.38 / 6.49 | 2.18 / 19.87 | 2.13 / 18.31 | yes |
| links_record | 150 | 3.35 / 6.56 | 5.1 / 23.99 | 5.14 / 22.18 | yes |
| object_put 32 KiB (MinIO) | 250 | 3.45 / 7.38 | 3.75 / 11.93 | 4.04 / 14.36 | yes |
| object_head (MinIO) | 50 | 0.79 / 1.77 | 0.9 / 3.47 | 0.97 / 5.74 | yes |
| object_get (MinIO) | 100 | 0.74 / 2.21 | 0.95 / 3.68 | 1.0 / 8.49 | yes |

(p99 shown is the worse of the two simulated hosts.)

| Run | Achieved units/s/host | Failed units | Schedule lag p99 | Publication lag p50 / p99 | NVMe max %util / w_await (iostat) | Scylla container peak |
|---|---|---|---|---|---|---|
| 1× | 10.1 / 10.0 | 0 | 0.4 ms | 0.55 / 1.11 s | 3.6 % / 3.3 ms | 22 % CPU (of 200 %), 0.74 GiB |
| 10× run 1 | 99.9–100.0 | 0 | 5.7–6.4 ms | 0.97 / 2.56 s | 29.1 % / 1.7 ms | 103 % CPU, 1.35 GiB |
| 10× run 2 | 99.9–100.0 | 0 | 5.4–6.4 ms | 0.96 / 2.54 s | 28.9 % / 1.7 ms | 114 % CPU, 1.36 GiB |

**Comparison with the HDD result is valid**: same script, same load definition, same duration (docs/phases/p02-storage/benchmarks.md): at 10× the HDD host achieved 25 units/s/host, with every p99 target missed, publication lag p99 237 s,
and `sda` at 99–100 % utilisation. On NVMe the same load is sustained, all 16 p99 targets pass in both repeats with ≥ 4× margin on the tightest (point-read p99 ≈ 9 ms vs 25 ms; links_record 24 ms vs 150 ms), the disk is at ≈ 29 %, and the host CPU (thermal) is now the visible constraint (`system-monitoring.md`).
MinIO ran at its 512 MiB memory limit during 10× without a restart. **Not measured:** compaction activity (`nodetool` cannot start in the container — host `aio-max-nr` limit; Scylla REST metrics were not collected), single-node behaviour beyond 60 s, anything at RF > 1.
Cleanup (verified): keyspace `crawler2_bench_latency` dropped; its 118 automatic `pre-drop-*` snapshots (tags created today for this keyspace only) deleted through the Scylla REST API with `kn=crawler2_bench_latency`; the other 560 snapshots (earlier test keyspaces) were left in place; bucket `crawler2-bench` (25,200 objects, 1.1 GB) deleted object-by-object over S3 with `scripts/wp1_cleanup_bench_bucket.py` (the crawler2 object store has no delete API), bucket removed (HTTP 204).

## Redis (project instance, port 16379) — read-only

7.4.2; AOF enabled, `appendfsync everysec`, RDB `save 3600 1 300 100 60 10000`, last bgsave ok; `maxmemory 768M`, `noeviction`; 1.17 MB used (peak 2.18 MB), 1 client, db0 = 18 keys, AOF 44.4 MB; container 11–21 MiB RSS, ≤ 3.5 % CPU at rest and 34.7 % during the 10× relay load.
`redis-cli --latency` (≈ 8 s, PING, loopback): **min 0 / max 1 / avg 0.13 ms**. No write benchmark was run; Redis exercised only as the P2 outbox relay target (publication lag above). Persistence behaviour on loss of the primary (H-06) was not examined.

## MinIO — inventory plus P2 object traffic

Single node, 21 buckets, volume 3.649 GB (before and after; the benchmark bucket's data was removed, MinIO's internal trash drained). Largest: `fingerprinter-targets` and `fp-blast20261116-targets` ≈ 1.7 GB each. Per-bucket **object counts were not obtainable** (no `mc`; the file layout does not expose them through `find`); sizes are `du` of the bucket directories.
Object PUT 32 KiB p99 7.4 ms (1×) and 11.9–14.4 ms (10×) vs 423–630 ms / 2.2 s on the HDD.

## Qdrant — E-R1 capacity harness on a dedicated container

Harness: `benchmarks/p10-index/capacity_e_r1.py` (Qdrant 1.15.1, qdrant-client in the isolated `datasets/p10/venv`, gRPC batches of 1,000, 768-dim vectors from the 104,443 cached real `dinov2-seg/2` vectors, copies as seeded rotations), container limit **1.5 GiB RAM + 1.5 GiB memsw, 2 CPUs**, fresh volume per run, run alone apart from the idle stack.
Phases: build g1 → warm queries → build g2 while serving 10 q/s + live writes → promote → 5 min steady. Pass rules (all): no `oom_kill`; cgroup anon peak ≤ 1.35 GiB; query p95 ≤ 10 ms in phases 2, 3 and 5 after a 60 s warm-up; exact counts equal the harness total; phase 3 reaches R_req.
New result files (fingerprinter repo): `benchmarks/p10-index/results/e-r1-1copy-nvme.json`, `…-nvme-run2.json`, `e-r1-6copies-nvme.json` (copies in `raw/`); per-second memory samples are outside git (`datasets/p10/runs/e-r1/wp1-nvme-*.samples.jsonl`). **No historical result file was overwritten.**

| | HDD (historical files) | NVMe 1 copy run 1 | NVMe 1 copy run 2 | NVMe 6 copies |
|---|---|---|---|---|
| N / R_req | 104,443 / 234,109 (1 copy); 626,658 / 1,304,649 (6) | 104,443 / 234,109 | same | 626,658 / 1,304,649 |
| phase 1 ingest (index green) | 6 copies ≈ 40 min | 50.9 s (+6 s) | — (similar) | **327 s** (+16 s) |
| g2 build ingest (green) | 153 s (+15) | 55.1 s (+8 s) | 56.9 s (+8 s) | not reached |
| warm query p95 (phase 2) | 6 copies: p50 16.6 s / p95 23.2 s (probe) | n/a (phase 14 s, no post-warm-up samples — harness rule) | n/a | **p50 46 ms / p95 67 ms**, p99 75 ms (1,014 samples) |
| rebuild-phase p95 (phase 3) | 1 copy: **5.1 s** (p50 27.7 ms) | **23.3 ms** (p50 9.18, p99 37.8; n = 6,482) | 22.8 ms (p50 9.40, p99 43.0; n = 6,450) | 634 ms (p50 253 ms, p99 1,112 ms) until the container died |
| steady p95 (phase 5) | 1 copy: **154 ms** (p50 8.7 ms) | **10.111 ms** (p50 8.553, p99 10.80; n = 2,402) | **10.171 ms** (p50 8.693, p99 11.07; n = 2,402) | not reached |
| live-write p50 / p95 / p99 | 132 ms / 2.4 s / — | 10.2 / 35.6 / 67.7 ms | 11.0 / 33.9 / 72.5 ms | 29 / 238 / 611 ms |
| anon peak (rule ≤ 1.35 GiB) | 0.39 GiB (1 copy) | 0.437 GiB | 0.431 GiB | **1.439 GiB — rule failed** |
| `oom_kill` | 0 | 0 | 0 | **container OOM-killed (docker `OOMKilled=true`, exit 137) during rebuild; 1,265 client errors** |
| volume (g1 / peak / after promote) | 0.89 / 2.36 / 1.07 GB (1 copy); 3.34 GB (6, phase 1) | 1.28 / 1.63 / 0.94 GB | — | 3.33 GB after phase 1 |
| **Verdict** | FAIL | **FAIL** (steady p95 10.11 ms and rebuild p95 23.3 ms > 10 ms) | **FAIL** (10.17 ms; 22.8 ms) | **FAIL** (memory rule, incomplete, latency) |

Interpretation (measured, not assumed):

- **Storage was the dominant cause of the HDD failures**: ingest 3–7× faster, rebuild-phase p95 5.1 s → 23 ms, steady p95 154 ms → 10.1 ms, live-write p95 2.4 s → 35 ms; NVMe utilisation stayed ≤ 44 % in both 1-copy runs, with read await ≤ 8.3 ms at ≤ 4.2 k read IOPS. H-02's premise ("cannot be evaluated reliably on the HDD") is confirmed.
- **At 1 copy the remaining miss is small but reproducible** (steady p95 +1.1 % / +1.7 % over the 10 ms limit; the same runs' p50 is 8.5–8.7 ms, i.e. a per-query cost floor, not a disk tail) and **the rebuild phase misses by 2.3×** (22.8–23.3 ms). The gate was **not** modified or reinterpreted.
- **At 6 copies the limit is memory, not storage**: with g1 (626,658 points) resident and g2 building under the 1.5 GiB cap, anon memory reached 1.44 GiB and the kernel killed Qdrant. Warm queries were already 67 ms p95 (6.7× the limit) with the originals competing with ≈ 0.7 GiB of anon memory for the page cache. More IOPS does not fix a RAM budget.
- Because the 6-copy run died in phase 3, phase 4–5 data do not exist for it. The 2/3-copy scales were not run (the design's own reasoning, validation §13.5, says they cannot pass what 1 copy fails; the NVMe 1-copy result, being a near miss, was not re-examined for that argument — an owner decision).

CAP-8 consequence: `index.max_points` stays at the provisional 250,000; no passing, gate-eligible E-R1 report exists, so no larger value is supported by evidence.

Cleanup (verified): container `qdrant-e-r1` and volume `wp1-qdrant-e-r1` removed after each run (`docker ps -a | grep e-r1` = 0, `docker volume ls | grep wp1` = 0). Dev Qdrant volume unchanged at 908 MB.

## Checkpoint — Stage D: **COMPLETED** for Scylla (P2 gate), Redis (read-only), MinIO (inventory + P2 traffic), Qdrant (E-R1). **INCOMPLETE**: Scylla compaction/flush metrics, MinIO object counts, any generic/isolated Redis write benchmark, any run longer than 20 minutes.
