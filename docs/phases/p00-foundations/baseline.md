# P0 Baseline — V1 regression bar

One measured run of V1 on a fixed seed set. V2 must meet or beat these
numbers on the **same seed set, duration and concurrency** (plan P0).
Raw artifacts: [`benchmarks/v1-baseline/results/20260927T193459Z/`](../../../benchmarks/v1-baseline/results/20260927T193459Z/).
Reproduce: `benchmarks/v1-baseline/run.sh 10 50` (with the compose stack up).

## Setup

| | |
|---|---|
| V1 commit | `2dfb542dd9ecf72f8d31e97589b922076ccf4317` (committed HEAD, run from a `git archive` snapshot; V1 untouched) |
| Harness | V1 `main.py --runtime 10 --crawler-engine auto --monitor-resources --monitor-interval 5 --output …` (V1's own run report + `tests/benchmarks/common.py:ResourceMonitor`), wrapped in `/usr/bin/time -v`; per-engine data parsed from V1's per-page `Processed … via <engine> chain=…` log lines (`benchmarks/v1-baseline/analyze.py`) |
| Seed set | `benchmarks/v1-baseline/seeds.txt` = V1 `seeds/piracy_sites.txt` @ `2dfb542`, 51 URLs (other V1 seed files are empty) |
| Configuration | V1 `config.yaml` @ `2dfb542` with overrides: `concurrency 50`, seed file above, frontier + media evidence on Redis `127.0.0.1:16379` db 2, namespaces `bench_v1_baseline`/`…_evidence` (flushed before the run). Unchanged: `timeout 15`, `rate_limit 0.3`, `max_retries 3`, `lease_ttl 90`, Scrapling enabled (headless, stealth). Full file: `results/…/config.yaml` |
| Workers | 50 async workers in one process (engine `auto` = hybrid escalation async → http/browser engines) |
| Duration | 10 min configured; 659.9 s crawl wall time; 660.6 s process wall time |
| Hardware | Intel i5-11400H (12 threads), 15 GiB RAM, NVIDIA RTX 2050 4 GB (unused), kernel 7.0.0-34, Python 3.12.3 |
| Network | Live internet, residential connection, 2026-09-27 19:35–19:46 UTC |
| Concurrent load | Compose stack idle (Redis/Scylla/MinIO); no other benchmark running |

## Results

| Metric | V1 baseline |
|---|---|
| **Pages/sec** (visited) | **1.02** (674 visited / 659.9 s); 1.05 processed/s |
| **Fetch success rate** (pages) | **97.5 %** (674 visited / 691 processed; 17 failed) |
| Success by engine (attempt → success) | async **93.5 %** (646/691) · scrapling **40.0 %** (18/45) · selenium **34.6 %** (9/26) · playwright **3.7 %** (1/27) · http **0 %** (0/17) |
| **Browser share** | **4.2 %** of visited pages finished on a browser engine (28/674); **12.2 %** of all fetch attempts were browser attempts (98/806) |
| **Bytes/page** | **not measurable** in V1 (see limitations) |
| **CPU** | 243.5 CPU-s over 660.6 s ⇒ **36.9 % of one core average** (whole process tree incl. reaped browser children, `/usr/bin/time`); V1 monitor, main process only: 8.8 % avg |
| **RSS** | main crawler process: **214 MB avg, 222 MB peak** (V1 monitor); largest single process in the tree: **296 MB** (`/usr/bin/time`) |
| **Media discovered / 1,000 pages** | **0** (0 media assets in the evidence namespace) |
| URLs discovered | 5,745 unique |

## Limitations (read before comparing)

1. **Live-internet, single run.** Piracy sites change domains, block and
   go down; numbers are indicative, not reproducible to the page. Compare
   V2 on the same seed file, 10 min, 50 workers, ideally the same day as a
   fresh V1 run, and look at magnitudes rather than decimals.
2. **Throughput is politeness-bound**, not CPU-bound: `rate_limit 0.3`
   per domain over 51 seed domains caps V1 well below its worker count,
   and CPU averaged only ~37 % of one core. V2's pages/s comparison must
   use the same politeness settings.
3. **Bytes/page cannot be measured**: every V1 engine's `fetch()` returns
   only `(html, error)` and nothing logs sizes (plan A.2 D1). V2 must
   record bytes from P4 (`FetchResult`), so V2 sets the first value.
4. **Media per 1k pages = 0** is a real observation for this seed set and
   window (no media-evidence keys were written), not a measurement fault.
   V1 only records media it detects directly in page HTML; these seeds are
   listing/landing pages whose video sits behind further navigation, ad
   gates or JS players. This baseline therefore does not constrain V2's
   media discovery; a longer run or a media-rich seed set is needed for
   that metric (P5/P8 audit).
5. **Engine success rates are conditional**: browser engines only see URLs
   the async engine already failed or flagged as needing rendering, so
   their low rates partly reflect hard pages, not only engine quality.
   An async result "needs browser rendering" counts as a failed async attempt.
6. **CPU/RSS scope**: V1's ResourceMonitor samples the main process only
   (instantaneous psutil samples; its peak CPU is unreliable and not
   reported). `/usr/bin/time` includes reaped children but its max RSS is
   the largest *single* process, not the sum of the tree.
7. **Nested retries (D4)**: V1 retries inside `fetch()` (up to 3×) before
   the frontier sees a failure, so "attempts" above are engine attempts per
   page, not HTTP requests.
