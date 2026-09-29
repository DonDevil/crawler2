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
| V1 engines on the 51 P0 seeds (per engine) | P4 | httpx 50 %, aiohttp 42 %, Playwright 56 % (276 KB/page), Selenium 65 % → 62 % status-corrected (467 KB, 1 process/page), Scrapling 50 % (797 KB, 3.8 GB RSS) | decision: httpx + Playwright (ADR-018) | [p4 §28](phases/p04-fetch-workers/p4-fetch-layer-worker-pools.md#28-seed-set-evaluation-exit-gates), `benchmarks/p4-fetch/` |
| **P4 exit gate**, 691 P0-run URLs, V1 hybrid vs V2, same session | P4 | success 89.9 % vs V1 97.3 % ❌; bytes/page 41.9 KB vs 175.3 KB ✅; browser share 0.3 % vs 7.4 % ✅; media body downloads 0 ✅ | ❌ **success gate open** | [p4 §30–31](phases/p04-fetch-workers/p4-fetch-layer-worker-pools.md#30-results) |
| P4 exit gate **rerun** after the P3 in-flight limit (ADR-019), same W691/definitions | P4 | success 95.51 % vs V1 96.82 % ❌ (V1 `visited` includes 10 Selenium 400/403/404 false successes; informational status-aware view V1 95.37 % vs V2 95.51 % — not a gate result); bytes/page 40.2 vs 204.1 KB ✅; browser share 0.3 % vs 8.1 % ✅; media 0 ✅ | ❌ **success gate open** | [p4 §30a–31](phases/p04-fetch-workers/p4-fetch-layer-worker-pools.md#30a-root-cause-of-the-899--run-and-the-rerun-after-the-p3-correction) |
| Frontier in-flight limit experiment (1/2/4/unlimited) on W691 | P3 | timeout attempts 8 / 8 / 21 / 71; wall 111 / 80 / 64 / 67 s → default 2 | decision: 2, frozen (ADR-019) | [p3 §26](phases/p03-frontier-scheduling/p3-frontier-scheduling.md#26-correction-after-p4-per-domain-in-flight-limit-adr-019) |
| P3 suite rerun with the limit | P3 | 1M chaos 0 lost/0 dup; starvation all pass; throughput 8 w V2 11.9–12.4k vs V1 11.2k same session (A/B limit cost −2.4 %) | ✅ relative (absolute 13k not re-attained this session even with the limit off) | same |
| **P5 parse CPU/page**, 639 W691 HTML pages, V1 `extract_content` vs V2 `extract` (full pipeline), same bytes/session, 3 rounds | P5 | V2 9.5 ms vs V1 53.2 ms aggregate → **17.9 % of V1**; no page slower than V1 (worst ratio 0.69) | ✅ (≤ 50 %) | [p5 benchmarks](phases/p05-extraction-page-intelligence/benchmarks.md), `benchmarks/p5-extraction/` |
| P5 change-detection precision, 53 hand-labeled real page pairs (3 h apart) | P5 | precision 1/23 (22 FPs = one site's in-content rotating widget); 30/30 rule-negatives correct; 452/475 byte changes suppressed | checked (no numeric threshold in the plan) — limitation → P6/P7 | [p5 validation §4](phases/p05-extraction-page-intelligence/validation.md#4-change-detection-evaluation) |
| **P6 filter throughput**, 106,098 rules (built_in, V1, EasyList, EasyPrivacy), 60,086 W691 links/sub-resources, no cache | P6 | **128.0k decisions/s** warm; 81.7k/s URL→decision; 71.6k/s cold; p99 31 µs; compile 3.9 s, +262 MB RSS | ✅ (≥ 100k) | [p6 benchmarks §1](phases/p06-filter-discovery/benchmarks.md), `benchmarks/p6-filter/` |
| P6 false-positive guard, 346 labelled items (185 synthetic) | P6 | 0 protected items blocked by generic rules (2 before the ABP document-semantics fix); AD P/R 0.912/0.886, TRACKER 0.867/0.963 | ✅ | [p6 benchmarks §2](phases/p06-filter-discovery/benchmarks.md) |
| Browser pool leak, 1 000 pages | P4 | RSS 601–714 MB, recycle every 50 pages/context and 500/browser, second browser ≤ 1.10× first | ✅ | same |
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
- Fetch benchmarks count bytes on the wire through one counting proxy for
  every engine of both systems (`benchmarks/p4-fetch/countproxy.py`); live-web
  results are compared only within one session.
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
