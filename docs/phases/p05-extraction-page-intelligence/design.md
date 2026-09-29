# P5 Design — extraction & page intelligence

Status: **PROPOSED — awaiting design review (plan B.1).** Two decisions
need the reviewer: **D-1** (page-version identity) and **D-2** (real-page
corpus). Audit: [audit.md](audit.md).

## 1. Pipeline and boundaries

```
page.observed (P4, outbox → Redis)            crawler2-extract (component `extraction`)
   │  IdempotentConsumer (P2), key = observation_id
   ▼
eligible?  2xx · HTML-ish content type · snapshot present      else: metric, done
   │
extract of this raw page_version already stored? ── yes ──► reuse it (no fetch, no parse)
   │ no
ObjectStore.get(snapshot)  (digest-verified bytes)
   ▼
parse_page(bytes)  ← the ONLY parser call               ParsedPage (one selectolax tree)
   ▼
one pass over the tree ─► links · media · metadata · script URLs · visible text · shape
   ▼
PageHashes (6) + PageExtract (pure data)
   ▼
PageIntelligenceWriter (P2 repositories only)
   links (W9/W10) + urls.discovered │ url_state (W11–W13) │ extract row + media.discovered
   revision sighting (first/last seen) + page.changed if the normalized state is new
   archival decision row
```

| Phase | Owns | P5 provides / consumes |
|---|---|---|
| P4 | fetching, snapshot upload, `page.observed` | consumed; P4 code unchanged |
| P5 | parse, extraction, hashes, revisions, change events, archival *decision* | this design |
| P6 | ad/tracker/blacklist policy | injectable `LinkPolicy` and `NormalizationRules` hooks, defaults documented |
| P7 | priority, recrawl, value, learned archival profile | consumes `urls.discovered`, `page.changed`; supplies `ArchivalProfile` later |
| P8 | media identity, probing, manifests | consumes `media.discovered` |

Extraction code (`crawler2/extraction/`) is pure: no network, no Redis,
no Scylla, no clock. Only the writer and the CLI touch P2.

## 2. Parsed tree ownership and lifetime

`parse_page(body, *, url, content_type) -> ParsedPage` is the single call
site of `selectolax.lexbor.LexborHTMLParser`. `ParsedPage` holds the tree,
the effective base URL (`<base href>` if valid, else the final URL) and
the decoding used. It lives for one `extract()` call and is dropped;
nothing is cached across pages. Extractors receive it read-only; **no
extractor mutates the tree** (text/shape extraction skips subtrees
instead of deleting them), so extractor order cannot change results.

Decoding: `charset` from the `Content-Type` header, else `<meta charset>`
/ `http-equiv` sniffed in the first 4 KiB, else UTF-8; always
`errors="replace"`. The raw hash never sees decoded text.

## 3. Extractor interfaces

```text
class PageExtractor(Protocol):          # one per fact family
    def extract(self, page: ParsedPage) -> ...: ...

def extract(body: bytes, *, url: UrlRef, content_type: str | None,
            rules: NormalizationRules = BASELINE, policy: LinkPolicy = ALLOW_ALL
            ) -> PageExtract
```

Implemented as one walker that visits each element once and feeds
per-family collectors (links, media, metadata, script text, visible text,
shape), plus a few C-level CSS selections where cheaper (`meta`, `link`).
No plugin registry: the families are fixed by this design.

## 4. URL handling (`crawler2/extraction/urls.py`, pure functions)

| Function | Rule |
|---|---|
| `clean_reference(raw)` | strip ASCII whitespace/controls (HTML attribute rules), reject > 8 KiB |
| `scheme_of(raw)` / `is_web_scheme` | only `http`/`https` survive; `mailto:`, `javascript:`, `data:`, `tel:`, `ftp:`, `blob:`, `about:`… are dropped and counted per scheme class (bounded label set) |
| `resolve(base, ref)` | RFC 3986 via `urllib.parse.urljoin`; protocol-relative inherits the base scheme; fragment-only refs resolve to the page itself and are dropped as self-links |
| `to_url_ref(abs)` | **P1 `canonicalize_url`** (fragment removed, host IDNA/lowercase, default port, dot segments, escape normalization). Invalid → dropped, counted. No tracking-param removal, no query reordering, no `www` stripping |
| `media_kind(url, declared_type)` | V1 extension/MIME tables mapped to `MediaKind` |
| `LinkPolicy` (P6 hook) | `allow(source: UrlRef, target: UrlRef, relation) -> bool`, default allow-all; P5 ships no rules |

## 5. Links

