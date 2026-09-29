# P6 Audit — filter engine and discovery port

Status: **Gate A complete (2026-09-29).** No P6 runtime code was written
before this audit. Design: [design.md](design.md).

Scope read: V1 `utils/url_utils.py` (filter parts), `datasets/domain_blacklist.txt`,
the six V1 fetch engines' redirect handling, `search_engines/*`, `discovery/*`,
`seeds/*`, V1 `config.yaml` (`search:`); V2 P1 events/ownership, P3 admission
API, P4 interception hook, P5 `LinkPolicy`/`NormalizationRules`, P2
`url_state`. V1 is at `2dfb542` with uncommitted user changes in its working
tree; nothing in V1 was modified.

## 1. V1 filter behaviour

| # | V1 behaviour (location) | Finding | P6 verdict |
|---|---|---|---|
| F-1 | `AUTO_BLACKLIST_DEFAULTS` — 39 registered domains: 29 reference/news/social (wikipedia, imdb, ndtv, facebook, x, youtube, reddit, t.me, …) and 10 ad networks (doubleclick, googlesyndication, googleadservices, adnxs, popads, propellerads, exoclick, taboola, outbrain, mgid) (`url_utils.py:87`) | Two different things mixed: ad networks (classification AD) and *out-of-scope* sites (content that is not a crawl target). Written into the blacklist file on every start (`ensure_blacklist_seeded`) | **Migrate**, split: ad networks → `built_in` host rules `AD`; out-of-scope sites → `built_in` host rules classification `CONTENT` with explicit action `BLOCK` and reason `out_of_scope` (explainable, not "ad") |
| F-2 | `AUTO_BLACKLIST_HINTS` — 27 substrings matched anywhere in host + registered domain (`url_utils.py:204`) | Substring match: `msn` hits `msnbc`, `imdb` hits any host containing it; `telegram` hits `telegram-ios.com`; causes whole-registered-domain blacklisting | **Drop.** Exact host rules (F-1) replace it |
| F-3 | `AD_HOST_HINT_PATTERNS` — two regexes on hostname labels: `adservice|adserver|adclick|adtrack|adsystem|advert|popunder|popup|banner` and `track|tracker|tracking|redirect|click` (`url_utils.py:129`) | The second regex matches a label boundary around `click`/`redirect`: `new1.movcloud.click`, `dl1.hotshare.click`, `data527.click`, `nowgoal.click`, `linkurl.click` all ended in the blacklist. `.click` is a **TLD** used by piracy download hosts | **Drop the second pattern.** First pattern → `built_in` host-label rule `AD`, confidence below the block threshold (CLASSIFY only, see design §5), never applied to the TLD label |
| F-4 | `ADULT_TOKEN_HINTS` / `ADULT_SUBSTRING_HINTS` on host **+ path + query**; a hit writes the **registered domain** to the blacklist (`clean_url`, `url_utils.py:791`) | One page path such as `/movie/sex-education-s01` condemns the whole site. This is the most probable origin of the seed-site entries in F-6 (unverifiable: V1 recorded provenance only in log lines, and no V1 logs are retained) | **Drop** automatic domain condemnation. Adult content is not one of the seven P6 classes; a later explicit operator rule may block specific hosts |
| F-5 | `is_blacklisted()` classifies *and writes*: every check may append to `domain_blacklist.txt` (`url_utils.py:572`) | Hidden writes from a pure-looking predicate; not multi-host safe (D6) | **Drop.** P6 rules are written only by importers/operator CLI into Scylla; evaluation is pure |
| F-6 | `is_suspicious_redirect(source, final)` — cross-registered-domain redirect is rejected when the final host is blacklisted/ad-like **or when the source path contains `/watch`, `/stream`, `/download`, `/movie`, `/episode`, `/play`** (`url_utils.py:450`); every V1 engine then returns an error and the fetch fails (`http_crawler.py:92` etc.) | A mirror redirect from `/movie/x` to another domain is a legitimate observation; V1 threw it away (D7) | **Change.** P4 records the chain (fact); P6 classifies it with explicit redirect rules only; a redirect never fails a fetch; a BLOCK decision only stops admission of that page's links (design §9) |
| F-7 | `should_queue_link` / `get_link_priority` / `RELEVANT_EXTERNAL_HINTS` / `LOW_SIGNAL_PATH_HINTS` / `CONTENT_PATH_HINTS` | Relevance and priority heuristics | **Defer to P7** (priority/value). P6 keeps only explicit scope rules (design §10) |
| F-8 | `TRACKING_PARAMETERS` rewriting in `clean_url` | Identity change | **Stays dropped** (P1/P5 decision) |
| F-9 | Playwright route handler aborting ad/blacklisted hosts inline | Filter logic inside the fetcher | Already moved to the P4 `RequestInterceptor` hook; P6 implements it |

## 2. `domain_blacklist.txt`

