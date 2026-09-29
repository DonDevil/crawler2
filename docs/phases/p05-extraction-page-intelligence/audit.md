# P5 Audit — extraction & page intelligence

Status: **audit complete (2026-09-29).** No code in this step.
Inputs: V1 `~/anti_piracy/crawler` at `2dfb542` (read-only), V2 P1–P4 as of
`82c84b7`. Design: [design.md](design.md).

## 1. What V1 has

| V1 module | LOC | What it does | Called from |
|---|---|---|---|
| `parsers/html_link_extractor.py` | 120 | BeautifulSoup(lxml) parse → `<a href>` → `urljoin` → `URLUtils.clean_url` → `should_queue_link` → external budget (10/page); then regex over every `<script>` string and over `soup.get_text()` | every V1 crawler (`extract_content`) |
| `parsers/media_link_detector.py` | 65 | **second** BeautifulSoup parse; `<a>`, `<video|audio|iframe|embed src>`, nested `<source src type>`, regex over scripts and text; keeps only URLs whose extension/MIME classifies as media | `extract_content` |
| `parsers/javascript_link_extractor.py` | 25 | regex `https?://[\w\-.:/?&=%#]+` + `clean_url` | both of the above |
| `parsers/page_metadata_parser.py` | 33 | third soup: `<title>`, description/og/twitter tags | **nothing** (dead code) |
| `parsers/streaming_manifest_parser.py` | 114 | HLS `#EXT-X-STREAM-INF` / DASH `Representation/BaseURL` variant lists | media evidence path only |
| `utils/url_utils.py` | 802 | classmethod object: normalization (fragment drop, lowercase host, tracking-param removal, `urlencode` re-encoding), scheme/host checks, media classification by extension/MIME, trap heuristics, adult/ad/blacklist policy with a file-backed, mtime-cached global blacklist, link relevance and priority | everywhere |

V1 has **no page hashing, no page versions and no change detection**, and
it stores **no page content** (plan D16). The only V1 digest is
`media_evidence_store` (sha256 of a cleaned media URL = V2 `MediaId`).

## 2. V1 behaviour that violates the V2 architecture

| # | V1 behaviour | Why it is wrong in V2 | Evidence |
|---|---|---|---|
| V-1 | HTML parsed **twice** per page (link extractor + media detector), a third parser exists unused (D8) | P5 single-parse invariant; CPU on the hot path | `extract_content` calls `extract_links` and `extract_media_links`, each `BeautifulSoup(html, "lxml")` |
| V-2 | `get_text()` computed twice more (link and media paths) and regex-scanned in full | repeated full-document traversals | same files |
| V-3 | `clean_url` **writes to the blacklist file** when it sees an "adult" URL (`add_to_blacklist`) | hidden persistence inside a pure-looking URL function; shared file written by crawler processes (D6) | `url_utils.py:776-800`, `:325-365` |
| V-4 | blacklist/adult/ad/relevance/priority decisions inside URL cleaning (`clean_url`, `should_queue_link`, `get_link_priority`) | policy belongs to P6 (filter) and P7 (priority); extraction must report facts | `url_utils.py:393-540` |
| V-5 | global mutable class state (`_blacklist_path`, `_blacklist_mtime_ns`, stat per call) | caused the 77× latency bug (D9); not multi-host safe | `url_utils.py:240-570` |
| V-6 | tracking-parameter removal and `parse_qsl`/`urlencode` re-encoding inside normalization | changes resource identity; P1 canonical v1 forbids it (ADR-008) | `normalize_url` |
| V-7 | scheme-less strings get `http://` prepended | invents URLs from relative paths (`"example.com/x"` vs path `example.com/x`) | `normalize_url` |
| V-8 | "trap" heuristics (`\d{4}/\d{2}/\d{2}`, `calendar`, `date=`, `sessionid=`) silently drop links | a crawl-policy guess applied as a fact filter; hides URLs from P7 | `is_probable_trap` |
| V-9 | external-link budget of 10 per page | crawl policy (P7), not extraction | `max_external_links_per_page` |
| V-10 | parsing runs synchronously on the asyncio loop of the fetch worker | extraction coupled to fetching (P4/P5 boundary) | `async_crawler.py:198-202` |
| V-11 | `visited` treated as permanent; no re-observation semantics | page memory must be versioned with first/last seen | frontier/crawler code (P3 audit) |

Things **not** found in V1 extraction: network access, retries, target-specific
logic in the parsers themselves (target logic lives in V1 discovery), or a
page→single-target assumption. V2 must simply not introduce them.

## 3. KEEP / CHANGE / DROP / DEFER

