# P6 Validation

Design: [design.md](design.md) · Implementation: [implementation.md](implementation.md) ·
Benchmarks: [benchmarks.md](benchmarks.md) · Decisions: [decisions.md](decisions.md).

## 1. Gate status

| Gate | Status | Evidence |
|---|---|---|
| A — audit | **PASS** | [audit.md](audit.md), commit `923847a` |
| B — design review | **PASS** | approved 2026-09-29 (Q1–Q4), [design.md](design.md) §22 |
| C — implementation | **PASS** | §2; commits in §6 |
| D — ≥ 100k decisions/s | **PASS** | 127,985 warm decisions/s, no cache ([benchmarks.md](benchmarks.md) §1) |
| E — false-positive safety | **PASS** (after fix X-3) | 0 generic-rule blocks of protected items; per-class P/R in benchmarks §2 |
| F — M1 closed loop | **PASS** | fixture loop test + live run §4.1 |
| G — 24 h run | **OPEN** | run 1 reached 16.7 h of its 24 h window (2026-09-29 21:20 → 2026-09-30 13:59:58 UTC) and was then killed with the operator terminal (X-10); no data lost; resource and reliability findings in §4.2. Next: rerun detached (`setsid`) for a full 24 h |
| H — P1–P6 regression | **PASS** (host tiers) | §3; the in-container `validate-stack` tier was not run while M1 occupies the stack |

## 2. Tests

P6 tier (`scripts/test-filter-discovery.sh`): **105 passed** — 100 unit
(filtering 76, discovery 24; per-file counts in implementation §5), 3 Scylla rule-store/discovery-row tests, the M1
fixture loop and the Chromium interception test. Test-plan coverage:

| # | Category | Covered by |
|---|---|---|
| 1–5 | model, parser, precedence, overrides, provenance | `test_engine.py`, `test_abp.py` |
| 6 | V1 importer (audit counts pinned) | `test_v1import.py` |
| 7–8 | ABP supported / unsupported / invalid syntax | `test_abp.py` |
| 9 | deterministic compilation (hypothesis) | `test_engine.py` |
| 10, 22 | hot reload, failure, rollback, concurrent readers during swaps | `test_store.py`, `test_filter_discovery.py` |
| 11 | admission consumer | `test_admission.py`, `test_m1_loop.py` |
| 12 | P4 interception, real Chromium | `test_intercept.py`, `test_interception.py` |
| 13 | redirects | `test_admission.py` |
| 14 | search adapters (synthetic pages), cooldown | `test_seeds_search.py` |
| 15–16 | seeds, admission generation | `test_seeds_search.py`, `test_admission.py` |
| 17–18 | corpus guard, precision/recall | `test_corpus.py`, `fp_eval.py` |
| 19 | throughput | `throughput.py` |
| 20–21 | Redis/Scylla integration, two consumers in one group | `test_filter_discovery.py`, `test_m1_loop.py` |
| 23 | M1 end-to-end fixture loop | `test_m1_loop.py` |
| 24 | 24 h run | §4.2 |

## 3. Regression (Gate H), 2026-09-29, host

| Tier | Result |
|---|---|
| `make check` (ruff, mypy 201 files, unit + contract P1–P6) | 538 passed, 112 skipped (integration, run below) |
| P3 frontier integration (Redis) | 53 passed |
| P5 extraction + P2 storage integration (`scripts/test-extraction.sh`) | 153 passed |
| P4 crawlers incl. browser and 1,000-page leak (`scripts/test-crawlers.sh`) | 86 passed, 22 skipped (contract cases not applicable to a fetcher) |
| P6 (`scripts/test-filter-discovery.sh`) | 105 passed |

P1 contracts are unchanged (P6 emits no events). V1 was only read.

## 4. M1

### 4.1 Live closed loop (Gate F)

Configuration: dataset `crawler2_m1` / Redis `m1` / streams `m1:events:*` /
bucket `crawler2-m1`; ruleset `rs-df6cc49a8786196590ecedd5133d3786`
(106,110 rules: built_in, V1 reviewed, EasyList and EasyPrivacy of
2026-09-29); seeds = the 52-line P0 seed file (`w52-v1-seeds`, every 6 h);
search = operator file `~/Desktop/query.txt` (4 queries, sha256
`338ffb33…`, every 24 h; engines duckduckgo, bing, brave, ahmia);
processes = relay, 2 × extract, admit, http pool (concurrency 4), browser pool
(2 pages), seeds, search, monitor, each supervised. Timeline (UTC):

