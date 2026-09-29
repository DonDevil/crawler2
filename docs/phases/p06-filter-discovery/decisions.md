# P6 Decisions, deviations, limitations and deferred work

## 1. Decisions

| # | Decision | Where recorded | Reason |
|---|---|---|---|
| D-1 | Discovered links are filtered **at admission** (`crawler2-discover admit`); P5 `LinkPolicy` stays allow-all | design §22 Q1, approved 2026-09-29 | P5 facts (links, `link_set` hash, `urls.discovered`) must not depend on the ruleset active at first extraction |
| D-2 | M1 scope = **rooted sites + leaves**: links within a seed/search/operator registrable domain are followed; external links are fetched once and not expanded | design §22 Q2, approved | V1-like breadth without whole-web expansion; no value judgement |
| D-3 | M1 revisits are **static and uniform**: seeds every 6 h, a rediscovered URL when its last admission is ≥ 24 h old | design §22 Q3, approved | no visited-forever set; P7 needs repeated observations; P7 replaces the rule |
| D-4 | M1 search queries come **only from an operator query file** | design §22 Q4, approved | P6 must not invent (target-derived) queries |
| D-5 | Deterministic rule engine; confidence is a declared rule property, not a computed probability | design §6 | reproducible, explainable decisions |
| D-6 | Precedence: operator explicit action → ABP exception / `$important` → kind specificity → longer match → protective class → source rank → rule id | design §6.2 | total order; generic rules cannot outrank specific protective ones |
| D-7 | Exceptions (`@@`) cancel only AD/TRACKER rules | design §6.2 | ABP exceptions are defined for blocking filters; they must not undo explicit scope policy |
| D-8 | Rule identity = hash of what the rule matches (source, kind, pattern, modifiers); ruleset identity = hash of all rule documents + decision policy + engine semantics + PSL version | model.py, engine.py | stable ids across re-imports; a decision is reproducible from its ruleset id |
| D-9 | Decision policy (block threshold, blockable classes) is stored **in the ruleset**, not in process configuration | design §6.3, `filter_rulesets.policy` | reproducibility |
| D-10 | Rules live in Scylla (F1–F4): immutable source revisions and rulesets, one active pointer moved by LWT; processes poll the pointer (15 s) and swap a verified, fully compiled engine | design §13–§14 | central, durable, multi-host; no local authoritative state |
| D-11 | Third-party lists are downloaded by an explicit operator import, **not vendored**; the list's licence header is stored with the revision; import refuses a list without one | audit §6 | EasyList/EasyPrivacy: GPL-3.0+ / CC BY-SA 3.0+ with attribution to "The EasyList authors (https://easylist.to/)" |
| D-12 | V1 blacklist entries are enabled only through a reviewed manifest (`crawler2/filtering/data/v1_blacklist_review.toml`); unreviewed hosts are quarantined | audit §2 | V1 entries have no provenance; 21 are crawl targets |
| D-13 | Out-of-scope sites (social/reference) are `CONTENT` + explicit BLOCK in the LINK context only | builtin.py, manifest | "not a target" is policy, not an ad classification; embedded players still load |
| D-14 | New dependency `publicsuffixlist` (MPL-2.0) for registrable domains | design §4 | third-party relationship needs eTLD+1; P1 keeps it out of identity |
| D-15 | P6 emits no events; decision data is written to P6-owned tables | design §13 | no P1 contract change |
| D-16 | M1 is its own dataset: keyspace `crawler2_m1`, Redis namespace `m1`, stream prefix `m1:events:`, bucket `crawler2-m1` | `benchmarks/p6-m1/run.sh` | P7 gets clean, delimited history |

## 2. Deviations found while implementing

