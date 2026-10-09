# P7 Audit — M1 history and data sufficiency

Status: **audit complete; data-sufficiency gate NOT PASSED (2026-10-07).**
P7 implementation has not started. Decisions and open questions:
[decisions.md](decisions.md).

Inputs: plan §P7 ([v2-phase-plan.md](../../v2-phase-plan.md)), P1 events
([events.md](../p01-contracts/events.md)), P2 schema (V001–V003), P3 frontier
([p3](../p03-frontier-scheduling/p3-frontier-scheduling.md) §9–§16, §24),
P4 outcomes and escalation ([p4](../p04-fetch-workers/p4-fetch-layer-worker-pools.md)
§8–§9, §22, §33), P5 ([design](../p05-extraction-page-intelligence/design.md)
§8–§12, ADR-020), P6 ([design](../p06-filter-discovery/design.md) §10, §13;
[decisions](../p06-filter-discovery/decisions.md); [validation](../p06-filter-discovery/validation.md) §4).

## 1. What was audited and how

The M1 dataset is the keyspace `crawler2_m1` (P6 D-16). It was exported
read-only to a local, git-ignored dataset and analysed offline:

| Step | Tool | Output |
|---|---|---|
| export fact tables (no bodies) | `benchmarks/p7-intelligence/export_m1.py --host <scylla-ip>` | `var/p7/m1-export/*.jsonl.gz` (~390 MB) |
| whole-dataset measures | `benchmarks/p7-intelligence/audit_m1.py` | [`results/m1-audit.json`](../../../benchmarks/p7-intelligence/results/m1-audit.json) |
| seed-set (piracy source) measures | `benchmarks/p7-intelligence/audit_seed.py` | [`results/m1-audit-seed.json`](../../../benchmarks/p7-intelligence/results/m1-audit-seed.json) |

Exported rows: 247,104 fetch attempts, 215,492 page observations, 138,920
page extracts, 130,695 page revisions, 384,028 admission rows (P6 F5),
114 search results, 52 seeds, 70 scope roots.

The export reads the crawler2 fact tables offline. A running P7 service
would get the same records from events: `fetch_attempts` ≙ `fetch.completed`,
`page_observations` ≙ `page.observed`, `page_extracts` ≙ `urls.discovered`
and `media.discovered`, and first sightings in `page_revisions_by_url` ≙
`page.changed`. Using the fact tables is a choice about the replay source.
It doesn't change who reads what at runtime (§6.3).

**Retention.** The `outbox` (the event envelopes) has a 14-day TTL, and
`fetch_attempts_by_*` and `observations_by_domain_day` have 30 days. Run 1's
events expire around 2026-10-13. The export uses only tables without a TTL,
plus F5, so it doesn't depend on those deadlines.

## 2. What M1 history exists

### 2.1 Time coverage

| Window | From (UTC) | To (UTC) | Attempts |
|---|---|---|---:|
| run 1 | 2026-09-29 17:24 | 2026-09-30 13:59 (terminal crash, P6 X-10) | 87,964 |
| run 2 | 2026-10-04 07:39 | 2026-10-05 09:31:46 | 159,140 |

**M1 is not running.** The project notes said it continued after run 2.
It did, but only until 2026-10-05 09:31:46 UTC, when Scylla and then Redis
stopped answering. The worker logs say "15:02" because they use local time
(IST). No M1 process has run since, and the stack came back with the host
on 2026-10-07 without M1. In total M1 covers about 46 h of crawling in two
windows 4 days apart.

### 2.2 Volume and distribution

| Measure | All data | 50 seed domains (W52 piracy seeds) |
|---|---:|---:|
| fetch attempts | 247,104 | 32,131 (13.0 %) |
| registrable domains fetched | 503 | 50 |
| domains with ≥ 30 attempts | 26 | 11 |
| domains with any 2xx `ok` | 357 | 25 |
| domains fetched in both runs (≥ 30 attempts) | 26 (≥ 10) | 10 |
| HTML 2xx URLs | 129,590 | 21,022 |
| URLs with ≥ 2 HTML 2xx observations | 12,817 | 2,321 |
| URLs with ≥ 3 HTML 2xx observations | 1,159 | 37 |
| … of which spanning ≥ 24 h | 1,030 | 9 |
| URLs with ≥ 1 change (revisions ≥ 2) | 2,349 | 317 |
| domains with a change | 48 | 6 |

