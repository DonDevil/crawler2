# P6 Design — filter engine and discovery port

Status: **PROPOSED 2026-09-29 — awaiting design review (Gate B).** Open
decisions are listed in §22; nothing below them is implemented yet.
Audit: [audit.md](audit.md).

## 1. Responsibilities and boundaries

| Phase | Owns | Relation to P6 |
|---|---|---|
| P1 | contracts, IDs, canonical URL v1 | P6 reuses `canonicalize_url`, `UrlRef`, `DiscoveredLink`, `PageObservation`; **no contract change** (P6 emits no events) |
| P2 | Scylla/MinIO repositories, outbox, consumers | P6 adds its own tables (migration V003) and repository; reuses `IdempotentConsumer`, `RedisStreamReader` |
| P3 | admission mechanics, dedup of active tasks, leases, retries, politeness | P6 calls `Frontier.admit_many(Admission)` only; never touches Redis keys |
| P4 | fetching, redirect chain as fact, browser pool | P6 implements `RequestInterceptor`; P4 aggregates the decisions it gets back |
| P5 | factual extraction, revisions | P6 consumes `urls.discovered`; P5 extraction is unchanged (§22 Q1) |
| **P6** | classification, explicit filter policy, rule storage/reload, importers, seeds, search adapters, discovery admission | this design |
| P7 | priority, recrawl policy, value/site/change-rate learning | replaces P6's *static* M1 defaults (§10) by producing admissions/profiles; consumes P6 decision facts |

P6 never scores value, never learns, never ranks by target, and never
evades protections: a CAPTCHA or verification page is recorded as
`blocked` by P4 and classified, nothing more.

### The M1 loop (ownership of every arrow)

```
 seed file ──(crawler2-discover seeds)──┐        search adapters ──(crawler2-discover search)──┐
                                         ▼                                                       ▼
                        P6 filter (link context) ──► P3 Frontier.admit_many(Admission) ◄─────────┘
                                                              │ claim
                                                              ▼
                                   P4 worker ── fetch (browser sub-requests → P6 RequestInterceptor)
                                                              │ W1/W4 + page.observed (outbox)
                                                              ▼
                                   P5 crawler2-extract ── links (facts) + urls.discovered (outbox)
                                                              │
                                                              ▼
       P6 crawler2-discover admit: redirect decision (from the observation) → per-link filter
       decision → scope → revisit gate → Frontier.admit_many → decision records (Scylla)
                                                              │
                                                              └──► frontier … next crawl
```

No component calls another directly: seeds/search/admission go through the
P3 API; P4→P5 and P5→P6 go through outbox events.

## 2. Packages and processes

| Package | Contents | Pure? |
|---|---|---|
| `crawler2/filtering/model.py` | `Classification`, `Action`, `Rule`, `RuleSource`, `Decision`, `FilterInput` | yes |
| `crawler2/filtering/engine.py` | compiled `FilterEngine` (indexes, precedence, decisions) | yes |
| `crawler2/filtering/sites.py` | registrable-domain helper (PSL, cached) | yes |
| `crawler2/filtering/abp.py` | ABP/uBO network-filter parser → `Rule`s + unsupported report | yes |
| `crawler2/filtering/v1import.py` | V1 blacklist parser + review manifest → `Rule`s + report | yes |
| `crawler2/filtering/builtin.py` | the small `built_in` rule set (§8.3) | yes |
| `crawler2/filtering/store.py` | `RuleStore` protocol + `ActiveRuleset` holder with hot reload | no (Scylla via P2) |
| `crawler2/filtering/intercept.py` | `FilterInterceptor` implementing P4 `RequestInterceptor` | yes (reads holder) |
| `crawler2/filtering/cli.py` | `crawler2-filter` (import, publish, activate, rollback, explain, report) | no |
| `crawler2/discovery/search/*.py` | `SearchAdapter` protocol + 6 adapters | parsing pure; HTTP via injected client |
| `crawler2/discovery/seeds.py` | seed file parser | yes |
| `crawler2/discovery/admission.py` | `DiscoveryAdmitter` (§10) | no |
| `crawler2/discovery/cli.py` | `crawler2-discover admit | seeds | search` | no |
| `crawler2/storage/scylla/filtering.py` + `cql/V003__filter_discovery.cql` | P6 tables and repository | no |

