# WHAT — page kind, URL templates and page relevance (P7 increment 3)

Status: **implemented as an offline library and evaluated (2026-10-08,
decision D-19).** Not wired to any runtime path. WHEN stays gated (D-16).

Inputs: [design.md](design.md) §6, [g5.md](g5.md) §4.1, [audit.md](audit.md) §5.
Code: `crawler2/intelligence/what.py`. Vocabulary (operator data):
`crawler2/intelligence/data/what_vocabulary.toml`.

## 1. Problem

G5 showed that neither raw `page.changed` nor plain URL novelty is a usable
recrawl signal on the seed set (precision 0.07 and 0.067). The useful
changes were new links from **hub** pages to pages of the site's **content
template**. Spam shares the WordPress `/<slug>/` template with films on
two sites. So WHAT answers three questions, deterministically and with
reasons:

1. Is a page a **hub** (home, listing, category, archive, search, author
   page), a **detail** page (one item: a film, song or series post, or a
   spam post), or **other** (utility, login/verification, empty or script
   responses)?
2. Which **URL templates** of a site hold its content pages?
3. Is a page **relevant** to the site's intended content, or
   **spam/irrelevant**? If the evidence is insufficient, it answers
   **unknown**.

WHAT produces features for a future WHEN: page kind, template and role, the
content-template links a hub carries, the relevance decision, reason codes,
and the policy and vocabulary versions. It produces no schedule, rate or
priority.

## 2. Data audit (what WHAT can consume today)

