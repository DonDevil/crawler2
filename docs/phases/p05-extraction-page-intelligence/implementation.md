# P5 Implementation — what was built

Design: [design.md](design.md) (approved 2026-09-29, deviations §21).
Current-state summary: [architecture/extraction.md](../../architecture/extraction.md).
Validation and exit gates: [validation.md](validation.md). Benchmarks:
[benchmarks.md](benchmarks.md). Decisions and limitations:
[decisions.md](decisions.md).

## 1. Modules

| Module | Role | Pure? |
|---|---|---|
| `crawler2/extraction/urls.py` | reference cleaning, scheme test, RFC 3986 resolution, P1 canonical `UrlRef`, media-kind classification, `LinkPolicy` hook | yes |
| `crawler2/extraction/parse.py` | charset detection, HTML sniffing, **the only parser construction** (`parse_page`), `<base href>` | yes |
| `crawler2/extraction/extract.py` | one iterative tree walk feeding every fact family; literals; hashes; `PageExtract` | yes |
| `crawler2/extraction/scripts.py` | script/text URL literal syntax (design §7) | yes |
| `crawler2/extraction/hashing.py` | canonical forms and tagged SHA-256 digests | yes |
| `crawler2/extraction/model.py` | `PageExtract`, `PageMetadata`, `ExtractStats`, `NormalizationRules`, `Limits`, `EXTRACTOR_VERSION` | yes |
| `crawler2/extraction/archival.py` | `ArchivalProfile`, deterministic `decide()` | yes |
| `crawler2/extraction/service.py` | `PageIntelligenceService`: eligibility, reuse, P2 write order, events, metrics | I/O via P2 only |
| `crawler2/extraction/cli.py` | `crawler2-extract` consumer loop (stale-claim, read, ack) | I/O |
| `crawler2/storage/scylla/cql/V002__page_intelligence.cql` | `page_extracts`, `page_revisions_by_url`, `snapshot_retention` | — |
| `crawler2/storage/scylla/pages.py` | `ScyllaPageIntelligenceRepository` | — |
| `crawler2/storage/repositories.py` | `PageIntelligenceRepository` protocol + read/write records (additive) | — |
| `contracts/…` (1.1) | `PageRevisionId`, `PageHashes`, `PageChanged` (`page.changed`), fixture, schema (ADR-020) | — |
| `crawler2/core/configuration/settings.py` | `WorkerRole.EXTRACTION`, `ExtractionSettings` (additive) | — |

P3 and P4 code is unchanged. V1 is untouched.

## 2. Pipeline

`page.observed` → `IdempotentConsumer` (key `observation_id`) →
`PageIntelligenceService.process`:

1. Skip unless status 2xx, a snapshot is present and the content type is
   HTML (or, when absent, the body starts with an HTML tag).
2. If `page_extracts` already holds this raw `page_version_id` under the
   current `EXTRACTOR_VERSION` and normalization scheme, reuse it (no
   object-store read, no parse).
3. Otherwise `ObjectStore.get` (digest-verified) → `extract()` (one
   parse) → links + `urls.discovered` (pattern B) → `url_state` →
   `page_extracts` row (commit marker).
4. Point-read the revision; write the sighting (first_* earliest-wins,
   last_* latest-wins) in one logged batch with `page.changed` (only if the
   revision was absent) and `media.discovered` (if any media).
5. Record the archival decision.

A per-page extractor exception is logged, counted
(`p5_pages_total{result="failed"}`) and the event is marked processed;
storage errors propagate and leave the stream entry pending for
redelivery (possibly on another host after `claim_idle_ms`).

## 3. Single parse

`extract()` calls `parse_page()` once; `parse_page()` is the only caller
of `LexborHTMLParser`. The walk is iterative (no recursion limit), visits
every node once, never mutates the tree, and dispatches elements to
handlers; hidden and boilerplate subtrees are pre-selected with two C-level
CSS queries on the same tree and compared by `mem_id`. The title uses one
`css_first("head title")`. Tests: `test_one_parse_and_one_tree_feed_every_extractor`,
`test_no_other_module_constructs_a_parser`.