A new `WorkerRole.DISCOVERY` (crawler2 config enum, not a P1 contract)
names the process role.

## 3. Filter model

```python
class Classification(StrEnum):
    CONTENT, MEDIA, PLAYER, NAVIGATION, AD, TRACKER, UNKNOWN


class Action(StrEnum):
    ALLOW, CLASSIFY, BLOCK


class Context(StrEnum):
    LINK, REQUEST, REDIRECT


@dataclass(frozen=True, slots=True)
class FilterInput:  # built once per evaluation from P1 canonical URLs
    context: Context
    url: str  # canonical v1 (links) or Playwright URL (requests)
    host: str
    path_query: str
    resource_type: ResourceType  # document, subdocument, script, image, stylesheet, xhr, media, font, ping, websocket, other
    source_host: str | None  # page / frame host (first-party side); None for seeds/search
    third_party: bool | None  # registrable(host) != registrable(source_host); None if no source


@dataclass(frozen=True, slots=True)
class Decision:
    classification: Classification
    action: Action
    confidence: float  # of the winning rule (1.0 for the default)
    rule_id: str  # "default" when nothing matched
    rule_source: str  # provenance label, e.g. "easylist@2026-09-29T10:00Z#sha:…"
    matched_field: str  # "host" | "url" | "redirect_hop[1].host" | …
    matched_pattern: str  # the rule's normalized pattern text
    reason: str | None  # rule reason, e.g. "out_of_scope"
    override_of: str | None  # rule_id this decision overrode (operator/exception tiers)
    ruleset: str  # active ruleset id (content digest)
    candidates: int  # rules that matched (debug; the full list via `explain`)
```

A `Decision` answers every question in the brief: *why allowed* (default or
an ALLOW rule), *why AD* (rule id + pattern + field), *why blocked*
(action + threshold, §6), *which rule / where from* (`rule_id`,
`rule_source` → source revision row with URL, digest, license, acquisition
time), *override involved* (`override_of`).

Resource types map to Playwright's names; links map by relation:
`anchor`/`meta_refresh`/`canonical`/`other` → `document`, `iframe` →
`subdocument`, `script_literal` → `other`.

## 4. Registrable domain (third-party)

`third_party` needs eTLD+1. P1 deliberately keeps it out of identity
(`DomainId` is the host). P6 derives it with the **`publicsuffixlist`**
package (MPL-2.0, bundled list, no network at runtime; pinned in `uv.lock`)
behind `crawler2/filtering/sites.py` with an LRU cache per host. The PSL
package version is part of the engine's semantics version (§7), so a PSL
upgrade yields a new ruleset id. IP hosts and single-label hosts are their
own registrable domain. *(New dependency — approval item §22.)*

## 5. Rule model

| Kind | Matches | Example | Sources |
|---|---|---|---|
| `host` | host equals `h` or is a subdomain of `h` (label boundary) | `||doubleclick.net^`, V1 entries | all |
| `host_label` | any host label equals one of a set (never the last label = TLD) | `adserver`, `popunder` | built_in |
| `path` | host (optional, suffix) + path prefix | `example.com/ads/` | operator, built_in |
| `url_pattern` | ABP pattern over the full URL: `|`, `||`, `*`, `^` | `/banner/*/ad_`, `||cdn.x^*/track.js` | easylist, easyprivacy, ublock, operator |
| `redirect` | a hop or the final URL of a redirect chain, optionally only cross-site | final host `||popads.net^` | operator, built_in |
| `selector` | CSS selectors for P5 `NormalizationRules` (§11) | `.elementor-loop-container` | operator only |

Modifiers on any non-selector rule: `resource_types` (+/−), `party`
(`any|first|third`), `contexts` (subset of LINK/REQUEST/REDIRECT),
`source_domains` include/exclude (ABP `$domain=`; the page/context rule
type), `important` (ABP `$important`), `exception` (ABP `@@` → ALLOW).

Every rule has:

