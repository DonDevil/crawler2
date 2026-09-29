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
| G — 24 h run | **IN PROGRESS** | window 2026-09-29 21:20 UTC → 2026-09-30 21:20 UTC (restarted after the last configuration change), §4.2 |
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

### 4.2 24-hour run (Gate G)

Frontier http depth reached its `max_depth` (200,000) at ~21:06; further
new admissions are refused explicitly (`rejected_full`, not recorded as
admitted, retried on rediscovery). Redis plateaued at ~684 MB of 768 MB.

Measured on the window above with `benchmarks/p6-m1/report.py`; results in
`benchmarks/p6-m1/results/m1-24h.json`. *(Filled in at the end of the window.)*
