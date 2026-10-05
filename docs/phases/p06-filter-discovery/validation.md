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
| G — 24 h run | **NOT PASSED** (run 2; run 3 needed) | run 1: 16.7 h, killed with the operator terminal (X-10), §4.2. Run 2: full 24 h window 2026-10-04 07:39:53 → 10-05 07:39:53 UTC, detached, 1,440 samples, no sampling gap; stability and leak criteria met (no crash, FDs/threads/processes flat, http/admit/extract RSS flat over the last 12 h, browser tree inconclusive); **lost-events criterion not met**: an 80-min event-loop stall on a full Redis and ~1,850 `page.observed` entries trimmed unread, plus a configuration change inside the window (X-16), §4.3 |
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

### 4.3 24-hour run (Gate G) — run 2, complete: not passed

Started 2026-10-04 07:39:53 UTC with `run.sh start ~/Desktop/query.txt`
(same query file, sha256 `338ffb33…`; configuration unchanged from run 1:
http 4, browser 2, 2 × extract, `stream_maxlen` 10,000). Every supervisor
has its own session (verified: SID = PID). The configuration is final from
the start, so the window starts with the run (`var/p6-m1/window_start`) and
ends 2026-10-05 07:39:53 UTC. The Scylla/Redis/MinIO dataset is run 1's,
continued (frontier full at 200,000; keyspace 2.6 GB; bucket 57,052
objects, 7.0 GB); run 1's samples, logs and restarts were archived to
`var/p6-m1/runs/2026-09-29T172615Z/`.

Tooling changes before run 2 (X-13): the monitor tallies every
`fetch.completed` entry per sample (outcome, capability, status, detail;
cursor persisted, trimmed-unread entries counted as `missed`), so the
V1-comparable fetch metrics no longer need the Scylla scan (X-12; the scan
is now `report.py --scylla`); it records host iowait/load/disk I/O; disk
sizes are measured in a background thread, MinIO's from its own bucket
usage metrics (`du` over the bucket took > 7 min on the HDD); Prometheus
`*_created` timestamps are no longer recorded as counters; a failing probe
is recorded in `errors` instead of stopping the monitor. `report.py`
evaluates from `window_start` and reports sampling gaps, exits per process,
completions per hour and the fetch metrics. `run.sh check` prints window
progress and last-hour health (exit 1 = a process down or the monitor
stale); `run.sh start` refuses to start over a running M1 and archives the
previous run's state.

First 2 minutes: all 9 processes up; one relay exit (Scylla `WriteTimeout`
on the checkpoint, X-11) and extraction `WriteTimeout`s retried in-process
while the extraction backlog (3.4k) drained at 46 % host iowait.
**Progress at 5.0 h (12:37 UTC, from the half-hourly `run.sh check`).**
No process has exited since the first two minutes (relay 10, X-11);
0 dead letters; both consumer groups within single digits of the stream
head; host iowait fell from 46 % to 9 %. Tree RSS: http 102 → 207 MB
(flat at 201–207 MB since 3 h), admit 370 → 418 MB, extract ~190 MB,
browser 1.0–1.5 GB (recycling). Free disk 370.7 → 363.8 GB; bucket
7.0 → 10.9 GB.

| Hour of window (UTC) | completions | fetches | 2xx of fetches |
|---|---:|---:|---:|
| 08:40 (1 h) | 5,993 | 7,923 | 78.1 % |
| 09:40 (2 h) | 8,065 | 11,213 | 72.6 % |
| 10:40 (3 h) | 7,014 | 9,555 | 66.7 % |
| 11:40 (4 h) | 4,585 | 6,382 | 68.3 % |
| 12:37 (5 h) | 3,231 | — | — |

**Throughput falls although nothing is saturated** (all 4 http slots
leased, 5,541 eligible domains, iowait 9 %): the mean fetch time doubled
(1.43 s for fetches finished 10:12–10:26 → 2.8 s for 11:16–11:39)
because the crawl concentrated on two hosts, which took ~85 % of fetch
time in the later sample: `www.ncbi.nlm.nih.gov` (slow responses,
timeouts) and `ftp.ncbi.nlm.nih.gov` (bulk files read until the size
limit aborts them: `too_large`, ~6 s per attempt, 16 % of fetch time in
an earlier sample). Both descend from the Bing results for the operator
queries (§4.1, P7 relevance). Separately, `scholar.google.com` answered
429 to every request (737 of 3,000 consecutive fetches at 2.5 h, 25 %) and
was claimed again and again: a 429 is recorded `blocked` and retried
without slowing the domain (X-14). These shape run 2's throughput and
fetch-rate numbers; they are not M1 stability failures. The operator
decided (2026-10-04) to keep run 2 running unchanged for the full window;
the report will state fetch rates with and without these hosts.