Sources: `a[href]`, `area[href]` → `ANCHOR`; `iframe[src]`, `frame[src]`
→ `IFRAME`; `link[rel~=canonical]` → `CANONICAL`; `meta[http-equiv=refresh]`
`url=` → `META_REFRESH`; inline-script literals → `SCRIPT_LITERAL`; bare
URLs in visible text → `OTHER`. `nofollow` from `rel`. Anchor text:
whitespace-collapsed, NFC, ≤ 300 chars. Identity = (`url_id`, relation),
matching W9's clustering; first occurrence in document order wins. First
10 000 unique links in document order are kept (P1 batch max; truncation
counted), then sorted by (url, relation) for determinism. Self-links
(target == page) are kept only for `CANONICAL`.

## 6. Media

Sources and `DiscoveryMethod`: `video[src]`, `audio[src]` →
`VIDEO_ELEMENT`; `source[src]` (+ `type`) → `SOURCE_ELEMENT`; `embed[src]`,
`object[data]` → `VIDEO_ELEMENT` when classifiable; `data-src`/`data-video`
on `video|audio|source` → same methods; `meta` `og:video[:url|:secure_url]`,
`og:audio`, `twitter:player:stream` → `META_TAG`; JSON-LD
`contentUrl`/`embedUrl` and player config literals → `PLAYER_CONFIG`
(when the literal sits in a `ld+json` script or a `file`/`src`/`source`/
`hls`/`url` key) else `SCRIPT_LITERAL`; `a[href]` to a media file → `LINK`.
Element sources keep unclassifiable URLs as `UNKNOWN` (the element says
it is media); link/script/meta sources require a classifiable kind.
Identity = canonical locator (the `MediaId` basis); one reference per
locator, method chosen by fixed precedence element > meta > player config
> link > script, then document order. Cap 1 000 (P1). No download, no
probe, no fingerprint, no target.

## 7. JavaScript URL extraction (no execution)

Input: text of inline `<script>` elements (any `type`, incl. JSON-LD),
≤ 1 MiB per script and ≤ 4 MiB per page (truncation counted). Supported
syntax, exactly:

1. absolute `http(s)://…` literals, also with JSON-escaped slashes
   (`https:\/\/…`) and `/`;
2. quoted protocol-relative literals `"//host/path"` / `'//host/path'`;
3. quoted relative or absolute-path literals ending in a media extension
   (`"/v/x.m3u8"`), for media only.