**The history is mostly not about piracy sources.** `nih.gov` took 70.4 %
of all attempts and `google.com` (Scholar, 429s) 14.4 %. They came from the
Bing results for the operator queries, which became scope roots (P6 §4.1,
X-14, X-15). The 26 domains with ≥ 30 attempts in the whole dataset are
mostly NCBI and similar hosts plus 11 seed domains. Of the 1,159 URLs with
≥ 3 observations, 400 are on `nih.gov` and 37 on seed domains.

**Revisits were throttled by the M1 policy itself.** Links were revisited
on a static 24 h gate. The frontier sat at its 200,000-task cap, so 2.67 M
admissions were refused in run 2 (P6 validation §4.3). Seeds were readmitted
every 6 h but mostly refused as well (seed log: 1 of 52 admitted, 51
`rejected_full`). As a result, 90 % of HTML URLs were observed exactly once.

### 2.3 Fetch outcomes and capabilities (`fetch.completed`)

| Capability | Attempts | Outcome codes (P4 §9, from `detail`) |
|---|---:|---|
| http | 241,889 | ok 141,271 · blocked 48,207 (47,652 × 429) · too_large 25,944 · http_error 11,911 · needs_js 5,119 · timeout 3,972 · captcha 2,026 · tls 1,217 · network 764 · not_modified 692 · dns 601 · invalid 146 · redirect 17 · media 2 |
| browser | 5,215 | ok 4,825 · http_error 223 · blocked 64 · timeout 57 · network 35 · captcha 9 · invalid 2 |
| tor | 0 | no Tor daemon on the host (P4 limitation) |

- Escalations (`needs_js` on http, then browser on the same URL): 5,112.
  Browser then returned `ok` for 4,761 of them (93 %).
- Browser attempts by domain: nih.gov 4,423, uniprot.org 449, blogger.com
  185, movcloud.click 71, … **Seed domains: 2 browser attempts (both
  captcha) and 2 `needs_js`.** The piracy seed sites are almost entirely
  server-rendered HTML.
- Status classes: 2xx 153,253 · 429 47,652 · none 32,755 · 403 5,839 · 404
  3,471 · 5xx 1,390 · 304 692. 66,464 attempts followed at least one
  redirect.
- On seed domains: 26,176 ok, 5,351 http_error, 302 timeout. 25 of 50 seed
  domains never returned a 2xx (seed-list decay, P0/P4: about half of the
  seeds were unreachable within days).

### 2.4 Page facts (`urls.discovered`, `page.changed`, `media.discovered`)

| Signal | Coverage (138,920 extracts) |
|---|---|
| title / lang | 134,660 / 121,453 |
| canonical / og_title / og_type | 53,972 / 53,456 / 52,255 (`article` 44,246, `website` 7,241, `video.movie` **3**) |
| published_at / modified_at | 17,329 / 16,294 (12 %) |
| links per page | median 9, p90 162, max 10,000; 9.67 M in total |
| pages with media references | 703 (3,304 refs: video_file 2,462, audio 795, hls 17); 127 of these pages are on seed domains |
| year token in URL path | 13,008 pages |
| path depth 0 / 1 / 2 / 3 / ≥ 4 | 10,367 / 57,494 / 32,382 / 8,480 / 30,197 |
| P5 stats per page | element count, text chars, dropped/truncated links |

`page.changed`: 130,695 revisions on 128,298 URLs, so 2,397 are changes
after a first sighting (the other events are first sightings, ADR-020).

