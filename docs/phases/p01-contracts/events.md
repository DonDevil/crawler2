# P1 Event reference (contract major 1)

Machine-readable source: `antipiracy_contracts.events.catalog.CATALOG`,
exported to `contracts/src/antipiracy_contracts/schema_json/catalog.json`.
JSON Schemas: `schema_json/events/<event_type>.v1.json`. Golden fixtures:
`compat/fixtures/events/<event_type>.v1.json`.

**Common to every event** (ADR-009): envelope as specified in ADR-009;
exactly one producing component (enforced by `new_event`/`decode_event`);
**at-least-once delivery, duplicates allowed**, no ordering across events;
consumers deduplicate on the idempotency key and ignore unknown fields.
Evolution: additive minors, new major for breaking changes.
All current schemas are `1.0`. Contract package 1.1 added `page.changed` (P5).

"Required" lists the payload fields without defaults; everything else is
optional.

---

### `crawl.requested` — crawl_intelligence → frontier

- **Meaning:** a URL should be crawled now or after `not_before` (seed,
  recrawl, discovery for a target, feedback).
- **Required:** `url` (`UrlRef`), `reason` (`seed|discovered|recrawl|target_discovery|feedback`).
- **Optional:** `priority` (0–100, default 50), `capability` (starting hint),
  `not_before`, `relevant_targets` (hint only; never stored as page state).
- **Idempotency:** (`url.url_id`, `reason`, `not_before`). The frontier merges
  requests per URL and keeps the highest priority.