The plan cites **1,463 lines**. That figure comes from V1's
`docs/architecture/history/optimization_blacklist.md` (2026-08). The file is
git-ignored in V1 (`.gitignore: /datasets`), has no history, and was
rewritten on **2026-09-06 06:28** (mtime). The file that exists today has
**100 lines: 2 comment lines + 98 entries, 0 duplicates** (`wc -l` reports
99 because the last line has no newline; the comment header appears twice,
once per re-creation). The 1,463-line version is lost; P6 imports what
exists and records this.

Classification of the 98 entries. The categories are an **operator review**
(2026-09-29, this audit), not code: they ship as a reviewed manifest with
its own provenance (design §8), and every entry absent from the manifest
defaults to *quarantine*.

| Category | Count | Examples | Verdict |
|---|---:|---|---|
| F-1 defaults: ad networks | 10 | doubleclick.net, taboola.com, mgid.com | migrate → `AD`, BLOCK |
| F-1 defaults: reference/news/social | 29 | wikipedia.org, imdb.com, youtube.com, t.me | migrate → `CONTENT`, BLOCK `out_of_scope` |
| Same family, added at runtime | 13 | facebook.net, cdninstagram.com, redditstatic.com, youtube-nocookie.com, telegram.me, tumblr.com, indiatimes.com, netflix.com, primevideo.com | migrate → `CONTENT`, BLOCK `out_of_scope` |
| Unrelated education/health sites | 8 | nih.gov, cornell.edu, microbenotes.com, celiac.com | migrate → `CONTENT` `out_of_scope`, confidence 0.6 (below the block threshold → CLASSIFY only) |
| Piracy / download sites (**crawl targets**) | 21 | hdhub4u.af, isaidub.love, kuttymovies1.fit, isaimini.com.in, stripemovies.com, moviedrivebd.com, mp4moviez.diet, moviesda32.com, new1–3.movcloud.click, dl1/2/5/6/7/10.hotshare.click | **quarantine** (`content_source`) — 6 are **M1 seed hosts** |
| Video downloaders (hint-substring artefacts) | 4 | sstiktok.co, youtubestorm.com | quarantine (`possible_media_source`) |
| `.click`/`.adult` artefacts (F-3/F-4) | 5 | linkurl.click, data527.click, keonhacai.adult | quarantine (`heuristic_artefact`) |
| Cloud/infra hostnames | 3 | redirect.prod.experiment.routing.cloudfront.aws.a2z.com, mt-file-tracking-temp.s3.eu-west-1.amazonaws.com | quarantine (`infra_ambiguous`) |
| Raw IPv4 addresses | 2 | 174.138.23.31 | quarantine (`ip_address`: shared hosting) |
| Google / reserved | 3 | google.com, googleusercontent.com, example.com | quarantine (`search_origin`, `possible_media_source`, `reserved_name`) |
| Invalid | 0 | — | — |

Result: **60 migrated** (52 BLOCK, 8 CLASSIFY), **38 quarantined** (stored,
disabled, reason recorded), **0 rejected**, 0 duplicates; 10 of the
migrated entries duplicate `built_in` rules and keep both provenances.

The importer reproduces these counts in its dry-run report and a test
pins them. **Risk:** importing
V1 blindly would have blocked 6/52 (11.5 %) of the M1 seed set and the
download hosts that P8 needs.

Representability: all 98 are plain host names (V1 stored the host that
matched; subdomain matching is `host == entry or host.endswith("." + entry)`),
so each maps onto a P6 host-suffix rule without loss. Nothing is
unrepresentable.

## 3. V1 search discovery

| Engine | Access | State today | Verdict |
|---|---|---|---|
| duckduckgo | `html.duckduckgo.com/html/` form, `/l/?uddg=` unwrapping | working in V1 | port |
| bing | `/search`, `/ck/a?u=a1<base64>` unwrapping | working in V1 | port |
| brave | `search.brave.com/search` | working in V1 | port |
| yandex | `/search/?text=`; captcha detection | V1 config marks it blocked (`showcaptcha`) | port the adapter; **disabled by default** |
| ahmia | clearnet form with hidden token, `redirect_url=` unwrapping | onion results | port; results are `.onion` → `tor` queue |
| torch | onion mirrors through Tor | no Tor daemon on this host (P4 finding) | port; unavailable here (adapter reports `unavailable`, not an error loop) |

Useful behaviour kept: redirect unwrapping per engine, per-engine result
cap, blocked-engine cooldown (V1: 999 queries after a captcha/"blocked"),
per-query report with engine errors. Dropped: `score_discovered_url`
(engine+rank priority = P7), `URLUtils.clean_url` on results (V2 uses P1
canonicalization), BeautifulSoup (V2 parses with selectolax, P5 D-7),
`CustomQueryGenerator` (identity function). CAPTCHA/verification pages are
**recorded as `blocked`**, never solved or evaded.