**Change-signal quality is unknown on M1 and suspect.** On
`tamilprint.com.in`, 202 of 218 revisited URLs changed. Every one of these
changes alters visible text, link set and structure, but never media. That
pattern fits a site-wide "latest posts" widget, which is the defect P5
measured: precision 1/23 on W691 pairs, caused by in-content template
widgets. On a piracy site such a widget can also be real new-content
signal. No M1 change has been labelled.

### 2.5 Provenance

| Source | Count |
|---|---|
| admissions by origin (F5, last action) | link 264,841 · leaf 98,028 · blocked before admission 21,086 · seed 52 · search 21 |
| search results (F10) | 114, all from Bing (DuckDuckGo 202, Brave 429 → cooldown, Ahmia empty) across 26 domains: Wikipedia, YouTube, IMDb, BookMyShow, NCBI BLAST … |
| scope roots (F11) | 70 |

### 2.6 Signals that do not exist

| Signal P7 would use | State | Consequence |
|---|---|---|
| targets / release dates (`target.registered`) | `targets` empty; no producer until P9 (fingerprinter target manager) | target relevance, release-timing boosts and target-triggered reactivation have no input |
| `media.observed`, media identity | media registry tables empty (P8) | "new media" can only count `media.discovered` references, not identities |
| Tor outcomes | no Tor daemon on this host | no `tor` fetch profile can be learned |
| per-request host in the monitor tallies | — | not needed: attempts carry URLs |
| labelled page types | none | page-type classification can't be validated without a labelled sample |
| labelled changes on M1 | none | change-rate inputs have unknown precision (§2.4) |
| media probe metadata | not persisted (P4 §32) | nothing about media size/type beyond declared kind |

What can be derived without new contracts: per-domain and per-pattern
outcome/capability statistics, escalation pairs, redirect facts, change
counts per URL, observation intervals, link-graph novelty (first-seen URLs
per fetch), URL path templates, metadata dates, link density, and
search/seed provenance.

## 3. Data-sufficiency gate

### 3.1 How the thresholds were set

The plan (§P7) fixes the form of the gate (volume and coverage, never
calendar time) and leaves the values to P7 design. Each threshold below
comes from what one P7 estimator needs to produce a usable number. None
comes from the observed counts. The thresholds were written after the
counts in §2 were known, so they were deliberately not chosen next to
those counts (none passes or fails by a small margin). They apply to the
**seed set**, because the M2 gate is defined "on the same seed set and
budget" and the seed set's sources are the population P7 acts on. Whole-data
values are shown for comparison only.

- **Proportions per source** (fetch success, capability need, dead rate):
  a Wilson 95 % interval at n = 30 is at most ±0.18 wide (worst case
  p = 0.5), the widest interval that still separates "mostly works" from
  "mostly fails". Hence 30 attempts per domain.
- **Lifecycle, decay and reactivation** need the same source seen at
  separate times. Two windows ≥ 24 h apart is the minimum that can show
  a state change.
- **Change rate (Poisson).** A per-page change-rate estimate from n
  revisits needs n ≥ 3 observations (≥ 2 intervals). Pooled per domain or
  pattern, the rate from k observed changes has a relative standard error
  of 1/√k, so k ≥ 10 changes gives roughly ±60 % at 95 %. That is the
  loosest precision that still sets an interval within a factor of about
  2, the granularity a bounded min/max interval schedule can use.
- **Breadth.** A population of sources: 20 domains (80 % of the 25 seed
  domains that ever answered 2xx) with ≥ 30 attempts. Change evidence:
  ≥ 10 domains with pages observed ≥ 3 times, and changes on ≥ 5 domains.
  Changes must also appear in ≥ 3 page classes. Before a classifier exists
  the class proxy is URL path depth (0 = home, 1 = section/listing-like,
  ≥ 2 = detail-like). This proxy is crude and is stated as one.

### 3.2 Result