| Input | Where it exists | Used by WHAT |
|---|---|---|
| final URL | P5 `PageExtract.page`; `page_extracts.doc`; export `url` | tokenization, host scope |
| anchor links (target, anchor text) | `PageExtract.links` (`DiscoveredLink`: target, relation, anchor_text ≤ 300, nofollow) | page-specific links, link evidence |
| title, og_title, og_type, published_at | `PageExtract.metadata` (`PageMetadata`) | title evidence, detail hints |
| text size | `PageExtract.stats.text_chars` | empty-page test |
| media references | `PageExtract.media` | carried in `PageFacts.media`, not used by a rule (127 pages, one site) |
| lang, canonical, modified_at, robots | `PageMetadata` | not used |
| revision id, hashes | `PageExtract.revision_id`, `hashes` | not used (WHEN's concern) |
| visible/normalized text | **not persisted** by P5 (only hashes) | not used at runtime. The label cards re-extracted it from MinIO for the labeller only |
| host | P1 `domain_id` (host) | profile scope |

- **Interfaces: no change needed.** WHAT reads `PageExtract` (runtime,
  through `facts_from_extract`) or the same fields from the M1 export
  (`facts_from_export`). P1 contracts, P5 models and tables are unchanged.
- **Gaps found (documented, not fixed):**
  1. Blogger pages (moviesda.business) have `title = None` in P5 metadata,
     although the page shows one. WHAT falls back to the slug and to
     anchors, which works there. A P5 title fix would need its own P5
     decision.
  2. P5 links carry no document order or page region (main list vs
     widget). WHAT cannot tell a listing's items from a "recent posts"
     widget, so a film-named listing whose items are spam can look
     relevant (two dev errors, §5.3). A region field would be a P5
     extension, which is not proposed now.

## 3. Labelled sample (not G5)

`benchmarks/p7-intelligence/what_sample.py`, committed with the labels in
`d699b94`, **before any rule existed**.

- **Population:** the 20,966 URLs with a P5 extract on the 11 live seed
  domains (audit §5.1). The latest extract per URL is used.
- **Sample:** 26 pages per domain (286 in all). Strata are domain × a
  coarse URL shape (a stratification key only, not the WHAT tokenizer).
  Half of each domain's quota is allocated in proportion to shape
  population (largest remainder). The other half is round-robin over
  shapes, so homes, searches, archives, author pages and scripts are
  covered. Within a shape, pages are taken in `sha256("p7-what-sample/v1|" + url)`
  order. **The 65 URLs in the G5 sample are excluded.**
- **Split:** `holdout` when `sha256("p7-what-split/v1|" + url)` is odd,
  otherwise `dev`. That gives dev 142 and holdout 144.
- **Evidence for the labeller:** a card per page with the URL, metadata,
  same-site links and anchors, external hosts, and the first 900
  characters of the P5 normalized main text. The text was re-extracted
  read-only from the M1 snapshot in MinIO (`crawler2-m1`), and the raw
  digest was verified. Cards live in `var/p7/what/cards` (git-ignored). The
  card showed no WHAT feature or decision. Labels map to evidence by
  sample id → URL → `page_version_id` in `var/p7/what/pages.jsonl.gz`.
- **Labels** (`benchmarks/p7-intelligence/what/labels.csv`, with a
  one-line note each):

| Field | Values | Definition |
|---|---|---|
| `page_kind` | `hub` | the page's purpose is to list or navigate to other pages: home, category/genre/label/year listings, date archives, author pages, search results, pagination. Beyond-the-end and empty listings count |
| | `detail` | the main content is one item: a film/song/series post, or one article, including spam articles and single-page spam subdomains |
| | `other` | neither: utility/legal pages, login or bot verification, empty or script (`wp-includes`, JSON) responses |
| `relevance` (hub and detail only) | `relevant_content` | about the site's intended content (films, songs, series). For a hub: its items are mainly such |
| | `spam_or_irrelevant` | gambling/betting/SEO/loans/unrelated articles, generic SEO filler about "watching movies", defaced pages. For a hub: its items are mainly such |
| | `ambiguous` | an empty listing, or items mixed without a clear majority |
| | `n/a` | kind `other` |

| Split | hub | detail | other | relevant | spam | ambiguous |
|---|---:|---:|---:|---:|---:|---:|
| dev | 66 | 68 | 8 | 105 | 26 | 3 |
| holdout | 79 | 62 | 3 | 107 | 31 | 3 |

**Separation.** The rules were developed on dev only. They were frozen and
committed (`2b8233f`) before the holdout was scored once. The holdout
numbers in §5 were produced after that commit, and nothing was changed
afterwards. The G5 labels were not read.

**Limits of the sample.** One labeller (the implementer) made the labels,
and they are not double-coded. `other` has 11 pages, too few for a
meaningful per-class estimate. Per domain, the holdout has 9–16 pages,
which shows whether a domain carries the result but gives no per-domain
confidence interval. The population is 11 film-piracy sites in three
families (WordPress clones, Blogger, small custom sites).

## 4. Algorithm (policy `what/v1`)

### 4.1 URL tokenization (`tokenize_url`)

Path segments are percent-decoded, NFKC-normalized and casefolded, then
classified:

| Token | Rule |
|---|---|
| `{year}` | 1950–2039 |
| `{mm}`, `{dd}` | 01–12 after `{year}`; 01–31 after `{mm}` |
| `{num}` | digits |
| `{slug}-{year}` | a slug whose last word is a year (`/lens-2017/`). Design §6 "slug year token". It separates item posts from other posts at `/<slug>/` |
| `{slug}.html` etc. | an extension (html, htm, php, asp(x), jsp, shtml) with the stem classified |
| literal | generic CMS words: page, category(ies), tag(s), author, label, archive(s), genre(s), browse, topic(s), search, feed, p, amp, comments, wp-*, cdn-cgi |
| `{lang}` | a first segment that is an ISO-639-1 code from a short list |
| `{slug}` | anything else |

- The query keeps only pagination keys (page, paged, start, offset,
  updated-max) and search keys (s, q, query, search, term, keyword).
  Values, tracking keys and comment ids are dropped. The fragment is
  ignored. The trailing slash is part of the template.
- At most 10 segments (the rest become `{deep}`) and 8 query keys.
  Malformed or non-HTTP URLs have no template.
- **Markers:** pagination (`page/{n}`, a trailing `{num}` after a slug, a
  pagination key), search, a listing path, a date archive, the site root.

### 4.2 Site profile (`build_site_profile`, per host)

1. One page per URL (the smallest by content hash). At most 1,000 pages,
   chosen by URL hash. At most 500 same-host links per page, chosen by URL
   hash. The result is independent of input order and duplicates.
2. **Boilerplate:** same-host links on ≥ 20 % of the sampled pages (≤ 256),
   and title segments (split on ` - `, `|`, `–`, `:`, …) on ≥ 20 % of titles
   (≤ 32), e.g. "Isaimini 2026". Page-specific links are the links that
   are not boilerplate. The **title core** is the title without
   boilerplate segments.
3. **Site script:** the dominant Unicode script of title cores that carry
   content evidence.
4. **Templates:** over the distinct URLs (pages and link targets, ≤ 20,000
   by hash). Templates with ≥ 20 members are kept, at most 32, ordered by
   (−support, template). Per template it counts members with **content
   evidence** (content term or year in title core/anchor/slug, no spam
   evidence), members with **spam evidence** (spam term, or a title script
   that differs from the site script), and members that are **listed**
   (linked from the site's listing pages).
5. **Role:** structural URL → `listing`; content ≥ 20 % and spam ≥ 20 % →
   `mixed`; (content ≥ 50 % or listed ≥ 50 %) and spam < 20 % → `content`;
   spam ≥ 20 % → `spam`; otherwise `unknown`. **Pattern templates** are
   mixed templates and content templates defined by evidence (content ≥ 50 %).
   Their members are expected to carry content evidence themselves.

### 4.3 Page analysis (`analyze_page`)

**Kind**, evaluated in order. Each step has its reason codes:

1. malformed URL → other; no links and < 200 text chars → other
   (`empty_page`);
2. slug of ≤ 2 words that is a utility term (contact, privacy, login, …) →
   other (`utility_page`);
3. ≥ 8 page-specific links → hub (`many_page_links`, plus markers), unless
   the page is an **item**: its own title/slug carries content evidence, it
   has no marker, it is not in a listing template, and it has < 24
   content-template links. An item stays a detail (`item_title`,
   `related_links`);
4. any marker (pagination, search, listing path, date archive, root with
   links, CMS hub title such as "Page 3 of 9" or "You searched for") → hub
   (`…, few_page_links`);
5. a short title core that is a utility term, with no content evidence →
   other;
6. content/mixed template membership, item title, article metadata or a
   published date → detail; a root page with text and no links → detail
   (`single_page_site`); any other page with text → detail (`text_page`);
   otherwise other (`no_signal`).

**Relevance** comes from independent signals:

| Positive | Negative |
|---|---|
| `content_terms` (title core / slug) | `spam_terms` |
| `year` (title core / slug) | `script_mismatch` (title script ≠ site script) |
| `content_template` (member of a `content` template) | `spam_template` |
| hub: `content_links` (≥ 3 own links with content evidence, ≥ 2× the bad ones) | `template_mismatch` (detail in a pattern template without content evidence) |
| hub: `content_template_links` (≥ 3 own links into content templates, ≥ 2× the bad ones) | hub: `spam_links` (≥ 3 bad own links, ≥ 2× the content ones) |

A link is **bad** when it has spam evidence, points into a spam template,
or points into a pattern template without content evidence.

**Decision:** spam if ≥ 2 negatives, or ≥ 1 negative and no positive.
Relevant if ≥ 2 positives and no negative, or ≥ 3 positives and 1
negative. Otherwise **unknown**. No single term decides. A template match
is deliberately not a positive, because it would restate the page's own
evidence.

### 4.4 Thresholds and their origin

| Parameter | Value | Origin |
|---|---|---|
| template support | 20 | design §6, before WHAT data |
| boilerplate share | 0.2 | population link survey (nav/sidebar links sit on ≥ 50 % of pages, widgets on 10–20 %), before labelling |
| `hub_min_own_links` | 8 | population survey of page-specific links per URL shape: detail p90 ≤ 7, listings median 9–114, before labelling |
| `hub_strong_links` | 24 (3 × 8) | **dev**: detail pages with in-text/related links carry up to 12 page-specific links |
| `min_text_chars` | 200 | **dev**: empty script responses (0) and bot verification (37) |
| content / minor share | 0.5 / 0.2 | design choice (a majority / one in five) |
| spam role at 0.2 | — | **population template statistics** (unlabelled): on isaimini/moviesda.com.in `/{slug}/`, 32–38 % of members carry explicit spam evidence, against 4–5 % with content evidence. Brand-name spam escapes the vocabulary, so explicit evidence under-counts spam |
| link evidence | 3 | **dev** |
| decision rule | 2 signals | design: no single signal decides |
| bounds | 1,000 pages, 500 links/page, 20,000 URLs, 32 templates, 256 links, 32 segments, 10 segments/URL, 200 result links | memory bounds. M1 maxima: 4,783 pages per host, 310 anchors per page |

### 4.5 Output (`WhatResult`)

`url`, `host`, `template`, `template_role`, `kind` (kind, reasons,
own_links, own_content_links), `relevance` (decision, positive, negative,
matched terms ≤ 8), `content_links` (sorted, ≤ 200), `policy`
(`what/v1`), `vocabulary` (`what-vocabulary/v1`). `SiteProfile` exposes
the templates with support, evidence counts and role, plus
`content_templates` and `pattern_templates()`.

### 4.6 Vocabulary (operator data)

`what_vocabulary.toml` (`what-vocabulary/v1`). Its provenance is in the
file. It was written before labelling, from knowledge of the operator's
content and of gambling/SEO spam in English, Thai, Devanagari, Vietnamese,
Indonesian, Portuguese and Spanish. It also holds generic CMS hub-title
patterns and utility terms. Latin terms match whole words, other scripts
match substrings. It can be replaced (`load_vocabulary(path)` /
`Vocabulary.from_mapping`), and results record its version. It is not a
universal blacklist: a spam term is one negative signal, and a page needs
a second one, or no positive, to be called spam.

## 5. Results

`benchmarks/p7-intelligence/what_eval.py --split dev|holdout` →
`results/what-eval-{dev,holdout}.json` (per-page reasons included).

### 5.1 Page kind

| Split | n | Accuracy | hub P / R / F1 | detail P / R / F1 | other P / R / F1 |
|---|---:|---:|---|---|---|
| dev (tuned on) | 142 | 0.979 | 1.000 / 0.955 / 0.977 | 0.958 / 1.000 / 0.978 | 1.000 / 1.000 / 1.000 (n = 8) |
| **holdout** | 144 | **0.972** | **1.000 / 0.962 / 0.981** | **0.953 / 0.984 / 0.968** | 0.750 / 1.000 / 0.857 (n = 3) |

Holdout confusion (gold → predicted): hub → hub 76, → detail 3;
detail → detail 61, → other 1; other → other 3.

### 5.2 Relevance (hub and detail pages with a relevant/spam label)

`unknown` is an abstention: it lowers recall, never precision.

| Split | n | Decided | relevant P / R / F1 | spam P / R / F1 |
|---|---:|---:|---|---|
| dev (tuned on) | 131 | 105 | 0.977 / 0.810 / 0.885 | 0.944 / 0.654 / 0.773 |
| **holdout** | 138 | **105 (76 %)** | **0.977 / 0.804 / 0.882** | **1.000 / 0.548 / 0.708** |

Holdout confusion: relevant → relevant 86, spam 0, unknown 21; spam →
spam 17, relevant 2, unknown 12.

### 5.3 Per domain (holdout)

| Domain | Pages | Kind correct | Relevance labelled | Decided | Correct | Wrong |
|---|---:|---:|---:|---:|---:|---:|
| gofullmovies.com | 12 | 10 | 12 | 1 | 1 | 0 |
| isaimini.com.in | 13 | 13 | 13 | 10 | 10 | 0 |
| isaiminimix.com | 9 | 9 | 9 | 9 | 9 | 0 |
| movflix.my | 13 | 13 | 11 | 11 | 11 | 0 |
| moviesda.business | 15 | 15 | 14 | 14 | 14 | 0 |
| moviesda.com.in | 15 | 15 | 14 | 13 | 12 | 1 |
| moviesda16.best | 10 | 10 | 10 | 10 | 9 | 1 |
| stripemovies.com | 16 | 15 | 16 | 8 | 8 | 0 |
| tamilprint.co.in | 14 | 13 | 14 | 9 | 9 | 0 |
| tamilprint.com.in | 15 | 15 | 14 | 12 | 12 | 0 |
| tamilrockers2024.com | 12 | 12 | 11 | 8 | 8 | 0 |

- **No single domain carries the result.** Page kind is ≥ 0.83 on every
  domain.
- Relevance is decided on most pages of 8 domains. It abstains on
  gofullmovies (11 of 12) and on half of stripemovies, whose titles carry
  neither years nor vocabulary.
- The 2 wrong relevance decisions (both spam called relevant):
  - moviesda.com.in: an SEO article whose slug ends in a year
    (`…-buyers-guide-for-2026`), which lands in the year-slug content
    template;
  - moviesda16.best: a film URL serving a defacement page.

Dev errors had the same shapes: film-named listings whose items are spam,
and a search listing of year collections at `/<slug>/` URLs.

### 5.4 Templates (qualitative; no template ground truth exists)

| Host | Content templates found (support) | Spam / other roles |
|---|---|---|
| isaimini.com.in | `/{slug}-{year}/` (1,136) | `/{slug}/` spam (2,012) |
| moviesda.com.in | `/{slug}-{year}/` (1,116) | `/{slug}/` spam (1,444) |
| tamilprint.com.in | `/{slug}-{year}/` (1,361) | `/{slug}/` spam (443) |
| moviesda.business | `/{year}/{mm}/{slug}.html` (331) | comment feeds unknown |
| tamilrockers2024.com | `/{year}/{mm}/{slug}-{year}.html` (81) | — |
| isaiminimix.com, movflix.my, stripemovies.com, tamilprint.co.in, gofullmovies.com | `/{slug}/` | movflix `wp-includes/*` unknown |
| moviesda16.best | `/{slug}` (1,092) | `/{slug}.php` unknown |

- **Recovery:** on every host, the template of the labelled relevant
  detail pages is a content template (dev 53/53, holdout 43/43).
- **Contamination:** holdout spam details inside a content template: 5/19.
  Two are in a year-slug template (an SEO "…for 2026" article and a
  Vietnamese betting post ending in "năm 2026"). One is a defaced film
  URL. Two are login-spam posts on tamilprint.co.in, whose `/{slug}/` is
  98 % films. Only 2 of the 5 were called relevant; the rest abstained.
  Dev: 0/15.
- On the three WordPress clones, the year-slug token alone separates film
  posts from the spam stream, as G5 suggested (`/lens-2017/` vs
  `/wolf-run-slots-at-playrand/`).

### 5.5 Determinism, boundedness, cost

- Evaluation output is byte-identical with `PYTHONHASHSEED` 1 and 999.
  Unit tests check order and duplicate independence with hypothesis.
- State is bounded by the policy (§4.4). No per-URL history survives a
  profile build.
- CPU (`what_bench.py`, whole population, I/O excluded): profiles for 27
  hosts take 3.2 s. `analyze_page` costs a median of 268 µs and p99 4.5 ms
  (pages with up to 310 links). Details: [benchmarks.md](benchmarks.md) §5.

## 6. Limitations

1. **One small population:** 11 film-piracy sites, mostly WordPress
   clones. Other content verticals need their own vocabulary and a new
   labelled sample.
2. **Abstention is common** where titles carry no years or vocabulary
   (24 % of holdout pages). WHAT says `unknown` rather than guess.
3. **Spam recall is 0.55** (precision 1.0). Brand-name spam escapes the
   vocabulary; the template role recovers part of it.
4. **Year-slug false positives:** SEO articles "for 2026" match the item
   template. Defacement is invisible to URL and title features.
5. **No page regions:** listings with spam items and film widgets are
   misjudged (§2, gap 2).
6. **Single labeller**, not double-coded, and the same person wrote the
   rules. The dev/holdout split, label-first commits and a single holdout
   run limit tuning bias but do not remove it.
7. **Profiles are a batch computation** over a host's pages. No runtime
   projection or storage exists yet.

## 7. Rejected approaches

| Approach | Why rejected |
|---|---|
| path depth as page kind | WordPress details sit at depth 1 (audit §5.2) |
| raw same-site link count | movflix details have more same-site links (54) than its listings (35) because of sidebars; removing boilerplate fixes this |
| a universal spam blacklist | brand-name spam escapes any list, and a list is site-agnostic; operator data plus template evidence instead |
| `template_match` as a positive | restates the page's own evidence, so one keyword decided (found by a unit test before the holdout) |
| an ML classifier | out of scope (plan P7: deterministic, explainable) |
| a Redis or stream consumer for WHAT | not needed: WHAT is a pure function of P5 facts (ADR-022/023 unchanged) |
| new storage now | WHAT runs as a batch computation over facts. A P7-owned profile projection is deferred until WHEN needs it (§8) |

## 8. Future dependencies (not implemented)

- **WHEN** (design §7, still gated): count, per hub, new `content_links`
  whose pages are not spam, credited to every hub that links them. The
  signal must reach precision ≥ 0.8 on a **new** labelled sample of hub
  revisits. M1 has few hub revisits (design §7 data caveat). A short
  hub-only experiment would be proposed, not run.
- **Runtime profiles:** if WHEN needs them, a P7-owned, rebuildable
  projection of `SiteProfile` from `page_extracts` (P5, read-only), with its
  own design, migration and fallback, following ADR-023. No Redis, no P5/P6
  table.
- **Priority** (design §8) stays unimplemented.