**Incident at 8.1 h: Redis full, event loop stalled 80 min (X-16).**
Redis used memory rose from 681 MB (15:20) to the 768 MB `maxmemory`
(`noeviction`) by 15:44 UTC: the `urls.discovered` stream held 406 MB in
its 10,000 retained entries (~40 KB each, NCBI pages carry thousands of
links; run 1's were ~17 KB). Redis then refused the relay's `XADD`, and the
relay exited on every attempt: 780 exits 15:44:14 → 17:04:52. Fetching
continued (completions kept rising), but no event was published, so
extraction and admission idled; nothing was lost (unpublished events
stayed in the Scylla outbox; no other process hit the limit). The
watcher reported the exit storm at ~15:46; the operator chose the fix at
17:04. Actions, 17:04:26–27 UTC: `XTRIM urls.discovered.v1 MAXLEN 2000`
(lossless: group `discovery-admission` had delivered the stream's last ID,
0 pending) → Redis 757 → 575 MB; `M1_STREAM_MAXLEN=3000 run.sh
restart-relay` (all streams retain 3,000 entries from then on). The relay
resumed publishing at 17:04:57 after one more X-11 outbox read timeout
and replayed the 80-minute outbox backlog within about two minutes.
**That replay lost events for extraction:** it outran the extraction
group under the new 3,000-entry cap, so ~1,850 `page.observed` entries
were trimmed before extraction read them: the group's lag settled at
1,858 once extraction had caught up (17:40; last-delivered ID = newest
entry), against 8 before the stall. Redis counts trimmed entries as never
read, so that lag stays as a constant offset. (A first estimate of
~1,550, taken at 17:06–17:08 while the replay was still running, was
low.) Those pages are stored (W2/W3 rows
and snapshots) but were not extracted, so their links were not
discovered; they are fetched again at their 24 h revisit, and re-publishing
them from the outbox is the unautomated F5/F6 replay procedure (P14).
`urls.discovered` lost nothing (0 trimmed unread). The monitor's
`fetch.completed` tally missed 1,234 entries of the same burst
(`fetch.missed`), so run 2's fetch metrics undercount the stall period.
The loss stopped once the backlog was drained (lag constant at
1,858 from 17:40 on). Lowering retention while an outbox backlog exists was the cause:
the lag check before the trim covered `urls.discovered` only. **This changes the
M1 configuration inside the Gate G window and stalls the closed loop for
80 min; both are reported with the gate verdict.** The watcher now also
alerts at Redis > 700 MB.

**Near-miss at 12.6–16.3 h.** Even at 3,000 entries, `urls.discovered`
reached 347 MB at 20:35 UTC (~115 KB per entry): pages of
`account.ncbi.nlm.nih.gov` carry ~5,200 links each (~4 MB per entry).
Redis rose from 356 MB to a peak of 653.8 MB (23:57 UTC), then fell back
to 407 MB as those entries rotated out; it never reached the 700 MB alert
or the 768 MB limit, and no process exited. A lossless trim was prepared
(operator-approved) but was not needed. Count-based retention cannot bound
memory when one entry can be 4 MB (X-8, X-16).

**Final results (window 2026-10-04 07:39:53 → 2026-10-05 07:39:53 UTC).**
`report.py --window-h 24 --since 2026-10-04T07:39:53Z --until
2026-10-05T07:39:53Z`, raw `benchmarks/p6-m1/results/m1-run2.json`;
1,440 samples over 23.98 h, no sampling gap over 5 min, no monitor probe
error. M1 was not stopped and continues as P7 history.

| Measure | Value |
|---|---|
| process exits | relay 790 (10 at start-up, 780 in the Redis stall, X-11/X-16); **no other process exited**; none after 17:04:52 UTC (14.6 h) |
| frontier completions / claims | 108,669 / 149,067 (4,528/h; run 1: 1,611/h) |
| completions per hour | 6,083 · 8,221 · 7,094 · 4,639 · 3,194 · 2,311 · 3,361 · 3,624 · 4,239 · 3,170 · 3,117 · 2,295 · 5,258 · 6,770 · 2,515 · 3,768 · 5,349 · 6,473 · 5,701 · 5,490 · 4,634 · 3,254 · 4,740 |
| completions during the 80-min stall | 5,967 (fetching continued; events waited in the outbox) |
| retried / exhausted / dead letters | 40,046 / 258 / **0** |
| admissions accepted / refused (frontier at its 200,000 cap) | 108,922 / 2,668,533 |
| fetch attempts (monitor tally) | 148,433 (1.72/s), of which browser 2.2 %; 1,234 not tallied (X-16 burst) |
| fetch outcomes | response 93,620 · blocked 34,882 · too_large 16,421 · timeout 2,061 · tls 737 · dns 377 · connection 335 |
| 2xx of attempts | **60.3 %** (response of any status 63.1 %; V1-comparable "success" = response + blocked 86.6 %) |
| 2xx excluding 429 and `too_large` attempts | **90.6 %** (89,472 / 98,756) — approximates "excluding Scholar and NCBI FTP": every sampled 429 came from `scholar.google.com` and 97 % of sampled `too_large` from `ftp.ncbi.nlm.nih.gov`, but the tallies carry no host, so this is by outcome, not by host |
| HTTP 429 | 33,256 (22 % of attempts, X-14) |
| Redis used memory | min 354 · mean 544 · **max 772 MB** (16:32 UTC, during the stall) · last 385 |
| host iowait | mean 11.9 %, max 65.6 % (start-up backlog) |
| Scylla keyspace / MinIO bucket / host free | 2.59 → 7.13 GB / 7.0 → 16.8 GB (57,052 → 165,257 objects) / 370.7 → 355.4 GB |

The relay's Prometheus counters restart with the process, so per-event
publication totals are not usable from this run; the monitor's fetch
tally is.

**Resources** (RSS, MB; least-squares slope; hour 0 excluded as start-up).

| Process | h 1–24 | last 12 h | last 6 h |
|---|---|---|---|
| http | 163 → 216, +0.99/h | 210 → 216, +0.51/h | 216 → 216, +0.16/h |
| admit | 392 → 432 (max 515), +1.05/h | 428 → 432, −0.19/h | 437 → 432, −0.44/h |
| extract / extract2 | 206 → 222 / 200 → 202, ~+1/h | +0.24 / −0.43/h | +0.65 / −1.07/h |
| relay | 94 → 82, −0.35/h | +0.12/h | −0.83/h |
| browser (Chromium tree) | 1,378 → 1,397 (max 1,903), +8.2/h | +18.4/h | +16.6/h |

FDs (http 38, others ≤ 19), threads and process counts were flat for every
process. http, admit, extract and relay plateau: the slow climb of http
in run 1 (+5 MB/h over 16.7 h) ends at ~210–216 MB after ~4 h of this run,
so it was warm-up, not a leak. The Chromium tree is a sawtooth (recycling)
whose 4-hour medians rose 1,266 → 1,305 → 1,378 → 1,330 → 1,340 → 1,462 MB
and minima 1,026 → 1,190 MB with peaks bounded at 1.68–1.90 GB: a small
upward drift that 24 h cannot separate from the changing page mix —
**inconclusive**, followed up in the continuing M1 run.

**Gate G verdict: not passed by run 2.**

- *Runs 24 h unattended without crashing:* **met.** A full detached
  window; no process other than the relay exited, and the relay's exits
  were supervisor-restarted.
- *No leaks:* **met for http, admit, extract, relay** (flat over the last
  12 h); **inconclusive for the Chromium tree** (bounded peaks, small
  median drift).
- *No lost events:* **not met.** The event loop stalled for 80 min when
  count-based retention filled Redis, and the backlog replay after the fix
  trimmed ~1,850 `page.observed` entries before extraction read them
  (pages stored, not extracted). In addition, `stream_maxlen` was changed
  inside the window (10,000 → 3,000, an operator decision).

**Run 3 needs**, before it starts: (1) Redis memory bounded by bytes, not
entries: byte-based retention (X-8) or, at minimum, a `stream_maxlen`
chosen for the largest entries (~4 MB, X-16 near-miss) together with
consumer-aware trimming, so a backlog replay cannot trim unread entries;
(2) the relay retrying storage and Redis errors in-process instead of
exiting (X-11); (3) per-domain back-off on persistent 429 (X-14) and no
bulk non-HTML downloads to the size limit (X-15), so that throughput and
fetch-rate numbers describe the crawl rather than two hosts. Items (1) and
(2) are P14 hardening in the plan; Gate G stays open until run 3, or until
the plan moves the gate to after that hardening.