Trailing `)`, `]`, `}`, `,`, `;`, `.`, `'`, `"`, `\` are trimmed. Not
supported (documented limitation): string concatenation, template
literals with expressions, base64/obfuscated strings, URLs built at
runtime, external script files (never fetched).

## 8. Metadata (only fields with a consumer)

| Field | Source | Consumer |
|---|---|---|
| `title` | first `<title>`, collapsed, ≤ 300 | P7 page classification, P12 evidence, P13 |
| `lang` | `<html lang>` | P7 target discovery by language |
| `canonical` | `link[rel=canonical]` (also a link) | P7 duplicate/mirror relations |
| `og_title`, `og_type` | OpenGraph | P7 content-kind hint (`video.movie`, `video.episode`) |
| `published_at`, `modified_at` | `article:published_time`, `article:modified_time`, `og:updated_time`, `itemprop=datePublished|dateModified`; ISO-8601 → UTC, else dropped | P7 freshness/recrawl |
| `robots` | `meta[name=robots]` content, ≤ 100 | P7/ADR-005 compliance hints |

Description/keywords/twitter tags are not kept (no consumer).

## 9. Hash set (all `sha256:<hex>`, i.e. P1 `ContentDigest`)

Each hash is `sha256(tag + "\n" + canonical_form)` with a versioned tag,
e.g. `antipiracy/page-hash/normalized/v1`; changing a rule bumps the tag.

| Hash | Canonical form |
|---|---|
| raw | `body_digest` of the observation (exact bytes; `ContentDigest.of_bytes`), not recomputed from decoded text |
| visible text | text nodes outside `script, style, noscript, template, head, svg, math, iframe, object, canvas` and outside elements with `hidden`, `aria-hidden="true"`, or inline `display:none`/`visibility:hidden`; NFC; whitespace runs → one space; case kept; joined by `\n` per block element |
| normalized | visible text **minus** baseline boilerplate regions (`nav, footer, aside, [role=navigation|contentinfo|complementary|banner]`) and minus elements matched by injected `NormalizationRules` (P6); NFKC + casefold + whitespace collapse; **plus** the sorted media-set lines (a swapped video is a meaningful change). Links are not part of it (ad hrefs rotate) |
| link set | sorted unique `relation \t canonical_url` lines |
| media set | sorted unique `kind \t canonical_locator` lines |
| structural | preorder `depth:tag` of every element (tag names only; no text, ids, classes or attributes), depth capped at 256, element count capped; comments/doctype excluded |

Baseline normalization is **not** an ad blocker: it removes invisible
content and landmark boilerplate only. Ad text rendered in the main
region still changes the normalized hash until P6 supplies rules.
Non-HTML observations are not parsed and get no revision (§10).

## 10. Page versions — decision D-1 (needs review)

**Conflict.** P1 `PageVersionId = derive(final.url_id, body_digest)` is
the *exact-bytes* state; P4 already creates one per raw change and P2 W8
keeps its first/last seen. The plan asks for a version only when the
**normalized** hash changes. Changing `PageVersionId` would change a frozen
ID derivation and a field's meaning (B.5 #5) and would force P4 to parse.

**Proposal (additive, ADR-020):**

- Keep `page_version_id` exactly as P1 defines it (raw state). Links (W9)
  and extracts stay keyed by it — identical bytes give identical facts.
- Add **`PageRevisionId`** (P1 minor, derived: `url_id`, normalization
  tag, normalized digest; prefix `pgr`) = the P5 "PageVersion": one
  meaningful content state of a URL. Everything this phase calls "page
  version" is a revision; the docs say so explicitly.
- A revision row is created on the first sighting of its normalized state
  and only its `last_seen`/`last_observation_id` move afterwards
  (`EW`/`LW` write timestamps, P2 ADR-012). Two workers seeing the same new
  content derive the same ID → one row; replay rewrites identical cells.
  No check-then-insert on the row itself.
- **Revert (A → B → A)** re-sights revision A (its `last_seen` moves) and
  creates no new revision. A version-per-transition model needs an ordered
  allocator (LWT or a lock) that P2 does not use outside evidence.
  Intelligence sees the revert through W5/revision `last_seen`.

## 11. last_seen

Per observation, the writer upserts the revision sighting with
`first_seen` `EW(observed_at)` and `last_seen` `LW(observed_at)`. Order of
arrival does not matter; an older observation processed late cannot move
`last_seen` backwards. The raw-version `last_seen` (W8) continues to be
maintained by P4. A `304` produces no observation (P4), so it does not
move `last_seen`; that is a documented limitation for P7.

## 12. Change detection and `page.changed`

Meaningful change := the observation's revision (normalized state) was
never seen before at this URL. Other hashes are diagnostic signals
carried in the event and extract row (e.g. raw changed + normalized
unchanged = ad rotation, timestamps or volatile markup).

`page.changed` (new event, P1 minor, producer `extraction`, consumer
`crawl_intelligence`), payload: `page_observation_id`, `page` (final
`UrlRef`), `page_version_id` (raw), `revision_id`, `normalization` tag,
`hashes` (the six digests), `observed_at`. Idempotency key:
`revision_id`. Emission: the writer point-reads the revision; if absent it
writes the sighting **with** the event in one logged batch (pattern A),
otherwise without. A concurrent first sighting may emit twice — same
idempotency key, deduplicated by consumers (at-least-once contract). No
event is emitted for re-sightings, raw-only changes or non-HTML pages.
`page.observed`/`fetch.completed` are unchanged; no `page.fetched`.

## 13. Raw snapshot archival policy

Today P4 stores every body (content-addressed). P5 does **not** change
that and deletes nothing. P5 adds the explicit decision:

```text
@dataclass(frozen=True) class ArchivalProfile:     # supplied by the caller
    evidence_candidate: bool = False               # P12 later
    high_value: bool = False                       # P7 later (no model in P5)
    sample_rate: float = 0.05