| V1 heuristic | Verdict | Purpose and V2 boundary |
|---|---|---|
| `<a href>` links resolved against the page URL | **KEEP** | core link fact → `LinkRelation.ANCHOR` |
| `<iframe>/<embed src>` treated as media candidates | **CHANGE** | iframe/frame `src` is a *page* link (`IFRAME`, embedded players are pages); `<embed>` stays a media candidate |
| `<video|audio src>`, `<source src type>` | **KEEP** | media references (`VIDEO_ELEMENT`/`SOURCE_ELEMENT`), `type` → `declared_type` |
| `<a href>` to a media file counted as media | **KEEP** | `DiscoveryMethod.LINK` |
| media classification by extension and MIME (`VIDEO_/AUDIO_/STREAMING_EXTENSIONS`, `mpegurl`, `dash+xml`) | **KEEP**, mapped to P1 `MediaKind` | pure function; `.ts`/`.m4s` segments → `VIDEO_FILE`, `.m3u`/`.m3u8` → `HLS_MANIFEST`, `.mpd` → `DASH_MANIFEST`. Document/archive classes dropped (not media in P1) |
| absolute-URL regex over inline scripts | **CHANGE** | also JSON-escaped `https:\/\/`, protocol-relative quoted `//host/…`, quoted relative media paths; trailing-punctuation trim; bounded input. Relation `SCRIPT_LITERAL` |
| regex over the whole page text | **CHANGE** | run once over the visible text P5 already computes for hashing (no extra traversal); relation `OTHER` |
| `<title>` / og tags (dead parser) | **CHANGE** | revived as the metadata extractor with an explicit field list (design §8) |
| fragment removal, host lowercase, scheme check | **KEEP** via P1 `canonicalize_url` | already frozen in P1; not re-implemented |
| tracking-parameter stripping | **DROP from identity / DEFER** | P1 forbids it; a "same resource" relation is P6/P7 work |
| `http://` prefix for scheme-less strings | **DROP** | RFC 3986 resolution only |
| trap heuristics, adult/ad detection, blacklist, `should_queue_link`, `get_link_priority`, external budget | **DROP from P5 / DEFER to P6 (filter) and P7 (priority)** | P5 exposes an injectable `LinkPolicy` hook, default allow-all |
| blacklist file writes from URL cleaning | **DROP** | no hidden persistence; P6 rules are central (B.4 #2) |
| HLS/DASH manifest variant parser | **DEFER to P8** | P4 probes media and does not store manifest bodies as pages; variant sets are level-2 `TechnicalHints` (P1 §9 assigns "HLS variant hints" to P8). The pure parser can be ported there |

## 4. P1–P4 facts that constrain P5

1. **`PageVersionId` is derived from the raw body digest** (`ids.py:162`,
   `derive(final.url_id, body_digest)`, validator in `PageObservation`).
   P4 computes it at recording time; P2 `page_versions_by_url` (W8)
   already keeps `first_seen`/`last_seen` per **raw** version; W9 links and
   M4 media are keyed by it. The plan's P5 rule ("a version only when the
   normalized hash changes") therefore **cannot** be implemented by
   `PageVersionId` without changing a frozen ID derivation and a field's
   meaning (B.5 #5). → design §10, decision D-1.
2. `page.changed` was deliberately deferred to P5 as an additive event
   (P1 design §7/§9). `page.fetched` was renamed `page.observed` in P1;
   P5 must not reintroduce it.
3. Component `extraction` already owns `urls.discovered` and
   `media.discovered` and consumes `page.observed` (P1 ownership table).
4. P4 stores **every** response body (any status, incl. blocked pages)
   content-addressed in MinIO before writing the observation (`recorder.py`);
   identical bytes dedupe to one object. A `304` creates no observation, so
   P5 never sees it (P4 §17).
5. P2 has no write path for `media.discovered` and none for page
   revisions/hashes; `LinkRepository.record` (pattern B) writes W9/W10 but
   not `url_state` — `UrlRepository.record_discovered` does W11–W13.
6. `ObjectStore.get(BlobRef)` returns verified bytes (digest + size).
7. `selectolax` is **not installed** in the V2 env (it is listed but unused
   in V1's requirements). No compatibility reason against it was found.

## 5. Corpus availability (golden tests, benchmark, precision set)

- V1 never stored page bodies (D16); `crawler/benchmark/results` holds
  only crawl metrics.
- The P4 gate runs used a logging recorder: **no bodies were kept**;
  the MinIO `crawler2-raw` bucket is empty and the integration buckets
  contain only test fixtures.
- Therefore **no saved real pages exist** in either repository. See
  design §18 and decision D-2.

## 6. How V1 parse CPU can be measured fairly

V1's `HTMLLinkExtractor.extract_content(html, url)` is a pure call once
the blacklist path points at a scratch copy (it otherwise *writes* to
`datasets/domain_blacklist.txt`, V-3). It can be timed in V1's own venv
(bs4 4.15.0, lxml 6.1.2, Python 3.12.3) read-only with
`PYTHONDONTWRITEBYTECODE=1`, using `time.process_time()` per page on the
same HTML bytes given to V2. This isolates parse+extraction CPU from
network, storage and browser time. V1 does less work than V2 (no metadata,
no hashes), so comparing V1 `extract_content` against the **full** V2
pipeline is conservative for V2.