| # | Deviation | Found by | Resolution |
|---|---|---|---|
| X-1 | Design §7 planned an optional per-process LRU decision cache. Not implemented: the uncached engine meets Gate D | Gate D | no cache; nothing to invalidate on reload |
| X-2 | The first engine build reached 33k decisions/s. Profiling showed 36 patterns with only 1–2-character bounded tokens being evaluated for every URL, then ~14 dict lookups and a new `Decision` object per default decision | Gate D profile | short tokens are a fallback index key; one prefilter regex per token bucket; token/key set intersection; one immutable default decision per field. Decisions unchanged (identical counts and ruleset id), 33k → 128k/s |
| X-3 | The ABP importer applied untyped URL patterns to page documents (`/ad/` blocked a content page, `/adserver.` a download link) | Gate E, 2 guard violations | ABP semantics restored: untyped URL patterns exclude `document` and `popup` (importer `p6-abp/v2`); hostname rules still apply to documents. This follows the ABP/uBO specification rather than the labelled set; the pre-fix result is in [benchmarks.md](benchmarks.md) |
| X-4 | P4 hook: `InterceptedRequest` gained `page_url`/`is_main_frame`, `InterceptDecision` gained `classification`/`ruleset`, `RenderMetrics` gained the per-page aggregate; the recorder writes F8 | design §12 | additive, crawler2-internal (no P1 change) |
| X-5 | **P5 defect:** `crawler2-extract` gave `RedisStreamReader` the frontier's `decode_responses=True` client; every real entry raised `KeyError(b'envelope')`. P5 tests used a raw client, so the CLI wiring was never exercised | M1 smoke run | `connect_stream_client` (byte replies) for both stream consumers, regression test (commit `434b511`) |
| X-6 | The V1 blacklist has 98 entries, not 1,463 (the larger file no longer exists) | audit §2 | imported what exists; recorded |
| X-8 | `events.stream_maxlen` (100,000, count-based) does not bound Redis memory at M1 volume: `urls.discovered` entries average ~17 KB, so the stream alone would exceed the compose Redis `maxmemory` (768 MB, `noeviction`) and block frontier and relay writes | M1 monitor at 0.9 h (546 MB, +400 MB/h) | M1 runs with `stream_maxlen=10000` (config only; lag ≤ 613 observed; the Scylla outbox stays authoritative for replay). A byte-based retention policy is a P14 item |
| X-9 | On the dev host (Scylla on a 5,400-rpm USB HDD, P2) the loop's Scylla writes limit M1 to ~0.7 extracted pages/s; at http concurrency 16 and 8 fetching outran extraction and the `page.observed` lag approached the stream cap | M1 monitor, 21:00 | M1 runs at http concurrency 4 with two extraction consumers. Not a P6 design change; the P2 disk caveat applies (docs/benchmarks.md) |
| X-7 | Worker and extraction CLIs expose no Prometheus endpoint; M1 monitoring uses process statistics, Redis/stream state, relay and admission metrics, and Scylla at report time | M1 tooling | left unchanged (P4/P5 scope) |

## 3. Known limitations

1. **Classification coverage.** Only AD and TRACKER have third-party
   rules; CONTENT is asserted only by explicit out-of-scope policy;
   MEDIA/PLAYER/NAVIGATION have no positive rules (recall 0 by design —
   P6's duty there is "do not block"). Positive content classification is
   P7/P8 work.
2. **ABP subset.** Cosmetic, scriptlet, HTML, regex, `$redirect`,
   `$removeparam`, `$csp`, `$rewrite`, entity (`example.*`) domains and
   popup-only filters are counted and skipped (27,784 EasyList lines,
   mostly cosmetic; 80 EasyPrivacy lines).
3. **Labelled corpus.** 346 items, 185 hand-written (synthetic) because
   the W691 pages reference few ad/tracker hosts; hand-picked well-known
   ad/tracker endpoints bias recall upward. Classes with < 20 items
   (navigation 19, unknown 15) are reported but not meaningful.
4. **Scope roots** are registrable domains; a piracy site that moves to a
   new domain is rooted again only when a seed, search result or operator
   adds it (redirect-following of seeds is not rooted automatically).
5. **Search adapters** depend on engine markup; selectors are validated
   on synthetic pages, and live behaviour is whatever the engines serve
   to the configured user agent (blocked pages are recorded, not evaded).
6. **Revisit gate** is per URL and static; a page that changes hourly is
   still revisited at most daily (P7).
7. **Normalization selectors** (`selector` rules) are supported by the
   model but not wired into `crawler2-extract` in M1 (baseline scheme;
   a scheme change re-keys revisions, ADR-020).
8. **F6 decision history** is bounded by "write on admission or change"
   and a 90-day TTL, not by a hard row cap.

## 4. Deferred work

| Item | Owner |
|---|---|
| Priority beyond static provenance priorities; recrawl scheduling; change-rate, site-template and value learning | P7 |
| Selector (cosmetic) rules for normalization, per-site templates (P5 precision 1/23) | P7 (templates), operator rules (P6 mechanism exists) |
| Media identity, probing, manifests from discovered media | P8 |
| Tor search (Torch) — no Tor daemon on this host | ops |
| Prometheus endpoints for worker/extraction processes | P14 |
| Rebuilding F5/F6 by replaying `urls.discovered` from Scylla facts | P14 (procedure documented, not automated) |
