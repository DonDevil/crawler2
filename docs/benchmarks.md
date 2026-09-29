# Benchmarks

Index of every benchmark with its headline result. Scripts and committed
raw results live under `benchmarks/<name>/`; methodology and full tables
are in the linked phase documents. How to run them:
[development.md](development.md#benchmarks).

| Benchmark | Phase | Headline | Gate | Details |
|---|---|---|---|---|
| V1 baseline crawl | P0 | 1.02 pages/s per host (politeness-bound) | reference | [p00 baseline](phases/p00-foundations/baseline.md), `benchmarks/v1-baseline/` |
| Scylla partitions, 1M pages | P2 | largest partition 43.4 MB (< 100 MB) | ✅ | [p02 benchmarks §1](phases/p02-storage/benchmarks.md) |
| Storage latency at 10× per-host load | P2 | not met on the dev host (USB HDD saturated) | ⏳ **open — environment** | [p02 benchmarks §2](phases/p02-storage/benchmarks.md) |
| Frontier throughput, 8 workers, no rate limit | P3 | V2 13.2–13.7k claims/s vs V1 11.3–11.5k on the same Redis (+18 %); compose Redis 9.3k vs 8.4k | ✅ | [p3 §22](phases/p03-frontier-scheduling/p3-frontier-scheduling.md#22-results) |
| 1M-claim distributed run with kills/pauses | P3 | 0 lost, 0 duplicate completions, 0 simultaneous ownership; 69 legitimate reclaims | ✅ | same |
| Eligible-domain index vs `domain_scan_limit` | P3 | V2 finds the eligible domain behind up to 20 000 gated ones at a flat ~32 µs; V1 (K=250) finds none beyond 250 at ~800 µs | decision: index | same |
| Starvation (7 V1 scenarios + cross-queue) | P3 | all pass; cross-queue starvation found and fixed (turn-taking) | ✅ | same |
| Crash recovery / heartbeat endurance / priority × rate limit | P3 | reclaim 2.5 s after kill (2 s lease); 0/200 heartbeated claims lost; per-domain gaps ≥ interval across queues | ✅ | same |

## Rules that keep numbers comparable

- A throughput number is meaningless without the worker count, whether
  politeness was on, the domain count and the Redis configuration; every
  P3 result JSON records them (`environment`, `summary`).
- V1 comparisons run **V1's own scripts** against the **same Redis in the
  same session** (V1 read-only, `PYTHONDONTWRITEBYTECODE=1`); historical
  V1 numbers are quoted only as context.
- Redis CPU is a time-normalised delta of `used_cpu_sys + used_cpu_user`,
  never a raw cumulative counter (V1 audit lesson).
- Benchmark definitions are not changed to make a result pass. Where a V1
  benchmark was flawed (V1's `scan-limit-window` ran without politeness
  and so could not isolate the K window), the correction and its reason
  are documented next to the result.

## Development-environment caveat

The development machine runs Ubuntu from an external 5 400-rpm USB HDD;
its internal NVMe (Windows) is intentionally not modified. Disk-bound
benchmarks (P2 storage latency) are therefore not representative and the
P2 10× gate stays open until re-run under WSL/NVMe or another high-IOPS
environment. The P3 frontier benchmarks are Redis-CPU-bound and not
affected by the disk.
