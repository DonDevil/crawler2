# W1 design review

Status: **review only (2026-10-08).** W1 ([when.md](when.md) §7) has **not
run**. Nothing was collected, no script was changed and M1 was only read.
This note reviews whether W1, as written, can produce the independent
evidence that the WHEN gate (when.md §8) needs.

**Verdict: D. Do not run W1 on the current seed population.** M1 shows
at most **one independent film-publishing site family** active during and
before M1. The gate needs ≥ 3. W1 would cost about 8,000 fetches and 14 days
to reach the failure that §7.1 already anticipates. W1 also has design
defects (§3–§8), which must be fixed before any future WHEN collection.

Inputs: `var/p7/m1-export` (read-only), `results/when-audit.json`,
`results/when-hub-proposal.json`. These are unchanged. Every number below
is recomputed from them. "Dated" means P5 `metadata.published_at`, or the
`/{year}/{mm}/` segment of a Blogger post URL.

## 1. State confirmed

- **WHAT:** frozen as `what/v1`.
- **G5:** failed, and its labels are held out. This review did not read
  them.
- **WHEN:** gated. There is no runtime WHEN and no Priority implementation;
  the only "WHEN" mentions in `crawler2/` are two docstrings in `what.py`.
- **M1:** read-only.
- **W1:** not run.
- **P1–P6, ADR-022 and ADR-023:** unchanged.
- **Audit figures re-verified from `when-audit.json`:**
  - 172 hub revisits;
  - 35 with a new-to-hub link;
  - 6 candidate events, 0 of them outside G5, 5 on moviesda.business;
  - 0 hubs with two candidate events.
- **The 22 moviesda.business credits** (5 + 5 + 5 + 4 + 3) are 5 URLs and
  4 films (sigma has two URLs).
- **The 23 isaimini.com.in links:** P5 dates 22 films to
  2024-05-15…2024-05-23 and one SEO post to 2025-10-11. None was published
  during M1.

## 2. Seed population: who publishes? (decisive)

| Site family (evidence of shared operator) | Domains | Latest dated post in M1 | What was published Aug–Oct 2026 |
|---|---|---|---|
| A: author `sher-ali` on all three; 1,246–1,762 shared URL paths per pair | isaimini.com.in, moviesda.com.in, tamilprint.com.in | 2026-10-04 | SEO and casino posts only (e.g. `top-8-aws-migration-companies-2026/`). **No film** since 2024-05 |
| B: same films in the same months (`vishwanath…`, `im-game`, `the-paradise`) | moviesda.business, tamilrockers2024.com | 2026-09 / 2026-10 (URL) | films: ≥ 10 and ≥ 6 dated URLs since August (lower bounds: M1 did not fetch all details) |
| C: identical latest posts (`tron-ares`, `wicked-for-good`, `zootropolis-2`) | gofullmovies.com, stripemovies.com | 2025-12-05 | none |
| movflix.my | 1 | 2026-07-23 | none (1,754 dated pages) |
| isaiminimix.com | 1 | 2024-10-01 | none |
| tamilprint.co.in | 1 | 2026-01-26 (spam) | none |
| moviesda16.best | 1 | no dates in M1 | **unknown** |

Of the 11 domains, only family B published films during or just before M1.
The 3 domains of family A publish several SEO posts a day. Some have year
slugs (`…-2026/`), the WHAT limitation 4 pattern, so they can become
candidates that are never useful. The data is consistent with
`when-audit.json`: every useful-looking candidate is on family B, and
family A gives spam-stream churn. M1 cannot predict the next two weeks.
moviesda16.best and movflix.my may still publish. But reaching
≥ 3 *independent* productive families would require at least two dormant
or unknown sites to start publishing, and nothing in M1 supports that.

**Consequence for the gate.** The rule "≥ 3 domains with ≥ 5 candidates"
can be met by mirrors. Family B alone covers 2 domains, and family A's
spam slugs could add a third. So "3 domains" does not guarantee three
publication processes (§6). With the current seeds, W1 most likely ends in
one of two ways:

- §7.1 ("the seed population cannot validate WHEN"), which this review
  already supports with M1 evidence; or