decide(profile, *, observation_id, new_revision) -> RETAIN(reason) | SAMPLED_OUT
```

Rules: evidence candidate → retain; high value and new revision → retain;
new revision → retain (first sighting of a content state); else retain iff
`uuid(observation_id) mod 10⁴ < rate·10⁴` (deterministic on every host).
The decision is stored per (snapshot digest, observation) so a future GC
(deferred, P13/P14 after ADR-005) keeps a blob if any row retains it.
Until P7/P12 exist, the profile is the configured default.

## 14. P2 integration (additive; V002 migration)

| Need | P2 change |
|---|---|
| extract per raw version (hashes, metadata, counts, revision id) | table `page_extracts` (pk `page_version_id`), repository `PageIntelligenceRepository.record_extract(extract, event=MediaDiscovered?)` (pattern A) / `extract(page_version_id)` |
| revision history with first/last seen | table `page_revisions_by_url` (pk `url_id`, ck `revision_id`) — `record_sighting(sighting, event=PageChanged?)`, `revision(url_id, revision_id)`, `revisions(url_id)` |
| archival decision | table `snapshot_retention` (pk `digest`, ck `observation_id`) — `record_retention(decision)` |
| links + `urls.discovered` | existing `LinkRepository.record` (pattern B) + `UrlRepository.record_discovered` |

Write order (first extraction of a raw version): links (+`urls.discovered`)
→ url_state → extract row (+`media.discovered`) → sighting (+`page.changed`)
→ retention. The extract row is the commit marker: redelivery after a
crash redoes everything before it idempotently and skips it afterwards.
Re-sighting of known bytes: sighting (+`media.discovered` for P8's
per-observation sightings, attached to the sighting batch) → retention;
links are not rewritten (same bytes, same links).
`page_revisions_by_url` bound: one row per *normalized* state; 100 MB ≈
300k revisions of one URL, measured in validation.

## 15. Event semantics summary

| Event | Producer | When | Idempotency |
|---|---|---|---|
| `urls.discovered` | extraction | first extraction of a raw version with ≥ 1 link | `page_observation_id` (P1) |
| `media.discovered` | extraction | every eligible observation with ≥ 1 media reference | `page_observation_id` (P1) |
| `page.changed` (new) | extraction | first sighting of a revision | `revision_id` |

## 16. Errors

Malformed HTML never raises (lexbor recovers). Oversized input: bodies >
`max_parse_bytes` (8 MiB) are parsed only up to the limit and flagged
`truncated`. Walk bounded by `max_elements` (200 k) and depth (256).
Undecodable bytes → replacement characters. Snapshot missing/corrupt
(`IntegrityError`) or `StorageUnavailableError` → the handler raises, the
event is not marked processed and is redelivered. A per-page extractor
bug is caught once at the top, counted (`p5_extract_failures_total`),
logged with the observation id, and the event is marked processed with no
revision (poison pages must not block the stream).

## 17. Observability (no URL/domain labels)

`p5_pages_total{result=extracted|reused|skipped_status|skipped_type|no_snapshot|failed}`,
`p5_parse_seconds`, `p5_extract_seconds`, `p5_links_per_page`,
`p5_media_per_page`, `p5_metadata_fields_total{field}`,
`p5_dropped_refs_total{reason=scheme|invalid|policy|cap}`,
`p5_revisions_total{kind=new|resighted}`, `p5_truncated_total{what}`,
`p5_archival_decisions_total{decision,reason}`.

## 18. Benchmark and corpora — decision D-2 (needs review)

No saved real pages exist (audit §5). The P5 CPU gate and the precision
set need representative HTML of the pages V2 actually crawls (streaming/
piracy portals: heavy inline scripts, player configs, ad slots).
Options:

- **(a) Recommended:** one controlled capture of the HTML documents of the
  W691 URLs (HTML only, no media, through the existing V2 worker so bodies
  land in MinIO as normal P4 snapshots), captured **twice** some hours
  apart. Bodies stay in MinIO/scratch, **never in git**. This gives the
  benchmark workload and real page pairs to hand-label. Git gets only
  small sanitized, hand-written fixtures derived from observed structure
  plus the result JSON.
- (b) No capture: synthetic pages only. The gate would be measured on
  hand-built HTML, which is weaker evidence and I would report it as such.

Method either way: same bytes to V1 (`extract_content`, V1 venv, blacklist
path in scratch) and V2 (`extract`); `time.process_time()` per page; 1
warm-up pass; 5 measured passes; report median, p95 and aggregate
CPU/page, page-size distribution, versions, hardware. The gate is
aggregate V2 CPU/page ≤ 0.5 × V1. V2 is timed doing the full pipeline
(more work than V1).

## 19. Tests

Unit (pure): URL functions (+ hypothesis: resolution never raises,
canonical output idempotent), each extractor, JS syntax table, metadata,
visible text, normalization, shape, each hash, ordering/determinism,
empty/malformed/Unicode/invalid bytes/huge attributes/deep nesting.
Single-parse: a counting parser factory proves one parser construction
per `extract()` and that every family consumed the same tree object.
Hash stability: same page twice; ad/boilerplate rotation keeps the
normalized hash; text, link, media, structure changes move the respective
hashes. Revision logic: first sighting, re-sighting, raw-only change,
meaningful change, revert, replay, concurrent identical (fake
repository + real Scylla). Golden: committed small fixtures with expected
extracts and hashes. Integration (real Scylla/MinIO/Redis): observation →
consumer → rows, events in outbox, idempotent replay, two concurrent
consumers → one revision. Precision: hand-labeled pairs (§18).

## 20. Compatibility

P1: additive minor only (`PageRevisionId`, `page.changed`); no existing
field or ID changes. P2: additive migration and one new repository;
existing methods unchanged. P3: untouched (frontier keeps admitting
`urls.discovered`). P4: untouched. P6 plugs into `LinkPolicy` and
`NormalizationRules`; P7 consumes `page.changed`, supplies
`ArchivalProfile`; P8 consumes `media.discovered` (and may port the
manifest parser). Target-independent throughout: no target id is read,
stored or keyed.