| Field | Meaning |
|---|---|
| `rule_id` | `{source}/{kind}/{sha256(canonical rule body)[:16]}` — stable across re-imports and line moves; the same text in two sources gets two ids |
| `source` | `built_in`, `v1_blacklist`, `easylist`, `easyprivacy`, `ublock`, `operator` |
| `source_revision` | content digest of the imported input (or of the operator rule set) |
| `classification`, `confidence` | what the rule asserts; confidence default per source (§6.3) unless the rule states it |
| `action` | optional explicit action (overrides the threshold mapping) |
| `reason` | short code (`ad_network`, `out_of_scope`, `content_source`, …) |
| `enabled` | false = quarantined: stored, reported, never compiled |
| `origin` | original text/line (`raw`, line number) for explanation |
| `note`, `author` | operator rules only |

Provenance is data (`source`, `source_revision` → source row with URL,
fetched-at, sha256, license header, importer version), never a comment.

## 6. Decision semantics

### 6.1 Evaluation

1. Collect **all** enabled rules that match the input (index lookups, §7);
   rules whose `contexts`, `resource_types`, `party` or `source_domains`
   exclude the input do not match.
2. Order candidates by the total precedence key (§6.2); the first is the
   **winner**.
3. Map the winner to an action (§6.3). No candidate → `UNKNOWN`, `ALLOW`,
   `rule_id="default"`, confidence 1.0.

The result is a pure function of (input, ruleset). No dict/set iteration
order is involved: candidate lists are sorted by the precedence key, which
ends in `rule_id`.

### 6.2 Precedence key (ascending = wins)

1. **Tier**: `0` operator override (operator rule with explicit action) →
   `1` important block (`$important`) → `2` exception/ALLOW rules (`@@`,
   explicit ALLOW) → `3` classification rules.
2. **Specificity** (more specific first): `redirect` > `path` >
   `host` (longer matched host suffix first) > `url_pattern` (longer
   literal part first) > `host_label`. Rules constrained by
   `source_domains` rank before unconstrained ones of the same kind.
3. **Class protection**: `MEDIA` > `PLAYER` > `CONTENT` > `NAVIGATION` >
   `TRACKER` > `AD` — on an exact specificity tie the protective class
   wins (false-positive safety).
4. **Source rank**: operator > built_in > easylist = easyprivacy = ublock
   > v1_blacklist.
5. `rule_id` (lexicographic) — total order.

Consequences, each covered by a precedence test: an operator rule beats
everything and records `override_of` = the best non-operator candidate; an
EasyList `@@` exception cancels an EasyList block but not an `$important`
one (ABP semantics); `||ads.site.com^` (AD) beats `||site.com^` (CONTENT)
because the longer host wins; a host rule for a content source beats any
generic `url_pattern` ad rule on the same URL.

### 6.3 Action mapping

| Winner | Action |
|---|---|
| explicit `action` on the rule | that action |
| exception / ALLOW rule | `ALLOW` (`override_of` = best cancelled block) |
| classification ∈ context's `block_classes` and confidence ≥ `block_threshold` | `BLOCK` |
| otherwise | `CLASSIFY` (label recorded, request/link proceeds) |
| no rule | `ALLOW`, `UNKNOWN` |