| # | Requirement (seed set) | Threshold | Observed (seed set) | Whole data | Result |
|---|---|---|---|---|---|
| G1 | domains with enough attempts for per-source proportions | ≥ 20 domains with ≥ 30 attempts | 11 | 26 | **FAIL** |
| G2 | sources seen in two windows ≥ 24 h apart | ≥ 20 domains with ≥ 30 attempts in both windows | 10 | 26 | **FAIL** |
| G3 | pages with change-rate exposure | ≥ 500 URLs with ≥ 3 HTML 2xx observations spanning ≥ 24 h, on ≥ 10 domains | 9 URLs (37 with any span, on 11 domains) | 1,030 | **FAIL** |
| G4 | observed changes across page classes | ≥ 10 changes in each of path-depth classes 0 / 1 / ≥ 2, on ≥ 5 domains | 8 / 264 / 45 changed URLs, 6 domains | — | **FAIL** (class 0) |
| G5 | change-signal precision | precision of `page.changed` ≥ 0.8 on a labelled M1 sample of ≥ 50 changed pairs, so the rate is overstated by ≤ 25 % | not measured; P5 measured 1/23 on W691 | — | **OPEN** |
| G6 | outcomes for every capability | each deployed capability ≥ 30 attempts on ≥ 5 domains; `needs_js` → browser escalation on ≥ 5 domains | http: 11 domains; browser: 1 domain (2 attempts); tor: not deployed | browser 20 domains, escalation 5,112 | **FAIL** (browser, tor) |
| G7 | target/temporal signal | ≥ 1 active target with `release_year` | 0 targets; no producer before P9 | 0 | **FAIL** (missing signal) |
| G8 | media baseline for M2 | ≥ 100 seed-set pages with media references | 127 | 703 | PASS |