| Time | Event |
|---|---|
| 17:25 | first start: `crawler2-extract` crash-looped (`KeyError: b'envelope'`) — P5 defect X-5; stopped, fixed (`434b511`) |
| 17:26:15 | M1 start (all processes) |
| 17:59:58 | http pool reconfigured 16 → 8 (disk budget: ~110 GB/day of snapshots at 16) |
| 18:06:45 | search added (query file supplied) |
| 18:25:12 | relay reconfigured `events.stream_maxlen` 100,000 → 10,000 (Redis memory, X-8) |
| 18:25–18:27 | the **monitor** crash-looped: it ran P3 `audit()` (documented offline-only) against the live frontier, which races with claims (`KeyError`); live audit removed, monitor restarted 18:27:32. Crawl processes unaffected; 2-minute sampling gap |
| 20:56:59 | relay exited on a Scylla `ReadTimeout` (outbox read, HDD; P2 raises `StorageUnavailableError` by design); supervisor restarted it 5 s later, resuming from its checkpoint |
| 21:01:07 | `page.observed` lag reached 9,792 of the 10,000-entry stream cap (extraction ~1.2 pages/s vs ~1.6 fetched; Scylla timeouts); second extraction consumer added — no gain (Scylla-bound) |
| 21:16:38 | http pool 8 → 4; extraction (2 consumers, ~2 pages/s) now outpaces fetching; the group never fell behind the stream's first entry (entries read + length ≥ entries added throughout), so no event was trimmed unread |
| 21:20 | **Gate G window starts** (final configuration; the earlier 18:30 window was abandoned) |

At 18:22 (0.93 h): 21,650 claims, 20,184 completed, 1,391 retries, 67
exhausted, 0 dead letters, frontier `audit()` 0 problems (the one hourly audit that ran before the monitor fix); 20,340
`page.observed`, 15,110 `urls.discovered`, 15,235 `page.changed`, 252
`media.discovered`; extraction lag ≤ 613, admission lag ≤ 5; 0 process
crashes. Filter decisions on links: 207,965 allow, 5,942 + 995 out-of-scope
blocks (built_in / V1), 283 ad and 235 tracker blocks. First search pass:
Bing returned 10 results per query (20 admitted, 20 blocked — all blocked
ones explicit `out_of_scope`: Wikipedia, IMDb, YouTube, BookMyShow,
JustWatch, Netflix); DuckDuckGo answered 202 (anti-bot page) and Brave
429 → recorded `blocked`, cooled down, not retried; Ahmia returned no
results.

**Observation for P7 (not changed in P6):** Bing ignored the piracy terms
of the operator queries and returned bioinformatics "BLAST" tools and
licensed platforms (ncbi.nlm.nih.gov, hotstar, jiotv); under the approved
scope rule these became roots. Relevance of search results is P7 work.

### 4.2 24-hour run (Gate G) — run 1, incomplete

**Outcome: OPEN.** The window ran 2026-09-29 21:20 → 2026-09-30 13:59:58 UTC
(16.7 h, 913 samples). At 13:59 the operator's terminal crashed and every
M1 process died with it: `run.sh` had started the supervisors with `&`
from that terminal's session, so they shared its process group (X-10).
The window is incomplete and is not counted. Results:
`benchmarks/p6-m1/results/m1-run1.json` (sample-based; `report.py --no-scylla`).

**Data safety at the stop.** Both consumer groups were still inside their
streams (entries read + stream length ≥ entries added), so no event was
trimmed unread; 154 unacknowledged `page.observed` and 4 `urls.discovered`
entries are reclaimed by `claim_stale` on restart; leased frontier tasks
return when their leases expire; the Scylla outbox stays authoritative.

**Volume (window).**

