# WHEN — readiness audit and offline experiment preparation

Status: **readiness work only (2026-10-08, decision D-20).** WHEN is **not
implemented** and stays gated (D-16, F-17). Nothing here adds a runtime
path, table, setting, consumer or interface. WHAT (`what/v1`) is used
unchanged as a frozen library.

Inputs: [design.md](design.md) §7, [what.md](what.md) §8, [g5.md](g5.md) §4.1,
[audit.md](audit.md) §5, [decisions.md](decisions.md) §4.
Tool: `benchmarks/p7-intelligence/when_audit.py`. Results:
`results/when-audit.json`, `results/when-hub-proposal.json`; per-event rows
in `var/p7/when/events.jsonl.gz` (git-ignored, regenerable).

**Result in one line:** M1 has **6** candidate hub revisits, on 2 domains,
all on hubs of the G5 sample (**0** outside it). The minimum is 50, so no
WHEN sample can be drawn from M1. A hub-only collection is proposed in §7;
its review rejects it on the current seeds (D-21, [when-w1-review.md](when-w1-review.md)).
**It was not run.**

## 1. The question

> When should a previously seen hub be revisited because it is likely to
> expose newly discovered content links?

G5 rejected raw `page.changed` (precision 0.07) and plain URL novelty
(0.067). The useful changes it found were new same-site links from hubs
into the site's content template. WHAT now supplies the hub/detail
distinction, the content templates and the link evidence. This document
asks whether M1 can validate a WHEN signal built on them.

## 2. Frozen inputs (unchanged by this work)

- WHAT `what/v1` and vocabulary `what-vocabulary/v1`: offline, deterministic,
  bounded, no storage, no Redis, no runtime hook, and no schedule, rate or
  priority (D-19, F-11). No WHAT rule, threshold or vocabulary term was
  changed for this audit.
- The WHAT labelled sample (`what/labels.csv`) and the G5 labels
  (`g5/labels.csv`) were **not read** by this work. The tool reads only the
  **URLs** of the G5 sample (`results/g5-sample.json`), to exclude them.
- M1 (`crawler2_m1`, the export in `var/p7/m1-export`) is read-only (F-12).
  The audit reads only the export. Nothing in Scylla, Redis or MinIO was
  touched.
- P1–P6, ADR-022 and ADR-023 are unchanged. Priority is unimplemented (F-18).

## 3. Definitions (fixed before the audit was run)

### 3.1 Observations, visits, hub revisits

| Term | Definition |
|---|---|
| observation | a `page_observations` row with HTTP 2xx and an HTML content type, keyed by its **final** URL, on the 11 live seed domains (audit §5.1), whose page version has a P5 extract |
| visit window | a URL's observations in time order, cut where an observation is ≥ **6 h** after the current window's first one. 6 h is the shortest P6 static revisit interval (seeds). Short-gap refetches (redirect variants, retries) merge into one visit, and their links count for it |
| hub | WHAT `kind = hub` on the URL's latest extract (as in the WHAT population). The kind at each window is recorded too: all 172 hub revisits were also `hub` at the window |
| hub revisit (event) | window *k* ≥ 2 of a hub, compared with windows 1…*k*−1 |
| interval | start of window *k* − start of window *k*−1 |

### 3.2 What "new" means

