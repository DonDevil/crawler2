# P8 Audit — media registry & identity

Status: **audit complete (2026-10-09; R-2 corrected after the design review).** The findings
below describe the repository **before** P8 code was written and are kept
as recorded. Design: [design.md](design.md) (approved for implementation
2026-10-09). Decisions: [decisions.md](decisions.md). Implementation
status: [validation.md](validation.md), [exit.md](exit.md).

Scope: the P8 section of [the phase plan](../../v2-phase-plan.md) (§P8),
B.1/B.3/B.5, and everything P0–P7 already froze that P8 must build on.
Nothing was crawled or collected. The only data read was the git-ignored
M1 export (`var/p7/m1-export/extracts.jsonl.gz`), read-only (§5).

## 1. What the plan asks of P8

- **Goal:** separate URL identity from content identity (V1 defect D11).
- **Identity hierarchy**, levels 1–5. P8 owns levels 1–3; levels 4–5 are
  the fingerprinter's (P9/P10). A cheaper level never asserts what only a
  stronger one can.
- **Develop:** a registry consuming `media.discovered`; a lightweight probe
  (HEAD / range / first-segment hash, with the fingerprinter's SSRF guard
  ported) run by a media-probe pool, not the crawler; `encode.requested`
  only for content identities lacking the required representation, learnt
  from `representation.ready`. Full media download stays in the
  fingerprinter.
- **Tests:** byte-identical video on 3 URLs → one level-3 identity and one
  encode; same media in different encodings/packaging → distinct level-3
  identities, not merged; crafted level-3 collision → not the same media;
  same URL with changed content → a new version; SSRF suite ported.
- **Exit gate:** zero duplicate encode tasks for byte-identical content in
  a synthetic dedup benchmark; zero false merges in the different-content
  test set.
- **Not P8:** encoder, target manager (P9); vector index, matcher and the
  level-5 equivalence relation (P10); feedback (P11); evidence (P12);
  analytics (P13).

## 2. What already exists (frozen, reused as is)

### 2.1 P1 contracts (`contracts/src/antipiracy_contracts`)

| Contract | What it fixes for P8 |
|---|---|
| `models/media.py` | the identity hierarchy as types: `MediaReference` (P5 output), `Media` (level 1), `TechnicalHints` (level 2), `ContentKey` (level 3, `scheme` + `digest` → `ContentId`), `ProbeStatus`, `MediaObservation` (one per (page observation, media)) |
| `ids.py` | `MediaId.of_locator(CanonicalUrl)` (tag `antipiracy/media/v1`), `ContentId.of(scheme, digest)` (tag `antipiracy/content/v1`); both deterministic, computable on any host |
| `urls.py` | `canonicalize_url` v1 keeps the query string (escapes normalized only), drops the fragment |
| `events/media.py` | `media.discovered` (producer extraction, consumer media registry, key `page_observation_id`, ≤ 1,000 refs); `media.observed` (producer media registry; key `(page_observation_id, media_id)`) |
| `events/fingerprinting.py` | `encode.requested` (producer media registry; key `(content_id, required_spec)`), `representation.ready` and `encode.failed` (consumer media registry) |
| `models/representations.py` | `representation_id` is derived from `(content_id, spec)` only: all media sharing a `ContentId` share one representation, and `match.found` is keyed by `content_id`. A level-3 share is therefore a merge for everything downstream (design §2, §3) |
| `ownership.py` | `Component.MEDIA_REGISTRY` in service `crawler2` |

`MediaObservation` validates that a content key only comes with
`probe_status = probed`. `Media` validates that `media_id` is derived from
the locator. A cheap level therefore cannot carry a strong claim.

### 2.2 P2 storage (`crawler2/storage`, V001)

