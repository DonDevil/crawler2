# P5 Decisions, limitations and deferred work

## 1. Decisions

| # | Decision | Where recorded | Reason |
|---|---|---|---|
| D-1 | "Page version" in the P5 sense is an additive **page revision** (`PageRevisionId`); P1 `PageVersionId` keeps its exact-bytes meaning | [ADR-020](../../adr/ADR-020-page-revisions.md), approved 2026-09-29 | a frozen ID derivation and a field's meaning may not change (B.5 #5); P4 must not parse |
| D-2 | Benchmark and precision corpus = HTML of the W691 URLs captured twice, 3 h apart, kept out of git | design §18, approved 2026-09-29 | V1 and P4 kept no bodies (audit §5) |
| D-3 | `page.changed` is new (contract 1.1, producer `extraction`, key `revision_id`); no `page.fetched` | ADR-020, P1 events.md | P1 deferred it to P5; `page.observed` already states "fetched and recorded" |
| D-4 | Revision identity is content-derived; a revert re-sights the old revision | ADR-020 | race-free convergence without LWT; transition counting needs ordered allocation |
| D-5 | Only 2xx HTML observations are extracted | design §1 | 4xx/5xx/blocked bodies (error and challenge pages) would pollute link sets and revisions; their status is already a fact in W5/`page.observed` |
| D-6 | Links are written once per raw version; `media.discovered` once per observation | design §14/§21 #1 | identical bytes have identical links; P8 wants per-observation sightings |
| D-7 | selectolax (lexbor) is the parser | audit §4 #7 | no compatibility obstacle; measured 0.7 ms/page parse on the corpus |
| D-8 | Baseline normalization removes only invisible content and landmark boilerplate (`nav`, `footer`, `aside`, ARIA landmarks); P6 rules plug in with a new scheme name | design §9, `NormalizationRules` | P6 owns ad/tracker policy; the scheme is part of the revision identity |
| D-9 | The normalized form includes the media set but not the link set | design §9 | a swapped video is meaningful; ad hrefs rotate |
| D-10 | Archival: decide and record, never delete; evidence/high-value flags come from the caller | design §13 | P7/P12 own value and evidence; GC waits for ADR-005 |

## 2. Known limitations

1. **Boilerplate outside landmarks.** Rotating widgets rendered as plain
   `<div>`s in the main region ("trending", counters, relative timestamps
   such as "5 minutes ago") change the normalized hash until P6 supplies
   rules. Measured ([validation.md](validation.md) §4): on the real pair
   sample precision was **1/23**, all 22 false positives from one site's
   Elementor "latest posts" loop grid inside `<main>`. Remedy: P6 selector
   rules (`NormalizationRules.extra_selectors`, new scheme) or site-template
   learning (P6/P7); deliberately not tuned on the evaluation set.
2. **Reverts are not new revisions** (ADR-020): A → B → A emits
   `page.changed` for A and B only.
3. **Event attribution under unordered delivery**: `page.changed` names
   the first *processed* sighting; `first_seen` must be read from the
   revision row (design §21 #4). A concurrent first sighting may emit two
   events with the same idempotency key.
4. **`304 Not Modified` produces no observation** (P4), so it does not move
   `last_seen`.
5. **JavaScript URLs**: only literal syntax (design §7); concatenation,
   templates, obfuscation and external scripts are invisible to P5.
   Browser-rendered HTML (P4 browser pool) already contains the DOM after
   script execution, which covers much of this for pages fetched that way.
6. **Metadata dates** without an offset are read as UTC; non-ISO dates are
   dropped.
7. **Normalizer upgrades** create a new revision for every page on its next
   sighting (expected, one `page.changed` each).
8. **Media on the benchmark corpus**: the W691 pages are mostly portal and
   listing pages with almost no player markup (V1 and V2 both extract 0
   media references there), so media extraction is validated by fixtures,
   not by the real corpus.
9. **Change-detection set is small** (see validation §4); no statistical
   significance is claimed.

## 3. Deferred (owner phase)

| Item | Owner | Hook left by P5 |
|---|---|---|
| ad/tracker/blacklist filtering of links and of normalization | P6 | `LinkPolicy`, `NormalizationRules(scheme, extra_selectors)` |
| tracking-parameter / mirror equivalence as relations | P6/P7 | links keep exact canonical URLs |
| value model (`high_value`), recrawl, priority, change-rate learning, W15 | P7 | `ArchivalProfile`, `page.changed`, `page_revisions_by_url` |
| manifest (HLS/DASH) parsing, media identity, probing | P8 | `media.discovered`; V1 parser to port |
| evidence candidacy input, finalization | P12 | `ArchivalProfile.evidence_candidate`, `snapshot_retention` |
| snapshot garbage collection | P13/P14 after ADR-005 | `snapshot_retention` (keep if any row retains) |
| `DiscoveryMethod.AUDIO_ELEMENT` | P8 if needed | consumer-first enum minor (ADR-009) |