| Notion | Definition used | Where it is used |
|---|---|---|
| new to the hub | in the window's same-host anchor links, in **no** earlier window of the hub | every event |
| known before | first known on the site (observed, or seen as a link target of any page of the registrable domain) **at or before** the previous window's last observation. This is archive rotation, a re-sampled widget, or an item reached by another path | excluded from candidates |
| new since the previous visit (`new_since_prev`) | first known on the site **after** the previous window, but first seen on another page | candidate (credited to every hub, §6) |
| first seen here (`first_seen_here`) | this window is the site's first sighting. This is the G5 "first discoverer" signal | candidate; reported separately |
| new to the crawler | same as new to the site here: these are same-host links, and no off-target page in M1 is relied on for live-seed URLs | — |
| new to the content-template population | the link is in WHAT's `content_links` (an own, non-boilerplate link into a `content` or `mixed` template, without spam evidence) | candidate condition |
| duplicated URL | `variant`: a spelling of a URL known before the previous visit (case, trailing slash) | excluded |
| redirected URL | the URL was observed to redirect (`requested ≠ final`) | role `redirect`, excluded |
| widget/sidebar link | a WHAT boilerplate link (on ≥ 20 % of the site's pages) | role `boilerplate`, excluded. Widgets on fewer pages cannot be told apart without page regions (§3.4) |
| spam URL | WHAT link evidence: spam vocabulary or script, a spam template, or a pattern template without content evidence | role `other:spam` / `content_bad`, excluded |
| genuinely new content item | a content item newly **published or added** on the site since the previous visit | **only a human label can establish it** (§5.2). The candidate rule approximates it |
| merely changed markup | a new page version with no new-to-hub link | counted as `changed` without a new link |

### 3.3 Candidate (`when-candidate/v0`)

A **candidate link** of a hub revisit is new to the hub, `new_since_prev` or
`first_seen_here`, a WHAT content link, and not a variant or redirect. A
**candidate event** has at least one candidate link.

The rule uses **only the hub's own observation**, the site's history up to
that observation, and WHAT. It never uses the target page: at runtime the
target has not been fetched when the hub is seen. The target's WHAT
relevance (from its latest extract, if it was ever fetched) is reported as
evidence only.

**The eventual WHEN signal should use this definition**: new to the site
since the hub's previous visit, crediting every hub that shows it (not only
the first discoverer), restricted to WHAT content links. Whether it is
useful is decided by the labels, not by WHAT.

### 3.4 No page regions

P5 links carry no region (main list vs widget) and no document order
(what.md §2, gap 2). This audit therefore cannot tell a listing's own new
item from a "recent posts" widget that happens to show it, or from a
category listing that rotates old items. It relies instead on (a) WHAT
boilerplate, (b) site-level novelty, and (c) WHAT link evidence. Section 6
shows where this falls short. No P5 region field is added or proposed here.

## 4. Method

`when_audit.py audit` makes one pass over the observations and one over the
extracts. It builds WHAT site profiles from the latest extract per URL
(identical to the WHAT population), site knowledge per registrable domain,
the visit windows and the events. It runs in 39 s with a peak RSS of 445 MB
(offline, dev host; not a production figure). The output is byte-identical
with `PYTHONHASHSEED` 7 and 999. Bounds: links listed per event ≤ 60,
window times per hub ≤ 30. Counts are always complete.

`when_audit.py sample` draws the labelling sample (§5.3). It **refuses**
while fewer than 50 candidate events exist. `when_audit.py hubs` writes the
proposed collection population (§7). The tool reads another export through
`P7_EXPORT` / `P7_WHEN_OUT`, so the same code can audit the proposed
collection.

## 5. M1 audit results

### 5.1 Population (A–C)

| Measure | Value |
|---|---:|
| observations in the export (all hosts) | 215,492 |
| HTML 2xx observations on the 11 live seed domains | 23,927 (56 without an extract, skipped) |
| URLs observed | 20,966 |
| URLs observed more than once (**A**) | 2,318 |
| URLs with ≥ 2 visit windows (≥ 6 h apart) | 475: 158 hub, 292 detail, 25 other |
| WHAT hubs | 4,663 |
| hubs observed more than once (**B**) | 690 |
| hubs with ≥ 2 visit windows (**C**) | **158** (153 with exactly 2 windows; the roots of isaimini.com.in 8, moviesda.com.in 6, tamilrockers2024.com 4, moviesda16.best 3, tamilprint.co.in 3) |
| hub revisits (events) | **172** |
| candidate events | **6** (2 domains); 1 with a `first_seen_here` link |
| candidate events on hubs outside the G5 sample | **0** |

Most multiple observations are short-gap refetches. Examples: 1,410
stripemovies.com URLs observed more than once, all within an hour, and 467
HTML observations of the isaimini.com.in root, 461 of them reached by a
redirect from another URL. M1 revisited hubs
essentially once: the run 1 → run 2 gap (median interval 116 h, p90 133 h).
Only the seed roots were revisited at the 6 h seed cadence.