## 4. URL semantics

Only P1 canonical form v1: fragment removed, scheme/host lowercased,
IDNA, default port dropped, dot segments removed, escapes normalized.
Query parameters are neither removed nor reordered, `www.` is kept, no
tracking-parameter stripping. Non-web schemes (`mailto:`, `javascript:`,
`data:`, `tel:`, `ftp:`, `blob:` …), fragment-only references, empty and
unparseable references are dropped and counted by reason. Media URLs are
reported as media, never as page links. Script/text literals pointing at
static assets (`.js`, `.css`, images, fonts, `.json`, `.map`) are dropped
(`asset`); markup links are never filtered by extension.

## 5. Hash tags

| Hash | Tag |
|---|---|
| visible text | `antipiracy/page-hash/visible-text/v1` |
| link set | `antipiracy/page-hash/link-set/v1` |
| media set | `antipiracy/page-hash/media-set/v1` |
| structural | `antipiracy/page-hash/structural/v1` |
| normalized | `antipiracy/page-hash/<scheme>` (baseline `html-normalized/v1`) |
| raw | the observation's `body_digest` (plain SHA-256 of the bytes) |

Canonical forms: design §9. Stability is pinned by golden results.

## 6. Metrics (prefix `p5_`, no URL/domain labels)

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `p5_pages_total` | counter | `result` = extracted, reused, skipped_status, skipped_type, no_snapshot, failed | observations handled |
| `p5_extract_seconds` | histogram | — | CPU time of parse + extraction per page |
| `p5_links_per_page`, `p5_media_per_page` | histogram | — | fact counts |
| `p5_metadata_fields_total` | counter | `field` (8 fixed names) | metadata presence |
| `p5_dropped_refs_total` | counter | `reason` = empty, self, scheme, invalid, policy, asset, cap | discarded references |
| `p5_truncated_total` | counter | `what` = body, elements, scripts, links, media | limits hit |
| `p5_revisions_total` | counter | `kind` = new, resighted | revision sightings |
| `p5_archival_decisions_total` | counter | `decision`, `reason` | snapshot decisions |
| `consumer_events_total` (P2) | counter | `consumer`, `outcome` | applied vs duplicate deliveries |

## 7. Configuration

`extraction.consumer_group` (`extraction`), `batch_size` (50),
`block_ms` (2000), `claim_idle_ms` (60 000), `archival_sample_rate`
(0.05). Limits (`Limits`): 8 MiB parsed per body, 200 000 elements,
depth 256 in the shape, 1 MiB per inline script and 4 MiB per page,
10 000 links and 1 000 media references (P1 batch maxima).

## 8. Compatibility

| Layer | Change | Compatibility |
|---|---|---|
| P1 | contract 1.1: `PageRevisionId`, `PageHashes`, `page.changed` | additive; released fixtures unchanged (MANIFEST test); `PageVersionId` meaning unchanged |
| P2 | V002 migration, `PageIntelligenceRepository`, `ScyllaStorage.page_intel` | additive; existing tables, methods and semantics unchanged |
| P3 | none | frontier keeps admitting `urls.discovered` |
| P4 | none | `page.observed` consumed as emitted; P4 still stores every body |

## 9. Running

```bash
make check                                             # lint, mypy, unit + contract tests
crawler2-extract                                       # consumer (needs the stack)
benchmarks/p5-extraction/run.sh capture benchmarks/p4-fetch/w691.txt var/p5-corpus/capture-1
benchmarks/p5-extraction/run.sh parse var/p5-corpus/capture-1 2
env/bin/python benchmarks/p5-extraction/pairs.py review var/p5-corpus/capture-{1,2} --out var/p5-corpus/review
```

Integration tests from the host (container IPs, P4 audit §6):
`docs/development.md` → "P5 extraction".