Queries: V1 took them from `--query` on the command line; nothing derived
them from targets in code.

## 4. V1 seeds

`seeds/piracy_sites.txt` (51 URLs) is the only non-empty seed file; the
other four are empty. `load_seeds` skips `#`/blank lines and has no
provenance. The P0 seed set `benchmarks/v1-baseline/seeds.txt` is that
file (52 lines incl. one duplicate host).

## 5. V2 integration points

| Point | Current state | Finding |
|---|---|---|
| P5 `LinkPolicy.allow(source, target, relation) -> bool` (`extraction/urls.py`) | allow-all | It runs **inside** `extract()`: a dropped link is not a fact, and it changes `link_set` hashes and `urls.discovered`. Extracts are reused per raw page version, so a policy that changes with hot-reloaded rules would make P5 output depend on the ruleset active at first extraction → **design decision P6-Q1** |
| P5 `NormalizationRules(scheme, extra_selectors)` | baseline scheme | A new selector set needs a new scheme name (ADR-020); switching schemes re-keys all revisions |
| P4 `RequestInterceptor.decide(InterceptedRequest) -> InterceptDecision(action, rule_id, reason)` | allow-all | synchronous, per sub-request; fixed resource-type blocking (`blocked_resource_types`) runs before it; top-level navigations reach it too |
| P4 redirect chain | `FetchResult.redirects` → `PageObservation.redirects` | available to P6 through `page.observed` / `PageObservationRepository.get` |
| P3 admission | `Frontier.admit/admit_many(Admission(url, queue, priority, not_before, reason≤64))` | there is no "CrawlTask" type in V2; an `Admission` is the P3 task request. **Nothing consumes `urls.discovered` today** although P1 events.md says the frontier's default admission of it keeps crawling alive. The frontier forgets a URL when its task ends (no visited set) → without an admission policy, re-discovery re-admits the same pages forever |
| P2 `url_state` (W12) | `first_seen`, `last_observed_at`, `last_status` | no "last admitted" and no filter state; P6 needs its own tables |
| P1 ownership | components: `crawl_intelligence`, `frontier`, … no `filter` | P6 emits **no events** in this design, so no contract change is needed |
| Registrable domain | none in V2; `DomainId` is the host by design (PSL is a derived attribute) | third-party rules need eTLD+1 → a PSL dependency (design §4) |

## 6. EasyList / uBlock

| List | Maintainer / URL | License (per list header) | Needed? |
|---|---|---|---|
| EasyList | easylist.to, `https://easylist.to/easylist/easylist.txt` | dual GPL-3.0-or-later / CC BY-SA 3.0 | yes — AD |
| EasyPrivacy | `https://easylist.to/easylist/easyprivacy.txt` | same | yes — TRACKER |
| uBlock filters (uAssets) | github.com/uBlockOrigin/uAssets | GPL-3.0 | not needed for M1; the importer accepts uBO network syntax subset if added later |

The license line is read from each downloaded file's header and stored
with the source revision; the importer refuses a file without an
identifiable license/homepage header. **Lists are not vendored in git**:
they are downloaded by an explicit operator import command, hashed, and
stored as rule rows in Scylla (internal use, no redistribution). Tests use
small hand-written ABP-syntax fixtures, not excerpts of the lists.

Syntax actually needed (network filters only): `||host^`, `|` anchors,
`*`, `^`, plain substrings, `@@` exceptions, `$third-party`/`$~third-party`
(`$3p`/`$1p`), resource types (`script`, `image`, `stylesheet`,
`xmlhttprequest`, `subdocument`, `media`, `font`, `ping`, `websocket`,
`other`, `document`, `popup`) and their negations, `$domain=a|~b`,
`$important`. Everything else — cosmetic (`##`, `#@#`, `#?#`, `#$#`,
scriptlets `##+js`), HTML filters, regex filters `/…/`, `$redirect`,
`$removeparam`, `$csp`, `$rewrite`, `$header`, `$match-case` — is counted
as **unsupported** and skipped, never approximated. Cosmetic rules are out
of scope: P6 does not modify pages and P5 normalization selectors are
operator-owned (design §11).

## 7. Risks

1. Generic ad/tracker rules matching piracy/media hosts (e.g. EasyList
   `/ads/` path patterns, `.click` hosts) → false-positive guard corpus +
   host-specific protective rules win by specificity (design §6).
2. M1 without a scope rule follows links into the whole web; with only
   first-sight admission it never recrawls (no change history for P7).
   Both are policy decisions (design §10, questions P6-Q2/Q3).
3. The 100k decisions/s gate in pure Python depends on indexing (host
   suffix map + token index) — measured, not assumed.
4. The M1 24 h run shares the 15 GB host and the USB-HDD Scylla (P2
   finding); Scylla write volume from decision logging must be bounded.