### 5.2 Per revisit (D, E)

Per-hub first and subsequent window times, page-version and revision
counts, and candidate counts are in `when-audit.json` → `hubs`. Per-event
rows (old/new link counts, appeared/disappeared, every new link's novelty
and role, anchors and target titles) are in `events.jsonl.gz`.

| Over the 172 hub revisits | Value |
|---|---|
| a new page version (`changed`) | 139 (81 %) |
| ≥ 1 link new to the hub | 35 (20 %) |
| links new to the hub | 156: known before 10 · new since previous 112 · first seen here 34 |
| roles of the 146 new (not known-before) links | **spam template 80** · content 45 · no template 19 · listing 2 |
| links appeared / disappeared vs the previous window | 157 / 264 |
| candidate links | 45 (28 distinct) in 6 events: 3–23 per candidate event |
| hubs with 0 / 1 / ≥ 2 candidate events | 152 / 6 / **0** |
| interval buckets | 6–24 h: 46 (0 candidates) · 3–7 d: 126 (6 candidates) |

| Domain | URLs | hubs | revisited hubs | hub revisits | 6–24 h / 3–7 d | changed | with a new-to-hub link | candidate events | candidate links |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| gofullmovies.com | 86 | 18 | 0 | 0 | — | 0 | 0 | 0 | 0 |
| isaimini.com.in | 4,783 | 466 | 13 | 19 | 8 / 11 | 19 | 8 | 1 | 23 |
| isaiminimix.com | 1,407 | 50 | 0 | 0 | — | 0 | 0 | 0 | 0 |
| movflix.my | 3,442 | 967 | 71 | 71 | 12 / 59 | 43 | 0 | 0 | 0 |
| moviesda.business | 1,632 | 1,305 | 13 | 13 | 0 / 13 | 13 | 13 | 5 | 22 |
| moviesda.com.in | 3,217 | 368 | 1 | 5 | 4 / 1 | 5 | 3 | 0 | 0 |
| moviesda16.best | 1,192 | 74 | 1 | 2 | 1 / 1 | 0 | 0 | 0 | 0 |
| stripemovies.com | 1,813 | 369 | 3 | 3 | 3 / 0 | 0 | 0 | 0 | 0 |
| tamilprint.co.in | 215 | 59 | 1 | 2 | 1 / 1 | 2 | 0 | 0 | 0 |
| tamilprint.com.in | 2,529 | 425 | 53 | 53 | 15 / 38 | 53 | 8 | 0 | 0 |
| tamilrockers2024.com | 650 | 562 | 2 | 4 | 2 / 2 | 4 | 3 | 0 | 0 |

What the new links are (from the event rows):

- **isaimini.com.in, moviesda.com.in, tamilprint.com.in roots, search and
  author pages:** 1–12 new posts per visit, all casino/betting/SEO
  (`/wolf-run-slots-at-playrand/`, `/nagapetir-login-…/`). WHAT puts them in
  the spam `/{slug}/` template, and **none became a candidate**.
- **moviesda.business:** 3 new films (`/2026/09/<film>-tamil-movie-download-….html`)
  on the root, the September archive, `/search`, `/?q=` and the "Tamil
  Movies" label, plus a new `/2026/10/` month archive. Two URLs (`sigma-movie-…`
  and `sigma-tamil-movie-…`) are one film under two URLs. Five candidate
  events. These are the hubs that G5 labelled useful.
- **isaimini.com.in `/tamil-hd-mobile-movies/`:** 23 content links, all films
  of 2018–2024, plus an SEO post with a year slug
  (`how-paten188-is-changing-the-game-in-2025`, WHAT limitation 4). The
  crawl had not seen these films before, so they count as "new since the
  previous visit". Their years suggest an old-item rotation rather than new
  publication. Only a label with the target's publication date can tell.
- **tamilprint.com.in, tamilrockers2024.com:** pagination links and items
  known before (`known_before`).