| Table | Kind | Repository |
|---|---|---|
| M1 `media` | derived (M2) | `ScyllaMediaRepository.get` |
| M2 `media_observations` | authoritative, written with the `media.observed` outbox row | `record_observation`, `observation`, `observations_on` |
| M3 `media_observations_by_media` (month × 16 shards) | authoritative copy | `sightings` |
| M4 `media_by_page_version` | derived (M2) | `on_page_version` |
| M5 `media_by_content` (8 shards) | derived (M2) | `by_content` |
| M6 `media_content_versions` (per month) | derived (M2) | `content_versions` |
| P1 `representation_status` | projection of `encode.*`/`representation.ready` | `ProjectionRepository.mark_encode_requested`, `apply_representation_ready`, `apply_encode_failed`, `representation_status` |

`rebuild_media` re-derives M1/M4/M5/M6 from M3 → M2. Partitions are
bounded by month buckets and shards (P2 schema.md). These are tested
(P2 validation) but have **no producer and no consumer yet**.

Not present: no outbox write path for `encode.requested`
(`mark_encode_requested` writes only the projection row), no table for
probe results, and no claim that makes "one encode per content" hold
across hosts.

### 2.3 P4 fetch

- `Expectation.MEDIA_PROBE` and the D2 invariant (P4 §18): a media URL is
  requested with `Range: bytes=0-(N-1)`, `N = media_probe_bytes` (64 KiB),
  and at most `N` bytes are read.
- `MediaProbe` (P4 internal dataclass) carries content type, declared and
  `Content-Range` total length, `Accept-Ranges`, whether Range was
  honoured, ETag, Last-Modified, bytes read, and a container sniff (mp4,
  webm, mpegts, flv, mp3).
- **It carries no digest of the bytes read.** Level 3 cannot be computed
  from today's `FetchResult`.
- ADR-017: "media probe metadata stays in `FetchResult` until P8 defines
  its persistence".