- a nominal domain pass that rests on one publication process.

Neither result is the independent evidence WHEN needs.

## 3. Population (`when-hub-proposal.json`, 84 hubs)

Ranking by content-template link count ("best 12 per domain") selected
high-degree *historical* listings, not likely producers:

| Class among the 84 | Count | Examples |
|---|---:|---|
| closed post-date archives (can only gain back-dated posts) | 12 | moviesda.business `/2025/11/`…`/2026/07/` (9 of its 12 slots), movflix `/2025/12/`, `/2026/02/`, `/2025/` |
| past release-year categories | 23 | `/category/2016/`…`/2024/` on the three family-A domains (9 + 7 + 7) |
| the same page under two spellings | 4 pairs | gofullmovies and stripemovies `/?s` and `/?s=`; moviesda.business `/` and `/?m=1`; tamilrockers2024 `/search/label/Tamil` and `…?max-results=20` |
| family-A domains | 36 of 84 | one operator, no film output |
| roots with 0 content-template links | 5 | still useful as spam-stream negatives |

- **A. Selection bias toward candidates?** No. The bias runs the other way.
  On the productive domain, 9 of 12 slots are closed month archives. Two
  of the five productive M1 hubs (`/search`, `/?q=`, both root-equivalent)
  are excluded as search roots, while `/?m=1`, another root spelling, is
  included. Yield would be underestimated and the negatives inflated by
  archives.
- **B. High-degree bias?** Yes. Full historical pages (10–15 links)
  outrank the live root (5–7).
- **C/D. Redundancy.** Several hubs exposing one publication (root,
  current archive, label) is *wanted* for per-hub WHEN behaviour, but those
  events are not independent (§6). Spelling duplicates add no information
  and should be removed.
- **E. Excluded classes.** Listings that WHAT calls `detail`
  (`/moviesda-dubbed-movies/` on isaimini) are correctly out of scope
  (detail stays on the P6 rule). The current and next month archives are
  left to the operator. They should be in the fixed file, because they
  are the productive class on dated sites.
- **F. Diversity.** By domain, 11 nominal. By publication process, 1 known
  and 1–2 unknown. By hub type, dominated by year/month archives.
- **G. Random component.** Use one, but not as a fix on its own. A
  deterministic stratified draw is better (fixed before the run, hash
  salt recorded):
  - per domain: the root (one spelling), the current and next month
    archives where dated, 1–2 hubs per distinct listing template
    (category, label, author, collection `/{slug}/`), and at most 2
    historical archives as rotation controls;
  - the remaining slots drawn by hash from the eligible hubs, not by
    degree.

## 4. Cadence: keep 6 h as the *measurement* cadence

- **Item dwell time.** The moviesda.business root lists 5–7 items. At the
  M1 rate (~0.75 films/day, one interval) an item stays about a week, and
  labels and categories hold 10–20. A 24 h cadence would not *miss* items
  on these hubs, so 6 h is not needed for that.
- **Why 6 h is still right.** It is the finest cadence the P6 seed path
  supports, and it can be sub-sampled offline (12/24/48 h windows rebuilt
  from the same observations). The reverse is impossible.
- **Cost.** 4× the hub fetches of 24 h: 5,040 vs 1,260, about
  48/domain/day. This is small next to M1's 247,104 attempts.
- **Dependence.** The real dependence is across hubs (one item, several
  hubs), not across consecutive windows (§6).
- **Required clarification.** The labelled and gated unit is the **native
  6 h revisit**. Sub-sampled cadences are analysis only. At coarser
  cadences, events merge and event-level precision changes, so a cadence
  must not be chosen after labelling.

## 5. Candidate definition and target evidence

- **The rule.** `when-candidate/v0` is the right thing to *collect*: it uses
  only what is known when the hub is seen. Its known weakness, site-level
  novelty ≠ publication, is a labelling problem, not a collection problem.
