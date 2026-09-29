# Extraction & page intelligence (current state)

Extraction turns a recorded response body into **page facts**: links,
media references, metadata, a hash set and the page's revision. Full
design, measurements and exit gates:
[P5 phase documents](../phases/p05-extraction-page-intelligence/).
Decision: [ADR-020](../adr/ADR-020-page-revisions.md) (page revisions,
`page.changed`, contract 1.1).

```
 P4 recorder ── page.observed (outbox → relay → Redis stream) ──► crawler2-extract
                                                                   │ IdempotentConsumer (observation_id)
                                                                   ▼
                         eligible? 2xx · HTML · snapshot     ─ no ─► counted, done
                                                                   │
                  extract for this raw page_version stored? ─ yes ─► reuse (no GET, no parse)
                                                                   │ no
                                         ObjectStore.get(snapshot) → parse_page (ONE selectolax parse)
                                                                   ▼
            one tree walk → links · media · metadata · script/text literals · visible/normalized text · shape
                                                                   ▼
                                       PageExtract (pure) + hashes + revision_id
                                                                   ▼
   LinkRepository (+urls.discovered) → UrlRepository → page_extracts (commit marker)
   → page_revisions_by_url sighting (+page.changed if new, +media.discovered) → snapshot_retention
```

## Identities

| Level | ID | Changes when |
|---|---|---|
| raw page version (P1) | `PageVersionId(final url, body digest)` | any byte changes |
| page revision (P5, ADR-020) | `PageRevisionId(final url, normalization scheme, normalized digest)` | the normalized content changes |

A revision row is created on the first sighting of a normalized state;
later sightings move only `last_seen` (latest-wins cells). A revert to an
earlier state re-sights the old revision.

## Hash set

`raw` (exact bytes), `normalized` (decides revisions: visible main-content
text, NFKC + casefold, plus the media set), `visible_text`, `link_set`,
`media_set`, `structural` (preorder `depth:tag`). Each is a tagged,
versioned SHA-256; definitions in the design §9.

## Guarantees

- **Single parse.** `crawler2/extraction/parse.py` is the only module that
  constructs a parser; a test proves one construction per page and one
  tree seen by every extractor.
- **Pure extraction.** No network, Redis, Scylla or clock in
  `extract()`. Page JavaScript is never executed and extracted URLs are
  never fetched.
- **Target independent.** Nothing is keyed by, or reads, a target.
- **Idempotent and multi-host safe.** Any number of consumers share one
  group; redelivery, replay and concurrent first sightings converge on
  the same rows; duplicate `page.changed` events carry one idempotency key.
- **Policy-free.** URL identity is P1 canonical v1. Link admission
  (`LinkPolicy`) and extra normalization rules (`NormalizationRules`)
  are hooks for P6, defaulting to allow-all and the baseline scheme.
- **Nothing is deleted.** P4 stores every body; P5 records an archival
  decision per (snapshot, observation). Garbage collection is deferred.

## Running it

```bash
crawler2-extract                 # long-running consumer of page.observed
crawler2-extract --exit-when-idle 3
```

Configuration: `extraction.*` (`consumer_group`, `batch_size`,
`block_ms`, `claim_idle_ms`, `archival_sample_rate`). Metrics are prefixed
`p5_` (implementation doc §6).