- The `media_probe` host role exists in settings (B.4 #6). Nothing uses it.
- crawler2 has **no SSRF guard** anywhere (no private/loopback address
  check on any fetch path).

### 2.4 P5 extraction

- `media.discovered` is emitted in the sighting batch of every eligible
  observation with ≥ 1 media reference (P5 design §14, deviation 1), so
  P8 gets one event per page observation.
- References come from `video`/`source` elements, meta tags, player
  configs, script literals and media-file links, with a `MediaKind` from
  V1's extension/MIME tables. Locators are P1-canonical `UrlRef`s.
- P5 provides no page region and no network-captured media
  (`NETWORK_CAPTURE` is in the enum and P5's method ranking, but nothing
  produces it; P4's browser blocks media resources). Manifests fetched as pages are parsed by P5, but no
  manifest-variant fact is emitted today.

### 2.5 ADR-022 / ADR-023 (event delivery)

- ADR-022: a consumer group on a stream **holds back publication** of that
  stream when it falls behind, and the relay's whole cycle stops at the
  first blocked stream. A dead group can stall the crawl loop.
- ADR-023: a *rebuildable projection* reads the outbox with its own
  checkpoint instead of joining a group (P7 increment 2).
- The registry's M2/M3 rows are **authoritative**, not a projection. Which
  mechanism it uses is therefore an open design decision (design §4).

### 2.6 P7

P8 needs nothing from P7 (P7 exit.md §8). `media.observed` lists crawl
intelligence as a consumer, but P7 implements no consumer of it. P8 must
not assume source advice, WHAT or WHEN.

## 3. V1 and fingerprinter (reference only, not modified)

| Source | Finding | P8 use |
|---|---|---|
| V1 `storage/media_evidence_store.py` | asset identity = sha256 of the cleaned media URL; idempotent asset creation, unconditional observation append, one fingerprint job per asset for its lifetime; manifest variants are bounded descriptive metadata, never fingerprinted separately | level 1 is the same idea (`MediaId`). V1's "one job per URL" is the D11 defect: the same bytes on many URLs were encoded many times. P8 keys encode work by `ContentId` instead |
| V1 `utils/url_utils.clean_media_url` | rejects traps and blacklisted URLs; does **not** strip tokens or signatures | no evidence for query stripping in V1 either |
| fingerprinter `acquisition/ssrf_guard.py` (102 lines) + `tests/test_acquisition_ssrf.py` (280 lines) | resolves the host and rejects loopback, private, link-local, unspecified, multicast, reserved and 100.64/10 addresses; documents the DNS-rebinding (TOCTOU) gap as out of scope | port the guard and its tests into the probe path (plan §P8). The rebinding gap stays recorded (P14) |

## 4. Gaps P8 has to close

1. A consumer of `media.discovered` that records observations and emits
   `media.observed` (nothing does today).
2. A level-3 content key: a named scheme (`ContentKeyScheme`, e.g.
   `sampled-bytes/v1`) and a source of bytes to digest (§2.3).
3. Durable probe results with provenance (which request, final URL after
   redirects, when, by which worker).
4. An `encode.requested` emitter, through the outbox, at most one logical
   request per (content, spec) across hosts.
5. A consumer of `representation.ready` / `encode.failed` that keeps the
   P1 projection.
6. An SSRF guard on the probe path.
7. The synthetic dedup benchmark and the different-content / collision
   test sets of the exit gate.

## 5. Read-only facts from the M1 export

Computed with a throwaway script over `extracts.jsonl.gz` (not
committed; M1 data untouched):

| Fact | Value |
|---|---|
| pages with media references | 703 (127 on seed domains, = P7 G8) |
| references / distinct locators | 3,304 / 1,578 |
| kinds | video_file 2,462; audio_file 795; unknown 30; hls_manifest 17 |
| methods | script_literal 1,662; link 1,402; source_element 175; video_element 37; player_config 28 |
| references with a query string | **3** |
| locators on > 1 page / on pages of > 1 domain | 59 / 3 |
| most pages for one locator | 227 |
| top locator hosts | abs.twimg.com 1,175; pmc.ncbi.nlm.nih.gov 546; pbs.twimg.com 470; sencloud.online 411; mp3teluguwap.net 349 |

What follows:

- Most M1 media is **not** relevant piracy media (site-chrome promo
  clips, NCBI article media). M1 is no population for P8's gates; the
  plan's gates are synthetic anyway.
- Signed or expiring CDN URLs are **not observed** (3 references with any
  query). Whether token churn inflates level 1 cannot be measured on this
  data. The design must not rely on query stripping it cannot validate.
- Repeated locators exist (one on 227 pages). M3's month × 16-shard
  partitioning bounds that; the registry must probe per media, not per
  sighting.

## 6. Risks and unknowns

| # | Risk / unknown | Where handled |
|---|---|---|
| R-1 | A registry consumer group could stall the crawl loop under ADR-022 | design §4, decision P8-D3 |
| R-2 | A sampled level-3 scheme collides for different content. Level 4 **cannot** split such a collision under P1 (one representation per `ContentId`) | design §3: `full-bytes/v1`, decision P8-D5 and plan corrections PC-1/PC-2 |
| R-3 | `media.observed` is keyed by (page observation, media): a second event after a probe is a logical duplicate and would be dropped | design §5.4, decision P8-D5 |
| R-4 | `encode.requested` is keyed by (content, spec): a re-request with another source after a failure collides with that key | design §6.2, decision P8-D7 (P9 coordination, no contract change in P8) |
| R-5 | Token-bearing CDN URLs multiply level-1 identities | level 3 dedups encode work; level-1 growth is measured, not prevented (P8-D2) |
| R-6 | Probing contacts third-party media hosts outside P3's per-domain politeness | design §5.2 |
| R-7 | HLS/DASH need a manifest + segment probe; their level-3 scheme is undecided | design §5.3, decision P8-D6 |
| R-8 | No SSRF guard exists on any crawler2 fetch path | probe path in P8; page fetching recorded for P14 (not P8 scope) |