- **Non-hub revisits (317, detail/other):** 402 of the 433 new links are spam
  in widgets of old posts (the G5 pattern). The 2 candidate events are
  isaimini category listings that WHAT classified as detail
  (`/moviesda-dubbed-movies/`, `/tamil-movies-collection/`). They show old
  films as well.

### 5.3 Sufficiency (G)

| Requirement | M1 |
|---|---|
| ≥ 50 candidate hub revisits for labelling (design §7) | **6 → FAIL** |
| outside the G5 sample (independent of the held-out labels) | **0** |
| ≥ 3 domains with candidate events | 2 (one of them with 5 of 6 events) |
| repeated revisits per hub (needed for any rate) | 5 hubs with > 2 windows, all roots. No hub has 2 candidate events |

**No WHEN-labelled sample is drawn, and nothing is labelled.** The sampler
(`when_audit.py sample`) refuses. A dry run on M1 would give 79 events, all
negatives, because the G5 hubs are excluded. Even with enough events, M1 is
**contaminated**: all 6 candidates are G5 hubs, g5.md §4 describes them as
the useful ones, and WHAT was designed after reading that result. No
amount of re-slicing M1 removes this.

## 6. Signal findings (analysis only; not a policy)

| Property | What M1 shows | Estimable? |
|---|---|---|
| candidate event rate per hub | 6 / 172 revisits (3.5 %); 0 on 9 of 11 domains | only as a pooled rate; no hub has > 1 candidate event |
| useful event rate per hub | unknown without labels. At most the 5 moviesda.business events are newly published films (G5's reading of these same hubs) | **no** (no independent labels) |
| time between useful events | — | **no** (no hub has two) |
| revisits with no useful event | ≥ 166 / 172 (no candidate) | as an upper bound only |
| interval vs discovery | every candidate is in 3–7 d; the 46 revisits at 6–24 h have none. This is **confounded**: the short intervals are the roots of the spam-heavy sites | **no** |
| page kind vs discovery | hubs 6/172 candidate events (3.5 %); non-hubs 2/317 (0.6 %, both WHAT kind errors on listings). New links on non-hubs are 93 % spam | yes, qualitatively: change on detail pages is widget churn |
| content-template membership | separates the spam stream: 80 new spam-template links on hubs, 0 candidates. It does not separate rotation of old items (isaimini 23) or one-film-two-URLs | yes, qualitatively |
| per-domain variation, concentration | 5 of 6 candidate events and 5 of 28 distinct links on one domain; the 23 links on isaimini are one event | concentrated in 1–2 domains |
| simple deterministic rate model | a per-hub Poisson rate needs ≥ 2 exposures per hub with events. M1 has 1 exposure per hub. Pooled per site: moviesda.business shows ~4 distinct new films in ~5.4 days (~0.75/day) from one interval, with no variance | **not testable on M1** |

**Strongest evidence:** on the one site that published films during M1, all
its listing hubs showed the new films, and WHAT kept them while rejecting
all 80 new spam-stream links on hubs. **Weaknesses:**

1. one productive domain;
2. one revisit per hub;
3. site-level novelty also counts old items the crawl had simply not seen
   (crawl coverage ≠ publication);
4. one film under two URLs (URL identity is not item identity; media
   identity is P8);
5. WHAT kind errors on two listings and a year-slug SEO post.

### 6.1 Crediting every hub that links the new URL (§9 of the brief)

| Observation (28 distinct candidate links) | Value |
|---|---|
| credited to 1 hub / to ≥ 2 hubs | 23 / 5 |
| hubs per moviesda.business film | 3–5: root, month archive, `/search`, `/?q=`, label |
| first credited hub is the site root | 5 |
| pages linking each link in the latest extracts | 2–8 (median 4), all hubs; none is on ≥ 5 % of the site's pages (no widget-like link) |
| hub chains | home → month/label listing → detail on moviesda.business. All levels show the film within the same 6-hour window |

Assessment:

- **Crediting every hub is supported** for estimating a per-hub rate. G5's
  false negatives came from crediting only the first page the crawl fetched,
  and here 4 of the 5 moviesda.business hubs would otherwise get no credit.
- **It double-counts the item** if the credits are summed into a site value:
  22 candidate links on moviesda.business are 5 URLs and 4 films. Any value
  or budget figure must count **distinct items per site**, and the labels
  are per event, not per link.
- **Redundant hubs:** the moviesda.business hubs expose the same items, so
  revisiting all of them is 4–5 fetches for one discovery. Choosing among
  dominated hubs is a scheduling/priority question (design §8), not part of
  the signal. It is recorded, not designed.
- **Large archive hubs:** M1 shows no over-credit from old month archives
  (they did not change). The over-credit risk it does show is a category
  listing rotating old items (isaimini, 23 links). Telling a "latest posts"
  region from a rotating or "related" region would need page regions. That
  is a P5 extension and is **not proposed** here. Until then, the labels must
  judge it from the target's publication evidence.
- **Recommendation:** keep "credit every hub" in the candidate definition,
  report distinct items per site beside it, and measure the rotation
  failure through the `known_or_archive` label (§8). The design is not
  changed.

## 7. Collection proposal: hub-only revisit experiment "W1" (NOT RUN)

**Not executed. It needs the operator's explicit go (F-20) and a review of
the benchmark-script changes listed below.**

**Design review (2026-10-08): do not run W1 as written on the current
seeds** ([when-w1-review.md](when-w1-review.md)). M1 shows one active
film-publishing site family; 35 of the 84 hubs are historical archives;
the stop rule is inconsistent with the 40 % cap; events of one publication
are not independent. The required changes are listed there (§10).

| Item | Proposal |
|---|---|
| Objective | ≥ 50 labelled hub revisits with candidate events, independent of G5 and WHAT samples, at a fine, fixed revisit cadence that can be sub-sampled offline (6 h → 12/24/48 h) |
| Why M1 is insufficient | §5.3: 6 candidate events, 0 outside G5, 1 revisit per hub, 1 productive domain |
| Population | `results/when-hub-proposal.json` (`when_audit.py hubs`): per live seed domain, WHAT hubs (latest extract), not spam, not pagination, the bare root plus hubs with ≥ 3 content-template links, best 12 per domain. **84 hubs on 11 domains** (gofullmovies 3, isaimini 12, isaiminimix 3, movflix 12, moviesda.business 12, moviesda.com.in 12, moviesda16 4, stripemovies 5, tamilprint.co.in 5, tamilprint.com.in 12, tamilrockers2024 4). The operator adds the current and next month's date archive on sites whose content template is dated (`/{year}/{mm}/…`: moviesda.business, tamilrockers2024.com, movflix.my): ≤ 6 URLs. Fixed before the run, never changed during it |
| URL classes | root, category/genre/label listings, current date archives, search-root listings. No pagination pages, no detail pages |
| Capability | `http` (the seed sites are HTTP-only, audit §5.3 G6). P4 escalation unchanged |
| Revisit | every 6 h (`crawler2-discover seeds <hub file> --every 21600`, the existing P6 seed path) |
| Discovered links | **not admitted**: the `crawler2-discover admit` process is not started. P5 still extracts every hub. With no admit process, `urls.discovered` has no consumer group in the fresh namespace (the group is created by `admit`, `discovery/cli.py`), so under ADR-022 it is length-trimmed and cannot hold the relay. **Pre-run check:** `XINFO GROUPS <ns>:events:urls.discovered` is empty |
| Targets for label evidence | once, at the end: every distinct link new to a hub since its previous visit, fetched once as a one-off seed file (no `--every`), ≤ 3,000 URLs, so the labeller sees the target's title and publication date |
| Budget | ≤ 90 hubs × 4/day × 14 d ≈ **5,040 hub fetches** + ≤ **3,000** target fetches ≈ **8,000 fetches** (M1: 247,104 attempts). ≤ 48 hub fetches per domain per day. Raw bodies ≈ 1–1.5 GB in MinIO |
| Duration | minimum 7 days (a weekly publication cycle), maximum **14 days** |
| Expected yield (from M1 rates) | moviesda.business: ~0.75 new films/day × ~4 linking hubs ≈ 3 candidate events/day → its 40 % share of 80 (32) in ~11 days. The other domains published no film in 5.4 days of M1. **Reaching ≥ 3 productive domains is uncertain**, and that outcome is itself a result (§7.1) |
| Stop conditions | **success:** day ≥ 7 and ≥ 60 candidate events with ≥ 3 domains ≥ 10 each (checked daily with `export_m1.py --keyspace crawler2_m1w` + `when_audit.py audit`, read-only); **end:** day 14; **abort:** relay backpressure or a Redis-full stall (X-16), any domain returning 429/blocked on > 20 % of its fetches (back off: drop that domain), disk < 20 GB free, or an operator stop |
| Success criteria | the sample has ≥ 50 candidate events after the domain cap (§8, W1). If not, the conclusion is "the seed population cannot validate WHEN", which feeds seed refresh (D-15) |
| Data persisted | the standard fresh dataset: keyspace `crawler2_m1w`, Redis namespace `m1w`, bucket `crawler2-m1w` (`M1_DATASET=m1w benchmarks/p6-m1/run.sh setup`). Then the export (`P7_EXPORT=var/p7/m1w-export`), the audit, the sample and the cards. M1 is untouched |
| Labels afterwards | §8 label set, on cards from `when_audit.py sample` (no candidate flag, no WHAT role, no target relevance) |
| G5 isolation | events are observations made after the experiment start, so no G5 pair (M1 runs 1–2) can appear. G5 labels are not read. Hub URLs that are also G5 URLs are allowed (new observations, new labels), and their share of the sample is reported |
| P6 authority | seeds go through the normal P6 admission (filter, scope, revisit gate). No P6 rule, priority or setting changes. P7 advice stays off. No P7 component runs |
| M2 isolation | its own dataset, no M2 seed set, no A/B, no P7 influence on the crawl. Its results are not M2 evidence |
| Benchmark-script changes needed before it can run (to review, not made) | `run.sh` takes a fixed seed path and always starts `admit` (`search` only with a query file, which W1 does not pass). It would need `M1_SEEDS=<file>` and a "no admit" switch. No crawler code changes |

### 7.1 If W1 also falls short

If fewer than 50 candidate events, or fewer than 3 domains, are reached in
14 days, WHEN cannot be validated on the current seed population. The
result is reported as such, and the gate is not relaxed. Seed refresh is an
operator decision (D-15), not P7's.

## 8. Proposed WHEN acceptance gate

The labels, the candidate rule and this gate are committed **before** any
labelling. The precision requirement (≥ 0.8) is the existing one and is not
lowered.

**Label set** (one label per sampled hub revisit, with a one-line note):

| Label | Meaning |
|---|---|
| `useful_new_content` | ≥ 1 link new since the previous visit is a content item (film, series, song post) newly **published or added** on the site since the previous visit (the target's date, a new release, a post absent before) |
| `known_or_archive` | the new links are existing or older items: rotation, a "related/popular" selection, a second URL of a known item, a redirect |
| `spam_or_irrelevant` | the new links are only spam or unrelated posts |
| `no_new_items` | no new item: navigation, pagination, a new month archive link, or nothing |
| `ambiguous` | the evidence cannot decide |

| # | Criterion |
|---|---|
| W1 | ≥ 50 labelled candidate events (excluding `ambiguous`) from a dataset collected after this proposal, with ≥ 3 domains each contributing ≥ 5, and no domain > 40 % of the candidates (the sampler's cap). Plus ≥ 50 labelled non-candidate events |
| W2 | **precision ≥ 0.8** = `useful_new_content` / decided candidate events, on the holdout split. The Wilson 95 % interval is reported (at 40/50 it is about 0.67–0.89); the gate is the point estimate, as in G5 |
| W3 | ≥ 40 `useful_new_content` candidate events (so the gate cannot pass on a handful) |
| W4 | per-domain robustness: precision ≥ 0.8 still holds with the largest domain removed. Per-domain precision is reported for every domain with ≥ 5 events |
| W5 | abstention: `ambiguous` is excluded from W2 and reported. If it exceeds 15 % of candidate events, the gate is **undecidable** until a second labeller resolves them |
| W6 | recall is **estimated, not gated**: useful non-candidate events in the negative sample, weighted by the non-candidate population, with an interval. M1 cannot bound the base rate, so no recall threshold can be justified yet. A threshold may be set after W1's measurement and before any runtime design |
| W7 | contamination and circularity: G5 and WHAT labels are not used. Cards show no candidate flag, WHAT role or WHAT relevance. Labels are judged from the target's own evidence (title, date, anchor), not from WHAT. The candidate rule (`when-candidate/v0`) and WHAT (`what/v1`) are frozen before labelling |
| W8 | temporal separation and splits: dev/holdout by `sha256("p7-when-split/v0"|event id)`. If the rule is tuned on dev, it gets a new version, and the holdout is scored once afterwards. Holdout labels never tune |
| W9 | reproducibility: the audit and sample are byte-identical across `PYTHONHASHSEED`. Salts and versions are recorded in the results |

**False-positive cost.** A false candidate inflates a hub's rate and moves
revisits to it. It is bounded by any interval floor, but it is systematic
on the spam-stream sites (isaimini.com.in publishes several spam posts a
day), which is why precision is gated. **A false negative** delays a
discovery until the P6 static revisit, which stays the floor. Its cost is
bounded by the P6 interval (6 h seeds, 24 h links).

## 9. WHAT/WHEN boundary

| Question | Owner |
|---|---|
| page kind, URL template, content templates, link evidence, page relevance, a hub's current content links | **WHAT** (`what/v1`) |
| is this hub worth revisiting, and when (a rate over its candidate events) | **WHEN** (not implemented) |
| filter, scope, revisit gate, admission, priorities | P6 |
| scheduling, queues, leases, politeness | P3 |
| target relevance, release timing | P9 (deferred, D-7) |
| item identity (two URLs for one film) | P8 media identity (deferred) |

- **Check of the current design.** design §7 uses WHAT's kind, content
  templates and spam evidence without redefining them, and leaves detail
  pages on the P6 static rule. It does not replace P6 filtering. It does not
  touch Redis, frontier priority, target relevance or fingerprints.
- **One open point (documented, not resolved):** design §7 ends with an
  interval `clamp(k/λ̂, min, max)`, and design §3 lists `crawl.requested`
  as a P7 output. How a WHEN interval would reach the crawler (advice that
  P6 applies at its revisit gate, as for source advice, or a request to P3)
  is not designed. Under F-5/F-8, P6 keeps revisit authority and P3
  executes. WHEN must not schedule crawls directly. This belongs in WHEN's
  own design review, after W1–W9 pass.
- The audit tool calls only the public WHAT API. Its own link
  classification (novelty, redirect, variant) concerns history, which WHAT
  does not have, and it changes no WHAT result.

## 10. Limitations

1. The WHAT profiles are built on the latest extract of every URL, so they
   use pages observed after some events. That is acceptable for an offline
   audit. A runtime WHEN would need an as-of profile (what.md §8).
2. Site knowledge is the crawl's knowledge. An item can be "new since the
   previous visit" only because the crawl never reached it before (§6,
   weakness 3).
3. 6 h windows: two fetches less than 6 h apart are one visit, so changes
   within 6 h are not resolved. The proposal fixes the cadence at 6 h for
   that reason.
4. The visibility of new items through widgets on detail pages is not
   attributed to hubs. Detail pages stay on the P6 rule (design §7).
5. One labeller (the implementer) would label, as for WHAT; W5 asks for a
   second labeller only for ambiguity.

## 11. Status

- **No runtime WHEN,** no revisit policy, no Poisson scheduler, no priority,
  no Redis consumer, no table, no setting, no interface. P1/P3/P4/P5/P6,
  ADR-022 and ADR-023 are unchanged. WHAT is unchanged.
- **No ADR is required:** no architectural decision was taken. The
  collection proposal and the gate are recorded as decision D-20.
- **Next:** W1 reviewed: not to be run on the current seeds
  ([when-w1-review.md](when-w1-review.md) §10). A WHEN collection needs a
  seed population with ≥ 3 independent publishing site families first
  (operator, D-15). Until then, WHEN stays gated.