Defaults: `block_classes` = {AD, TRACKER} for REQUEST and LINK,
{AD, TRACKER} for REDIRECT; `block_threshold` = 0.9. Source default
confidence: operator 1.0, built_in 1.0, easylist/easyprivacy/ublock 0.95,
v1_blacklist 0.9 for reviewed-migrated entries (0.6 for the "unrelated
sites" category → CLASSIFY). Confidence is a declared property of a rule,
not a probability the engine computes: the engine is deterministic.

REQUEST context: a top-level navigation (`is_navigation` and no frame) is
**never blocked** — the page was already admitted; its decision is still
recorded.

## 7. Compilation and evaluation

A ruleset compiles into one immutable `FilterEngine`:

- `host_index: dict[str, tuple[Rule, …]]` keyed by host suffix; a lookup
  walks the host's suffixes (≤ label count lookups).
- `label_index: dict[str, tuple[Rule, …]]` for `host_label`.
- `path_index` keyed by host suffix → sorted path prefixes.
- `token_index: dict[str, tuple[CompiledPattern, …]]` for `url_pattern`:
  each pattern is indexed under its rarest literal token (≥ 3 chars; ties
  → longest, then lexicographic); patterns with no usable token go to a
  small `untokenized` list (count reported). Evaluation tokenizes the URL
  once and checks only candidate patterns (compiled regex per pattern).
- Everything is built from rules sorted by `rule_id`; the **ruleset id** =
  sha256 over the sorted canonical rules + `ENGINE_SEMANTICS` version + PSL
  version. Same rules → same id → same compiled behaviour (tested).

Optional per-process LRU decision cache (bounded, default 50 000 entries,
keyed by the full `FilterInput`), dropped on every ruleset swap. The
performance gate is measured **without** the cache (§21).

## 8. Importers

All importers are pure parsers returning `(rules, report)`; the CLI writes
the result as a new *source revision* (§13). `--dry-run` prints the report
and writes nothing. Re-importing identical input yields the same revision
digest and rule ids (idempotent: rows are upserts of identical values).

### 8.1 V1 blacklist (`crawler2-filter import-v1 PATH --manifest M`)

1. Parse lines; skip blanks/comments (counted); lowercase; strip scheme,
   path, port, trailing dot (counted as *transformed*); validate as a host
   or IP (invalid → *rejected* with line number).
2. Duplicates within the file → one rule, *duplicate* count, both line
   numbers kept in `origin`.
3. Look up each host in the **review manifest**
   (`crawler2/filtering/data/v1_blacklist_review.toml`, reviewed
   2026-09-29 from audit §2; its path + digest become part of the source
   revision). Entry verdict `migrate` → enabled rule with the manifest's
   classification/reason/confidence; `quarantine` → stored `enabled=false`
   with reason. **Hosts absent from the manifest are quarantined**
   (`unreviewed`), never silently enabled or discarded.
4. Report: imported / quarantined / rejected / transformed / duplicated /
   ambiguous (= in manifest with `quarantine`), per category.

### 8.2 ABP/uBO network filters (`crawler2-filter import-abp --source easylist URL|PATH`)

- Downloads over HTTPS only when the operator runs the command (never at
  crawler runtime), stores the sha256 and fetch time, reads `! Title`,
  `! Version`, `! Last modified`, `! Homepage`, `! License` headers into
  the source revision; **refuses** input without a license/homepage header
  unless `--license` is given explicitly (recorded).
- Supported syntax and unsupported categories: audit §6. Each unsupported
  line is counted by category; the report lists counts and the first N
  examples. Unsupported lines never become approximate rules.
- Class per source: `easylist` → AD, `easyprivacy` → TRACKER, `ublock` →
  AD unless the operator states otherwise at import.
- Raw list files are cached under git-ignored `var/filter-lists/` for
  re-parsing only; Scylla rows are the authority.

### 8.3 `built_in`

A small code-reviewed set: the 10 V1 ad networks (AD), the V1 F-1
out-of-scope sites (CONTENT, BLOCK, `out_of_scope`), the F-3 first pattern
as `host_label` AD at confidence 0.6 (CLASSIFY only). **No
content-source/target hosts are hard-coded** — protective rules for known
content/media sources are operator rules with provenance (§22 note), and
test fixtures use reserved `.test` names.

## 9. Redirects

P4's chain (`PageObservation.redirects`, final URL) is evaluated in the
REDIRECT context by the admission service: each hop's target and the
final URL are matched (`matched_field = redirect_hop[i]` / `final`), with
`third_party` relative to the *requested* URL. Only explicit `redirect`
rules and host/pattern rules whose `contexts` include REDIRECT apply —
nothing is inferred from source path words (V1 F-6 dropped). A BLOCK means:
the page's outgoing links are **not admitted** (recorded once per
observation with the rule). The chain, final URL and observation stay
untouched facts; nothing is rewritten and no fetch is failed.

## 10. Discovery admission (`crawler2-discover admit`)

Consumer group `discovery-admission` on `urls.discovered` (P1 lists the
frontier as its consumer; this is that default admission). Per event
(idempotent by `page_observation_id`):