- **Ordering:** none.
- **Note:** seeds are issued by the intelligence component (its seed
  loader). With intelligence stopped, the frontier's default admission of
  `urls.discovered` keeps crawling alive (plan B.5 #1).

### `fetch.completed` — crawler_worker → crawl_intelligence

- **Meaning:** a fetch attempt ended, whether it succeeded or not. Input to
  fetch-profile and source learning.
- **Required:** `attempt` (`FetchAttempt`: id, requested URL, capability,
  worker, started/finished, outcome).
- **Invariants:** `http_status` present iff outcome ∈ {`response`,
  `blocked`}; `final` present iff outcome = `response`; finished ≥ started.
- **Idempotency:** `attempt.fetch_attempt_id`. **Ordering:** none.

### `page.observed` — crawler_worker → extraction, crawl_intelligence

- **Meaning:** a response was durably recorded as a page observation (any
  HTTP status).
- **Required:** `observation` (`PageObservation`: ids, requested/final URL,
  capability, observed_at, status, body digest/size, `page_version_id`).
- **Optional inside:** `content_type`, `validators` (ETag/Last-Modified),
  `snapshot` (`BlobRef`, digest must equal `body_digest`), `redirects`.
- **Idempotency:** `observation.observation_id`. **Ordering:** none. To
  find the latest, compare `observed_at`, never arrival order.

### `urls.discovered` — extraction → crawl_intelligence, frontier

- **Meaning:** the outgoing links of one page observation, as one batch
  (1–10 000).
- **Required:** `page_observation_id`, `page`, `page_version_id`, `links`
  (`DiscoveredLink`: target `UrlRef`, `relation`, optional `anchor_text`,
  `nofollow`).
- **Idempotency:** `page_observation_id`. **Ordering:** none.

### `page.changed` — extraction → crawl_intelligence *(contract 1.1, ADR-020)*

- **Meaning:** an observation showed a normalized content state (page
  revision) never seen before at its final URL: the P5 definition of a
  meaningful change.
- **Required:** `page_observation_id`, `page` (final `UrlRef`),
  `page_version_id` (raw), `revision_id` (`PageRevisionId`), `normalization`
  (scheme, e.g. `html-normalized/v1`), `hashes` (`PageHashes`: raw, normalized,
  visible_text, link_set, media_set, structural), `observed_at`.
- **Invariants:** `page_version_id` = f(page, hashes.raw); `revision_id` =
  f(page, normalization, hashes.normalized).
- **Idempotency:** `revision_id`. **Ordering:** none; re-sightings, reverts
  to a known revision, raw-only changes and non-HTML bodies emit nothing.

### `media.discovered` — extraction → media_registry

- **Meaning:** media references found on one page observation (1–1 000).
- **Required:** `page_observation_id`, `page`, `page_version_id`,
  `references` (`MediaReference`: locator, kind, method, optional
  declared type).
- **Idempotency:** `page_observation_id`. **Ordering:** none.

### `media.observed` — media_registry → crawl_intelligence, evidence_collector

- **Meaning:** a media reference was resolved to a `Media` entity and
  possibly probed.
- **Required:** `observation` (`MediaObservation`: media, page observation,
  page, page version, observed_at, probe_status).
- **Optional inside:** `content` (`ContentKey`, only when probe_status =
  `probed`), `hints` (`TechnicalHints`).
- **Idempotency:** (`observation.page_observation_id`, `observation.media.media_id`).
- **Ordering:** none.

### `encode.requested` — media_registry → encoder *(crosses to fingerprinter)*

- **Meaning:** a content identity lacks a required representation.
- **Required:** `content` (`ContentKey`), `source` (`Media` to fetch from).
- **Optional:** `referer` (page `UrlRef`), `required_spec` (absent = the
  fingerprinter's default), `priority` (default 50).
- **Idempotency:** (`content.content_id`, `required_spec`). The encoder must
  not encode the same content twice for one spec.
- **Ordering:** none. Priority orders work inside the encoder queue, not
  delivery.

### `representation.ready` — encoder → media_registry, matcher *(crosses back)*

- **Meaning:** a representation of a content identity exists (new, or
  already present when a duplicate request arrives).
- **Required:** `representation` (`Representation`: id derived from
  content + spec, content_id, spec, created_at; optional segment count and
  duration). No vectors.
- **Idempotency:** `representation.representation_id`. **Ordering:** none.

### `encode.failed` — encoder → media_registry *(crosses back)*

- **Meaning:** an encode request could not be satisfied. This is **not** a
  "no match".
- **Required:** `request_id` (the `encode.requested` event id), `content_id`,
  `failure` (`source_unreachable|source_forbidden|unsupported_format|too_large|decode_error|timeout|internal_error`),
  `retryable`, `attempt` (≥1).
- **Optional:** `spec`, `detail`.
- **Idempotency:** (`request_id`, `attempt`). **Ordering:** none; a later
  `representation.ready` for the same content and spec supersedes it.

### `target.registered` — target_manager → crawl_intelligence, matcher *(crosses)*

- **Meaning:** a target, or a new version of one, is active. Triggers
  discovery and backfill matching against existing representations.
- **Required:** `target` (`Target`: ref {target_id, version}, title, kind,
  registered_at; optional aliases, release_year).
- **Idempotency:** (`target.ref.target_id`, `target.ref.version`).
- **Ordering:** none. Consumers keep the highest version per target.

### `target.retired` — target_manager → crawl_intelligence, matcher *(crosses)*

- **Meaning:** the target is no longer searched for. Past matches and
  evidence stay valid.
- **Required:** `target_id`, `retired_at`. **Optional:** `reason`.
- **Idempotency:** `target_id`. **Ordering:** retirement is terminal and
  wins over any registration.

### `match.found` — matcher → feedback, evidence_collector *(crosses)*

- **Meaning:** content was verified against a target version.
- **Required:** `match` (`MatchResult`: match_id derived from both
  representations + technique, content_id, representation_id, target ref,
  target_representation_id, technique {name, version}, verdict
  (`confirmed|probable`), confidence 0–1, decided_at; optional aligned
  segments).
- **Idempotency:** `match.match_id`. **Ordering:** none.

### `evidence.candidate_created` — evidence_collector → evidence_finalizer

- **Meaning:** provenance for a match was collected and awaits
  completeness checks and sealing.
- **Required:** `candidate` (evidence_id, match_id, target ref, content_id,
  ≥1 media_ids, ≥1 page_observation_ids, collected_at; optional artifacts
  as `BlobRef`s with roles).
- **Idempotency:** `candidate.evidence_id`. **Ordering:** none.
- **Note:** legal content is open (ADR-005). Additions will be additive.

### `evidence.finalized` — evidence_finalizer → evidence_exporter

- **Meaning:** an evidence item is sealed (write-once manifest pinned by
  digest).
- **Required:** `seal` (evidence_id, manifest `BlobRef`, finalized_at).
- **Idempotency:** `seal.evidence_id`. **Ordering:** none.

---

## Ownership summary

| Component | Service | Produces | Consumes |
|---|---|---|---|
| crawl_intelligence | crawler2 | crawl.requested | fetch.completed, page.observed, page.changed, urls.discovered, media.observed, target.registered, target.retired |
| frontier | crawler2 | — | crawl.requested, urls.discovered |
| crawler_worker | crawler2 | fetch.completed, page.observed | — (claims work from the frontier, P3) |
| extraction | crawler2 | urls.discovered, media.discovered, page.changed | page.observed |
| media_registry | crawler2 | media.observed, encode.requested | media.discovered, representation.ready, encode.failed |
| feedback | crawler2 | — (P11 writes intelligence state) | match.found |
| evidence_collector | crawler2 | evidence.candidate_created | media.observed, match.found |
| evidence_finalizer | crawler2 | evidence.finalized | evidence.candidate_created |
| evidence_exporter | crawler2 | — | evidence.finalized |
| target_manager | fingerprinter | target.registered, target.retired | — |
| encoder | fingerprinter | representation.ready, encode.failed | encode.requested |
| matcher | fingerprinter | match.found | representation.ready, target.registered, target.retired |