| Measure | Value |
|---|---|
| frontier claims / completions | 39,209 / 26,845 (1,611 completions/h) |
| retries / exhausted / deferred | 9,951 / 117 / 2,296 |
| dead letters | 0 |
| new admissions accepted | 26,963 |
| admissions refused, frontier full (`max_depth` 200,000 since ~21:06) | 510,029 (268,623 leaf + 241,406 link) |
| rediscoveries skipped by the 24 h revisit gate | 793,739 |
| `urls.discovered` events handled | 21,857 (18,000 rooted pages admitted, 3,857 leaf pages not expanded) |
| link filter decisions | 594,511 allow · 38,437 out-of-scope block (built_in) · 72 out-of-scope block (V1) · 681 tracker block · 4 ad block |
| completions per 2 h | 3,290 · 2,943 · 5,301 · 4,557 · 4,269 · 2,300 · 2,553 |

Whole run (17:26 → 13:59, 20.5 h): ~69,100 completions; the first hour at
http concurrency 16 completed ~20,000 pages (~8/s).

**Throughput is disk-bound.** From ~08:00 UTC the host spent ~77 % of CPU
time in I/O wait while Scylla compacted `crawler2_m1` (2.6 GB) on the USB
HDD; completions fell from 4.3–5.3k to 2.3–2.5k per 2 h and the
extraction lag rose from 8 to 3,933 (max in window 9,694 at the start,
before the http 8 → 4 change drained it). The filter costs microseconds
per decision and is not the bottleneck.

**Resources (window; least-squares slope per hour).**

| Process | RSS first → last (max) | slope | FDs | threads | children |
|---|---|---:|---|---|---|
| admit | 408 → 542 (736) MB | +8.6 MB/h | 10 flat | 7 flat | — |
| browser (tree) | 1,296 → 1,279 (1,632) MB | −3.3 MB/h | 19 → 24 | 10 → 14 | 8 (max 11) |
| extract / extract2 | 178 → 229 / 177 → 245 MB | +2.5 / +1.9 MB/h | 9 flat | 6 flat | — |
| http | 102 → 199 MB | +5.1 MB/h | 29–39 | 19 → 22 | — |
| relay | 77 → 67 MB (restarted 399×) | — | — | — | — |
| search | 352 MB flat | 0 | 10 | 6 | — |
| Redis used memory | 681 → 625 MB (max 686) | −3.7 MB/h | clients bounded | | |

Reading: no file-descriptor, thread or process growth anywhere; the
Chromium tree is bounded by recycling; Redis is flat (frontier and streams
both capped). **Not conclusive:** the http worker's RSS rose steadily
(~5 MB/h) and admit's is not monotonic (peak 736 MB, then 542 MB, i.e.
likely caches). 16.7 h cannot separate a slow leak from warm-up, so the
"no leaks" criterion is **not yet demonstrated**.

**Reliability.** Exits in the window: relay 399, seeds 4 — every one a
Scylla `ReadTimeout` / `WriteTimeout` / `OperationTimedOut` or "cannot
connect" (connect timeout 2 s) during the I/O saturation. The supervisor
restarted each within 5 s and the relay resumed from its checkpoint, so
nothing was lost, but a publisher that exits on every storage timeout is a
P14 hardening item (X-11). No other process exited.

**Storage.** Host free space 380.4 → 369.6 GB over the whole run; Scylla
keyspace 2.6 GB (+ 1.3 GB commitlog). The MinIO bucket size sample failed
(the `du` timed out under the same I/O load).

**Search.** Only Bing returned results from this host (DuckDuckGo 202,
Brave 429 → recorded blocked, cooled down); Bing ignored the piracy terms
of the operator queries. A P7 relevance issue.

**Not measured.** The V1-comparable fetch metrics (response / 2xx rate,
browser share, per-outcome counts) need a scan of the M1 keyspace; it
timed out at 10 s and 60 s even with 200-row pages while Scylla was still
compacting after the stop. Re-run `report.py` when the disk is quiet.

**Next run (Gate G).** `benchmarks/p6-m1/run.sh start` now starts every
supervisor with `setsid` in its own session (survives terminal crashes).
Restart it with the same configuration and search file, record the new
window start, and evaluate after 24 h with `report.py --window-h 24`.