1. Redirect decision for the source observation (§9); BLOCK → stop.
2. For each link (sorted by `url_id` for determinism): LINK-context
   decision with the source page as first party. BLOCK → record, skip.
3. **Scope** (static, explicit, §22 Q2) → out of scope: counted, skip.
4. **Revisit gate** (static, §22 Q3): read `url_admission` (§13) for the
   batch; skip if last admission is younger than `revisit_after_s`.
5. `Frontier.admit_many` with a **static** priority by provenance: seed 70,
   search 60, in-scope link 50, leaf link 40 (P1 priority scale; P7
   replaces these). Queue: `tor` for `.onion`, else `http` (P4 escalates
   to browser itself). `reason` = `discovered`.
6. `REJECTED_FULL` → not recorded as admitted (a later rediscovery
   retries); counted.
7. Write `url_admission` rows for admitted URLs and decision rows (§17).

Crash between 5 and 7: the event is redelivered, the frontier answers
`duplicate`/`merged` for still-active tasks, and the rows are rewritten
with the same values — at-least-once is harmless.

## 11. Normalization rules (P5 hook)

`selector` rules (operator only) compile into
`NormalizationRules(scheme="html-normalized/v1+p6:<digest12>", extra_selectors=…)`
supplied to `crawler2-extract` at start. Changing them changes the scheme
and therefore every revision id (ADR-020) — so the scheme is **not**
hot-reloaded: it changes only when the operator restarts extraction with a
new selector set. **M1 runs with the baseline scheme** and ships no
selectors (the P5 Elementor finding is a P7 site-template item; tuning on
the labelled set is forbidden, P5 D-limitation 1).

## 12. P4 browser integration