- **What existing P5 data covers.** On family-A-type WordPress sites,
  `published_at` covers 94–98 % of extracts. Those dates cleanly identify
  the old-item cases: the isaimini 23 are `known_or_archive` from the
  date alone. Coverage is 78 % on stripemovies, 68 % on tamilprint.co.in,
  51 % on movflix, 43 % on gofullmovies, and **0 %** on moviesda.business,
  tamilrockers2024 and moviesda16 (Blogger).
- **What labelling would rely on, by case:**

| Case | Evidence available with existing P5/MinIO | Sufficient? |
|---|---|---|
| target has `published_at` | the date vs the hub's previous visit | yes (a re-upload with a new post date counts as "added", per the label definition) |
| Blogger, no date | URL `/{year}/{mm}/` (month only), plus the **hub timeline**: the item is absent from the root/current archive in all earlier W1 windows, then appears | yes, if the label uses the timeline. Month alone is not enough |
| no date at all | hub timeline and title only | weak, so `ambiguous` |
| target removed by the end | nothing, if fetched only at the end | **lost** |
| target changed after first exposure | the end-of-run version only | partly lost (title, date) |
| old URL newly linked (rotation) | `published_at` or URL month older than the previous visit | yes where dated |
| two URLs for one film, spam, SEO, widget | title, anchor, date, the existing WHAT boilerplate | yes, by a human. Item identity stays P8 |

- **Missing evidence (no P5 change proposed):**
  - **link position in the hub page.** The raw hub body is already in
    MinIO for every fetch, so the card can show the archived hub HTML
    around the link. That is a labelling-tool change, not a P5 change.
  - **the hub timeline per link** (first and last window in which each hub
    showed it). The audit already computes this; the card should show it.
- **Target fetch timing.** Fetching once at the **end** loses removed and
  changed targets. It also lets a day-14 page date stand in for day-3
  evidence. The smallest fix keeps the same budget (≤ 3,000 distinct
  targets, each fetched once) but fetches each new target within
  **≤ 24 h of its first exposure**: a daily one-off seed file built from
  the daily read-only audit that W1 already runs for its stop check. Do
  not refetch targets on every revisit.

## 6. Credit every hub, and independence

- **Two separate uses:**
  - *per-hub informativeness:* crediting every hub is correct. G5's miss
    came from crediting only the first discoverer.
  - *site publication rate:* this must count distinct items per site.
    when.md §6.1 already says so.
- **What is not covered.** The gate counts **events** (W1 ≥ 50, W3 ≥ 40)
  as if they were independent. In M1 the 5 moviesda.business candidate
  events are **one publication cluster** (the same 3–4 films on 5 hubs),
  so M1's effective sample is 2 clusters, not 6. In W1, one film on
  family B could produce about 6 events (root, current archive, label on
  2 domains). Fifty events could then be about 10 publications on one
  family.
- **Required change (gate, before labelling):**
  - report the number of **distinct publication clusters**: candidate
    events of one site family whose candidate links share an item URL;
  - require W1/W3 to hold with **≥ 25 distinct useful clusters**;
  - add a **cluster check** to W4: precision ≥ 0.8 still holds when each
    cluster is counted once.
- This does not choose which redundant hub "wins" (Priority) and does not
  lower 0.8.

## 7. Gate arithmetic (W1–W3 and the stop rule)

| Check | Finding |
|---|---|
| sample 80 candidates, 50/50 hash split | holdout ≈ 40 decided events. W2 is scored on the holdout. W1 (≥ 50) and W3 (≥ 40 useful) do not say which split. **On the holdout alone, W3 needs ≥ 40/40.** That is not intended |
| 40 useful of 50 | point 0.80, so it passes W2 exactly. Wilson 0.67–0.89; on a 32/40 holdout 0.65–0.90. The gate is the point estimate, as in G5, unchanged |
| W1/W3 vs the 40 % cap | consistent: 50 decided with ≤ 20 from the largest domain needs ≥ 30 from the others |
| **stop rule vs W1** | **inconsistent.** "≥ 60 candidates, ≥ 3 domains ≥ 10 each" can be met by 40 + 10 + 10, but under the 40 % cap that gives a sample of at most ~33 (x ≤ 0.4 (20 + x)). W1 could stop as a "success" and fail W1 |
| ≥ 50 non-candidate events | trivially met (the 3 family-A roots alone give 168 windows) |