Passed: G8. Failed: G1, G2, G3, G4, G6, G7. Open: G5. **The gate is not
passed, so P7 implementation is stopped** (plan §P7, "the gate is not
waived").

### 3.3 Why the gate fails

1. **Run length and the M1 revisit policy (G1–G4).** About 46 h in two
   windows, a full frontier and a 24 h revisit gate give few repeat
   observations.
2. **Budget spent off-target (G1–G4, G6).** 85 % of attempts went to NCBI
   and Scholar, reached through irrelevant Bing results. The seed set got
   13 % of the budget.
3. **The workload does not exercise the browser (G6).** Piracy seed sites
   are server-rendered. More M1 time on the same seeds would not produce
   browser outcomes for them. Browser learning data exists only for
   non-seed domains.
4. **Missing producers (G6 tor, G7).** No Tor daemon on the host, and no
   target manager before P9. No amount of M1 collection fixes these.
5. **Unlabelled change signal (G5).** Needs a labelling pass on M1 pairs.
   That is work, not collection.

### 3.4 Additional collection that would close G1–G4

> Superseded (D-12): this 4–5 day estimate was for M1b, which was rejected
> and not run. §5 shows the limit is the seed population, not collection
> time. Kept as history.

M1 gave the seed set about 700 attempts per hour (32,131 in ~46 h) while NCBI dominated.
If the same http capacity (4 slots, about 1.7 fetches/s) is spent on the
seed set, that rises to about 6,000 per hour. That is enough to revisit the
21,022 known seed-set HTML URLs about once every 4 h, and the 24 h gate
limits it to once a day. Three daily observations spanning ≥ 48 h then take
at least **3 days of crawling focused on the seed set**. Allowing for the
frontier refill and storage stalls on the USB HDD (P2, X-16), **4–5 days**
is the realistic estimate. This assumes:

- the existing frontier contents (200,000 tasks, mostly NCBI) are drained
  or the run starts from a fresh M1 keyspace and namespace;
- the operator excludes the off-target search roots with P6 operator rules
  (an existing mechanism, static policy, no intelligence), or search is
  disabled for the run;
- the P6 run-3 reliability items (X-8 byte-bounded, consumer-aware stream
  retention; X-11 relay retry) are in place, because a multi-day run
  without them risks another Redis-full stall.

Any of these changes the M1 configuration and needs an operator decision
(no new run without explicit approval). Continuing the old configuration
unchanged would mostly add NCBI history.

## 4. Other findings that affect P7 design

| # | Finding | Effect |
|---|---|---|
| A-1 | P4 §33a lists "P7 intelligence" as not in scope until the P4 success-metric decision (Option A/B) is made, and that decision is still pending | must be resolved or explicitly overridden before P7 implementation (decisions.md Q2) |
| A-2 | P1 already defines `crawl.requested` (crawl_intelligence → frontier: url, reason, priority, capability, not_before), and P3 maps it with `admission_from_request`. No process consumes it yet | the P7 → P3 path needs no contract change: P7 emits `crawl.requested` through its outbox and a frontier-side consumer admits it, or P7 calls `admit_many` the way P6 does. Choice in design |
| A-3 | P3 counts scheduled tasks against `max_depth`, and M1 hit the 200,000 cap | P7's recrawl calendar must live in P7-owned Scylla and be fed to P3 near its due time (P3 §10, §23) |
| A-4 | P3 already exposes `set_domain_interval` and `set_domain_inflight_limit` for P7. Per-domain retry/backoff is listed as P7 policy but has no API (P3 §14) | 429 back-off (X-14) can use the interval API. Per-profile retry policy would need a P3 change |
| A-5 | Escalation is `fail(next_queue=browser)` inside P4's static `decide()`, costing one attempt. A P7 profile can only choose the **starting** queue (`crawl.requested.capability` / `Admission.queue`) | fetch profiles act at admission, and P4 escalation stays as is. That matches B.5 #1 |
| A-6 | Browser use in M1 (2.1 % of attempts) is 85 % `nih.gov`, and the seed set has 2 browser attempts | a "≥ Y % fewer browser fetches than M1" M2 gate would be met by not crawling NCBI (a WHAT decision), not by fetch-profile learning. Y can't be justified as a fetch-profile measure on the seed set (stop condition 8, decisions.md Q4) |
| A-7 | Offline replay can only score URLs that M1 actually fetched, because unfetched pages have no observed content | offline evaluation can rank and schedule within M1's observed set. "More new pages under the same budget" needs a live A/B run of both policies (plan §P7 M2) |
| A-8 | 25 of 50 seed domains never answered 2xx | lifecycle "dead" is visible in the data; negative knowledge has real input |
| A-9 | Only 12 % of pages carry published/modified dates, there are 3 `video.movie` og types, and 13,008 paths contain a year | temporal relevance without targets would rest on URL years and dates on a minority of pages |

## 5. Reassessment of the seed set (2026-10-07, read-only; decision D-12)

Added after the gate result above. §1–§4 are unchanged. The question was
whether the existing M1 data is enough to start P7 without the M1b run
(benchmarks.md §2). Tool: `benchmarks/p7-intelligence/reassess_seed.py`,
results in `results/m1-reassess-seed.json`. It uses the same export and
reads nothing new from Scylla, Redis or MinIO.

### 5.1 Seed availability

**11 of the 50 seed domains are live** (≥ 50 HTML 2xx pages): 21,013 HTML
2xx pages in total. The other 39:

| Class | Domains | Evidence |
|---|---:|---|
| unreachable (timeout / DNS / connection only) | 24 | 3–18 attempts each, never a response |
| moved to a new mirror or parked | 7 | the response's final URL is on another registrable domain: isaimini.farm → isaimini.site, tamilprint7.art → tamilprint68.art, 1tamilmv.army → 1tamilmv.lease, tamilrockers.website → tamilrockerss.cfd, xn--3-… → xn--6-…, isaidub.love → isaiidub.com, moviesrace.xyz → expireddomains.com (parked). P6 does not root redirect targets (P6 limitation 4), so the mirrors were never crawled |
| a response, but ≤ 2 HTML pages | 7 | e.g. moviesda.info (304s), isaimini.spot (404s), tamilprinttv.com |
| only 4xx | 1 | moviesda.baby (404 × 9) |

| Live domain | Attempts run 1 / run 2 | HTML 2xx pages | ≥ 2 obs over ≥ 24 h | ≥ 3 obs | changed pages | change pairs (new URL revealed) | media pages |
|---|---|---:|---:|---:|---:|---|---:|
| isaimini.com.in | 5,112 / 315 | 4,801 | 94 | 1 | 87 | 93 (10) | 0 |
| movflix.my | 5,014 / 536 | 3,462 | 78 | 8 | 4 | 4 (0) | 0 |
| moviesda.com.in | 5,652 / 79 | 3,217 | 19 | 1 | 10 | 12 (3) | 0 |
| tamilprint.com.in | 2,501 / 286 | 2,529 | 202 | 1 | 202 | 202 (1) | 0 |
| stripemovies.com | 3,320 / 96 | 1,813 | 0 | 15 | 0 | — | 0 |
| moviesda.business | 2,729 / 433 | 1,640 | 13 | 1 | 13 | 13 (1) | 0 |
| isaiminimix.com | 1,455 / 1 | 1,408 | 0 | 0 | 0 | — | 127 |
| moviesda16.best | 2,768 / 45 | 1,192 | 1 | 1 | 0 | — | 0 |
| tamilrockers2024.com | 1,030 / 4 | 650 | 2 | 1 | 1 | 1 (0) | 0 |
| tamilprint.co.in | 234 / 4 | 215 | 2 | 1 | 0 | — | 0 |
| gofullmovies.com | 150 / 0 | 86 | 0 | 6 | 0 | — | 0 |

- **Revisits on the live domains collapsed in run 2.** 30k seed-domain
  attempts in run 1 against 1.8k in run 2. The frontier stayed full of NCBI
  tasks, so seed links could not be readmitted.
- Browser: 2 attempts (both captcha) and 2 `needs_js`, all on moviesda.com.in.
  No 429 on seed domains.

### 5.2 What the changes contain

- **325 `page.changed` transitions** on 6 seed domains. Only **15** of them
  linked to a same-site URL the crawl had not seen before. (An earlier ad-hoc
  count of 10 kept each revision's last extract instead of its first sighting.)
- tamilprint.com.in's 202 changes are on old detail posts
  (`/radhe-shyam-2022/`, `/purampokku-…-2015/`). In 164 of them the link set
  changed, yet only 1 revealed a new URL. That is consistent with rotating
  widgets.
- isaimini.com.in carries SEO-spam posts (casino, EPR registration) next to
  its film pages.
- WordPress detail posts sit at path depth 1, so **path depth does not
  separate listing from detail pages**. G4's class proxy is invalid for
  these sites.
- Of the 413 live-domain pages revisited between run 1 and run 2 (4 days
  apart), **8 revealed a new same-site URL**, 30 new URLs in total: about 2 %
  of revisits.

### 5.3 Gate reading (the definitions in §3 are unchanged)

| Gate | Formal status | What the existing data shows |
|---|---|---|
| G1 | FAIL (11/20) | The 11 are exactly the live domains. The dead ones would reach 30 attempts through about 2 more days of timeouts: a numeric pass with no new knowledge. The binding limit is seed availability. |
| G2 | FAIL (10/20) | Same. The live domain missing from run 2 (gofullmovies) was starved by the full frontier. |
| G3 | FAIL; capacity **not provable** | A pool of 21,013 HTML pages on 11 domains exists. ≥ 10 qualifying domains would need almost every live domain revisited ≥ 3 times, which M1 never did. Per-page rates on these sites would be ~0 anyway (§5.2). |
| G4 | FAIL; **proxy invalid** | Changes exist on 6 domains, but path depth is not a page class, and most changes are template churn. |
| G5 | OPEN → **FAIL (measured 2026-10-07)** | 325 pairs and their snapshots exist, so it was measurable without new collection. Precision 0.07 / 0.015 (g5.md §4) |
| G6 | FAIL for the seed set | Seed sites are HTTP-only. The 5,112 escalations on 20 off-target domains can test the HOW mechanism, but they are not seed-set evidence. Tor is deferred (D-7). |
| G7 | deferred (D-7) | no targets before P9 |
| G8 | PASS | 127 pages, all on isaiminimix.com |

**Conclusion.** The limit is the seed population, not collection time. A
longer run on the same 50 domains would mostly wait on dead domains, pass
G1/G2 numerically, and re-measure template churn. The existing data is rich
for HOW and negative knowledge (dead, moved, parked, 4xx, 304, 429), for
WHAT (21k pages on 11 live sites) and for lifecycle. The one missing
measurement is G5, and that blocks only the WHEN component.

## 6. Increment 2 architecture review (2026-10-07; decision D-18)

Earlier sections are unchanged. Chronology: M1 → gate not passed (§3) →
reassessment, M1b rejected (§5) → G5 FAIL ([g5.md](g5.md)) → design →
increment 1 → **this review** → increment 2 implementation
([implementation.md](implementation.md)).

**Question.** The design draft (D-17) proposed a P7 consumer group
`intel-sources` on `fetch.completed`, with groups named `intel-*` exempt
from ADR-022 retention. Is that safe under B.5 #1?

**What the code does** (`storage/events/publisher.py`, `relay.py`):

- `trim_floor` and the byte-budget check treat every group alike. A
  grouped stream whose unacknowledged entries fill `stream_max_bytes`
  raises `StreamBackpressureError`, and the relay ends its whole cycle at
  that stream.
- `fetch.completed` has **no** consumer group in the runtime. Only
  `page.observed` (extraction) and `urls.discovered` (discovery) are
  grouped. Its stream is length- and byte-trimmed (ADR-022 rule 3).

**Finding.** An ordinary P7 group would make `fetch.completed` a grouped
stream. A dead P7 would then stall publication of `page.observed` and
`urls.discovered`, which is the X-8/X-16 failure mode. The integration test
`test_an_ordinary_group_on_fetch_completed_would_stall_the_relay`
reproduces it on the real stack. The `intel-*` exemption would avoid the
stall, but only by adding a new group class to the P2 publisher. That
reader would still need gap detection on Redis IDs (reset by a flush) and
a durable rebuild source.

**Resolution (ADR-023).** P7 needs no group. It reads the outbox, which
already holds every `fetch.completed` envelope for 14 days, through P2's
read-only `OutboxStore` interface, with its own checkpoint. A gap exists
only after a 14-day outage, and it is rebuilt from the same source by the
same code path. ADR-022 is unchanged. Its interaction with P7 is removed
by construction, not by an exception.


## 7. WHAT data audit (2026-10-08; decision D-19)

Earlier sections are unchanged. Before WHAT was implemented, its inputs
were audited against P5/P6 (what.md §2):

- WHAT can run on existing P5 facts (`PageExtract`: final URL, anchor links
  with anchor text, title/og metadata, text size). **No interface
  changes**, and no P1/P5/P6 change.
- P5 persists no visible text. The labelling cards re-extracted it from
  the M1 snapshots, for evaluation only.
- Two gaps were documented and not fixed: Blogger titles are missing in
  P5 metadata, and P5 links carry no page region.
- The seed population behind WHAT is the 20,966 extracted URLs on the 11
  live seed domains (§5.1). The labelled sample is new and separate from
  G5 (what.md §3).

## 8. WHEN data audit (2026-10-08; decision D-20)

Earlier sections are unchanged. Before any WHEN work, M1 was audited for
hub revisits ([when.md](when.md) §5), read-only, from the export:

- 158 WHAT hubs were revisited (≥ 2 visits ≥ 6 h apart), 172 times.
  Nearly all of these are the single run 1 → run 2 revisit, because the
  run 2 collapse (§5.1) left only the seed roots on a 6 h cadence.
- 6 revisits expose new-since-previous-visit WHAT content links: 5 on
  moviesda.business and 1 on isaimini.com.in. All 6 are hubs of the G5
  sample.
- The data is **insufficient** (50 required) and **contaminated** for an
  independent WHEN sample. A hub-only collection is proposed, not run
  (when.md §7).