`FilterInterceptor(holder)` implements `RequestInterceptor`: builds a
REQUEST `FilterInput` (first party = `frame_url`, or the request itself for
navigations), evaluates with the currently active engine, and returns
`InterceptDecision(action=BLOCK|ALLOW, rule_id, reason=classification)`.
Two small **P4 changes** (crawler2-internal, no contract change):
`InterceptDecision` gains optional `classification` and `ruleset` fields;
`_PageObserver` aggregates decisions into bounded counters
`(classification, action) → count` plus up to 50 distinct
`(host, rule_id)` blocks, carried on `FetchResult.render`, written by the
recorder to `interceptions_by_observation` (§13). The fixed
`blocked_resource_types` policy stays in P4 and runs first. Worker wiring:
`workers.browser.interceptor = "allow_all" | "filter"` (default `filter`
once P6 ships; `allow_all` keeps B.5 #1 testable).

## 13. Storage (migration V003, keyspace `crawler2`)

| # | Table | Key | Access pattern |
|---|---|---|---|
| F1 | `filter_sources` | PK `(source)`, CK `revision_at DESC, revision` | list revisions of a source; latest; provenance lookup (url, sha256, license, importer version, counts, report JSON) |
| F2 | `filter_rules` | PK `((source, revision, bucket))` bucket = hash(rule_id) % 16, CK `rule_id` | load all rules of a revision (16 partitions, ~3–5 k rows each for EasyList) |
| F3 | `filter_rulesets` | PK `(ruleset_id)` | a ruleset = list of (source, revision), rule count, digest, engine semantics, PSL version, created_at/by, note |
| F4 | `filter_active` | PK `(name)` = `default` | the active pointer: `ruleset_id`, `previous_ruleset_id`, `activated_at`, `activated_by`; changed by LWT `IF ruleset_id = :expected` |
| F5 | `url_admission` | PK `(url_id)` | revisit gate; last admission (at, provenance, priority, queue, ruleset), last decision (classification, action, rule_id) |
| F6 | `filter_decisions_by_url` | PK `(url_id)`, CK `decided_at DESC, context` , TTL `decision_ttl_s` (90 d) | "why was this URL blocked/admitted?" — written only when the decision **differs** from F5's last decision or the URL is admitted (bounded, §17) |
| F7 | `redirect_decisions_by_observation` | PK `(observation_id)` | redirect decision of one observation |
| F8 | `interceptions_by_observation` | PK `(observation_id)` | P4 aggregate of browser decisions (§12) |
| F9 | `seeds_by_source` | PK `(seed_source)`, CK `url_id` | seed provenance: source file path + sha256, line, original text, canonical URL, imported_at, operator metadata |
| F10 | `search_results_by_query_day` | PK `((query_digest, day))`, CK `retrieved_at, engine, rank` | search provenance: engine, adapter version, query, rank, returned URL, canonical URL, title, snippet |
| F11 | `scope_sites` | PK `(registrable_domain)` | rooted sites for the scope rule (§22 Q2): origin (seed/search/operator), added_at |

Consistency: LOCAL_QUORUM writes/reads (single node locally); F4 via LWT
(the keyspace has tablets disabled, P0/P2). Rule revisions and rulesets
are immutable; only F4 moves. Partition sizes: F2 bounded by bucketing;
F6 bounded per URL by the change-only rule and TTL; F9/F10 bounded by
input size. All P6 tables are P6-owned (B.5 #2); F5/F6 are derived and
rebuildable by replaying `urls.discovered` from Scylla facts.

## 14. Hot reload

- **Identity**: ruleset id = content digest (§7). Import creates a source
  revision; `crawler2-filter publish --source easylist@<rev> …` composes a
  ruleset (validated: every referenced revision exists and its row count
  equals the recorded count; compile succeeds in the CLI) and writes F3;
  `activate <id>` moves F4 (LWT). `rollback` = activate
  `previous_ruleset_id`.
- **Discovery**: every process holding an `ActiveRuleset` polls F4 every
  `filter.reload_interval_s` (15 s; one single-row read).
- **Swap**: on a new id the process loads F2 for each revision, verifies
  counts and the recomputed digest, compiles in a worker thread (browser
  workers are asyncio), and then replaces one reference. Evaluations in
  progress finish on the engine object they already hold; the next one
  gets the new engine. No locks on the hot path.
- **Failure**: load/verify/compile error → keep the current engine, log +
  `filter_reload_failures_total`, retry with backoff; a partially loaded
  ruleset is never activated. Startup with Scylla unavailable or no active
  ruleset → `ruleset="none"` allow-all engine, `filter_ruleset_state=0`
  metric, keep polling (B.5 #1: fetching never depends on P6).
- No distributed coordination beyond the one pointer row; processes
  converge within one poll interval (decisions carry the ruleset id, so a
  mixed window is visible in the data).

## 15. Search discovery

```python
class SearchAdapter(Protocol):
    name: str
    version: str
    network: Literal["clearnet", "tor"]

    def build_request(self, query: str) -> SearchRequest: ...  # pure
    def parse(
        self, query: str, final_url: str, body: bytes
    ) -> SearchPage: ...  # pure: results | blocked | parse_error
```

`SearchRunner` executes requests with the P4 `HttpFetcher` settings
(httpx, same UA/timeouts; Tor proxy for `tor` adapters), parses with
selectolax, unwraps redirect links per engine (V1 behaviour), canonicalizes
with P1, and returns `SearchResult(engine, adapter_version, query, rank,
url, canonical UrlRef | None, title, snippet, retrieved_at)`. `blocked`
(captcha/verification detected) puts the engine in cooldown for
`search.blocked_cooldown_queries` (V1 default 999); nothing is retried or
evaded. Results are written to F10, filtered (LINK context, no first
party), and admitted with priority 60 (reason `search`). Queries come from
an operator query file (§22 Q4); adapters never derive queries from
targets. Engines enabled by default: duckduckgo, bing, brave, ahmia;
yandex and torch present but disabled (captcha / no Tor here).

## 16. Seeds

`crawler2-discover seeds FILE [--source NAME] [--meta k=v …]`: parse
(skip blank/`#`, keep line numbers), canonicalize with P1 (invalid lines
reported, not dropped silently), sort by `url_id`, write F9 rows (file
path, sha256, line, original text, imported_at, operator metadata),
filter (LINK context), add the registrable domain to F11 (scope root),
admit with priority 70, reason `seed`. Re-running is idempotent. Seed
revisits: §22 Q3 (`crawler2-discover seeds --every S` re-admits the seed
set on a static interval; no visited set anywhere — the frontier dedups
only active tasks).

## 17. Decision records, logs and metrics

| Kind | Where | Bound |
|---|---|---|
| durable link decisions | F5 (latest per URL) + F6 (history on change/admission, TTL) | O(distinct URLs × decision changes) |
| redirect decisions | F7 | one row per observation with a redirect |
| browser decisions | F8 aggregate per observation | ≤ 50 detail entries per page |
| debugging | structured log only for reload events, import reports, and a sampled (`filter.log_sample_rate`, default 0) decision log | no per-URL logging by default |
| metrics | `filter_decisions_total{context,classification,action,source}`, `filter_reload_total{result}`, `filter_ruleset_rules{source}`, `filter_ruleset_state`, `filter_eval_seconds{context}` (histogram), `discovery_admissions_total{origin,outcome}`, `discovery_links_total{outcome}`, `search_queries_total{engine,result}`, `seeds_loaded_total{result}` | labels bounded by enums and the six source names — never URL, host, path or rule id |

## 18. Failure semantics

Scylla unavailable in the admission consumer → `StorageError`, entry left
pending (P5 pattern). Frontier unavailable → `FrontierUnavailableError`,
entry left pending. Invalid rule input → importer report, no partial
revision. Engine evaluation cannot raise on untrusted URLs (property test).

## 19. Configuration (`filter.*`, `discovery.*`, `search.*`)

`filter.reload_interval_s=15`, `filter.block_threshold=0.9`,
`filter.block_classes`, `filter.cache_size=50000`,
`filter.decision_ttl_s=7776000`, `filter.log_sample_rate=0`;
`discovery.consumer_group`, `batch_size`, `block_ms`, `claim_idle_ms`,
`revisit_after_s`, `seed_revisit_s`, `scope_mode`, static priorities;
`search.engines`, `search.max_results=20`, `search.timeout_s=15`,
`search.blocked_cooldown_queries=999`, `search.query_file`.

## 20. Test plan

| # | Category | Where |
|---|---|---|
| 1–5 | model, parsers, precedence (every §6.2 consequence), overrides, provenance | `tests/unit/filtering/` |
| 6 | V1 importer: the audit counts pinned against a copy of the 98 hosts (a fixture, not the V1 file) | unit |
| 7–8 | ABP supported syntax + unsupported/invalid categories on hand-written fixtures | unit |
| 9 | deterministic compilation: shuffled inputs → same ruleset id and decisions (hypothesis) | unit |
| 10, 22 | hot reload: new version swap, invalid/incomplete ruleset keeps the old one, rollback, concurrent evaluation during swap (threads) | unit + Scylla integration |
| 11 | admission consumer on `urls.discovered` (P5 links → decisions → frontier) | Redis + Scylla integration |
| 12 | P4 interception: fixture web page with ad/tracker sub-requests through a real browser worker | integration (host browser) |
| 13 | redirect classification | unit + integration |
| 14 | search adapters: saved *synthetic* result pages per engine (no live network in CI), unwrapping, blocked detection, cooldown | unit |
| 15–16 | seed loader, admission generation | unit + integration |
| 17–18 | labelled corpus: false-positive guard and per-class precision/recall | `tests/unit/filtering/test_corpus.py` + benchmark |
| 19 | ≥ 100 k decisions/s | `benchmarks/p6-filter/` |
| 20–21 | Redis/Scylla integration, two admission consumers + two workers | integration |
| 23 | M1 fixture loop: seeds → worker → extract → admit → worker … on the fixture web, terminates by scope + revisit gate | integration |
| 24 | 24 h M1 run | `benchmarks/p6-m1/` (monitor script + results JSON) |

Regression: P1–P5 suites run unchanged at Gate H.

## 21. Benchmarks and gates

- **Throughput (Gate D)**: ruleset = built_in + V1 migrated + EasyList +
  EasyPrivacy (real imports); input stream = request/link URLs replayed
  from the W691 P5 corpus (sub-resource URLs extracted from the stored
  HTML), plus synthetic misses; **cache disabled**; single process;
  report compile time, rules per kind, untokenized count, RSS, decisions/s
  warm (JIT-free steady state after 1 pass) and cold (first pass), p99
  latency. Cached throughput reported separately, never used for the gate.
- **False-positive safety (Gate E)**: labelled corpus (~400 items: seed
  and W691 content pages, player/embed hosts, media/manifest URLs,
  navigation, ad/tracker URLs harvested from the corpus pages' script/img
  references, redirects, first/third-party pairs, adversarial URLs such as
  `/ads-free-movie/`, `.click` download hosts, `track-list` paths). Labels
  are assigned by hand before the ruleset is evaluated and committed as a
  small JSON fixture (URLs only, no bodies). Report per-class precision /
  recall where a class has ≥ 20 labelled items, otherwise "insufficient".
  Hard assertion: **no labelled CONTENT/MEDIA/PLAYER item is BLOCKed by a
  generic (`url_pattern`/`host_label`/third-party) rule**; intended
  overrides are operator rules and appear with provenance.
- **M1 (Gate F/G)**: §22 Q2–Q4 define the configuration; the monitor
  samples every 60 s: worker states/restarts, RSS and open FDs per
  process (browser processes included), asyncio task counts (worker
  debug endpoint), Redis `used_memory`/clients/frontier stats, stream
  lag/pending per group, Scylla disk + write counts, MinIO bucket size,
  filter/admission metrics, frontier `audit()` every hour. "No leak" =
  RSS, FDs, Redis clients, browser processes and task counts show **no
  monotonic growth over the last 12 h** (linear-fit slope within noise,
  stated numerically), and engine/caches stay at their bounds.
  "V1-or-better metrics" = P0 baseline table (pages/s, fetch success,
  bytes/page, browser share, CPU/RSS, media/1k) compared on the same seed
  set; P4's open success-gate definition is **not** changed by M1.

## 22. Decisions needing approval (Gate B)

| # | Question | Options | Proposed |
|---|---|---|---|
| Q1 | Where are discovered links filtered? | **A** admission time only (`crawler2-discover admit`); P5 extraction keeps allow-all so links stay complete facts and P5 output never depends on the ruleset. **B** also plug P6 into P5 `LinkPolicy` (blocked links are not stored) | **A** — B makes `link_set` hashes and stored facts depend on the ruleset active at first extraction (extracts are reused per raw version) |
| Q2 | M1 crawl scope (static, explicit) | **A** rooted sites: follow in-scope links on seed/search/operator sites (registrable domain in F11); external links are fetched once as *leaves* (not expanded). **B** same-site only. **C** open crawl bounded only by frontier depth | **A** — V1-like breadth without whole-web expansion; no value judgement |
| Q3 | M1 revisits (no visited-forever, no intelligence) | **A** static uniform: seeds re-admitted every 6 h; a rediscovered URL is re-admitted when its last admission is ≥ 24 h old. **B** first-sight only (no recrawl → no change history for P7) | **A** — P7's data-sufficiency gate needs pages observed ≥ 3 times |
| Q4 | Search queries for M1 | **A** you supply an operator query file (generic piracy terms or titles you choose; stored with provenance). **B** M1 seeds-only; search adapters validated on fixtures + one short live smoke run | your call — P6 must not invent queries |
| — | New dependency `publicsuffixlist` (MPL-2.0) for third-party | — | approve unless you prefer `tldextract` |
| — | EasyList + EasyPrivacy downloaded by an explicit import command, not vendored, license header recorded | — | approve |

## 23. Commit plan

1. `docs(p6): audit and design` · 2. `feat(storage): p6 filter/discovery
tables (V003)` · 3. `feat(filtering): rule model, engine, precedence` ·
4. `feat(filtering): v1 blacklist and abp importers, rule store, hot
reload, crawler2-filter` · 5. `feat(crawlers): plug the filter into
browser interception` · 6. `feat(discovery): seeds, search adapters,
admission consumer, crawler2-discover` · 7. `test(...)` suites ·
8. `test(bench): p6 throughput and corpus results` · 9. `docs(p6):
implementation, validation, benchmarks, decisions; architecture updates` ·
10. M1 results.