**Smallest changes:**

- **W1, W3:** count dev + holdout together. W2 and W4 stay on the holdout.
  The holdout must hold ≥ 25 decided candidate events, so the sample
  grows if needed. Alternatively, if `when-candidate/v0` is never revised
  on dev, all labelled events are holdout-equivalent and W2 may use all of
  them. That choice must be fixed before labelling.
- **Stop rule:** success when, after the 40 % cap, ≥ 60 candidate events
  remain, with ≥ 3 **site families** each ≥ 10, and ≥ 25 distinct
  clusters.
- **Domains:** for the ≥ 3 requirement, the cap and leave-largest-out (W4),
  count **site families** (§2), fixed before the run from M1 path and
  title overlap.
- None of these lowers 0.8 or relaxes the 3-source requirement. They make
  it mean independent sources.

## 8. Rate estimation (what W1 could support)

- **Per hub.** 6 h × 14 d gives 56 windows, so 55 revisit intervals
  (27 at 7 d).
- **Productive family.** At ~0.75 films/day and ~3 linking hubs per
  domain, each hub would get about 8–10 candidate events. That is enough
  for a per-hub candidate-event rate and the time between events, on
  **that family only**.
- **Silent hubs.** 0 events in 55 intervals gives only an upper bound,
  about 5 % per 6 h (rule of three).
- **Useful-event rate.** Not per hub: the sample labels about 80 events
  across all hubs. Only pooled (precision × candidate rate) per family.
- **Variation.** Between hubs, only within the one productive family, and
  correlated. Between domains or families, **not estimable** (1 known
  producer).
- **Conclusion.** Even if run, W1 could estimate one family's hub rates. It
  could not validate a rate model across sites.

## 9. Information per fetch

- **Fixed vs staged.** The fixed design is preferable to staged or
  adaptive collection. A low-frequency stage followed by "focus on
  productive hubs" selects on the outcome and biases per-hub rates
  (regression to the mean). It also edges into Priority. The fixed
  6 h cadence costs only about 3,800 hub fetches more than 24 h.
- **The real waste is the population.** Thirty-five historical archives
  plus 4 spelling duplicates are about 2,200 of the 5,040 hub fetches.
- **Seeds.** Running on seeds where 1 of about 7 families publishes buys
  little information.
- **Cheapest decisive step before any WHEN collection.** Decide, from
  publication evidence, whether the seed set has ≥ 3 independent actively
  publishing film families. For the current seeds M1 already answers: no
  (§2).

## 10. Decision and what is required before another WHEN collection

**D: do not run W1.** On the current seeds, M1 shows one active
film-publishing family (B). Of the other 9 domains:

- the three family-A domains publish SEO/spam only;
- 5 domains have been dormant since 2024-10…2026-07;
- moviesda16.best is unknown.

W1's gate requires ≥ 3 independent sources and cannot get them from this
population. That is when.md §7.1's failure outcome, already supported
offline.

**Required before another WHEN collection** (operator decisions; nothing
here is implemented):

1. **A seed population** (seed refresh, D-15, an operator decision outside
   P7) with ≥ 3 independent site families that published film items within
   the last 30 days, as evidenced before the run (dated posts on the root
   or current archive). Mirrors count once.
2. **Hub selection** per §3:
   - stratified by template;
   - one root spelling, with spelling duplicates removed;
   - current and next date archives in the fixed file;
   - ≤ 2 historical archives per domain;
   - the rest drawn by hash.
3. **Targets fetched once within ≤ 24 h of first exposure** (§5), same
   budget.
4. **Cards show** the hub timeline per link and the archived hub HTML
   around the link (§5).
5. **Gate W1/W3/W4 and the stop rule** amended per §6–§7:
   - site families, not domains;
   - distinct clusters;
   - split accounting;
   - a stop rule consistent with the 40 % cap.

   Precision stays ≥ 0.8.
6. The run.sh changes already listed in when.md §7 (seed-file option and
   a no-admit switch), reviewed before any run.

WHEN stays gated.
