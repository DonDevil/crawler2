# P1 Audit — contracts and identity inherited from V1

Scope: only what informs V2 identity and contract semantics. V1 is a
reference, not a compatibility target. Nothing below is ported as-is.

## 1. V1 crawler

| V1 structure | Fields | Observation | V2 consequence |
|---|---|---|---|
| `core/frontier.py::FrontierClaim` | `url`, `token`, `attempt`, `domain`, `priority`, `lease_expires_at`, `source_query` | Raw URL is the identity. `source_query` ties a crawl claim to the search that produced it (target-centric). Lease fields are execution state. | `UrlId` from a canonical URL. Target relevance is only a hint on `crawl.requested` (`relevant_targets`) and never page state. Claims, leases and tokens are frontier internals (P3), not P1 contracts. |
| `url_frontier` / URL DB *visited* set | cleaned URL → visited | Terminal visited semantics block rediscovery for later targets (the defect V2 exists to fix). | Pages are observed repeatedly. Every fetch is a `FetchAttempt`, every response a `PageObservation`, content states are `PageVersionId`s. Nothing is terminal. |
| `storage/sqlite_media_evidence_store.py` `media_assets` | autoincrement `id`, `url UNIQUE`, `media_type`, `source_domain`, `mime_type`, `content_id`, first/last seen, `last_source_page`, `last_referrer_url`, `last_discovered_by`, `last_discovery_method`, `observation_count` | Host-local integer identity. "last_*" columns overwrite provenance, so only the latest observation survives on the entity. | `MediaId` is derived from the locator and host-independent. Each sighting is an append-only `MediaObservation`. Discovery method and referrer are per observation. |
| `media_observations`, `manifest_variants` (FK `asset_id`) | per-asset sightings, HLS variants | Useful: observation history, variant awareness. | Kept as concepts: `MediaObservation`; variants become `MediaKind` + future probe hints (additive). |
| `fingerprint_jobs` / `fingerprint_results` in the same SQLite DB | `asset_id`, status, claim token, retry count, priority | The crawler stores fingerprinter job state (cross-service coupling). | The crawler keeps only its own projection of representation status, built from events (ADR-011). |

## 2. V1 fingerprinter (`../fingerprinter`)

| Structure | Fields | Producer → consumer | Observation |
|---|---|---|---|
| `work_queue/jobs.py::Job` (schema 1) | `job_id`, `media_evidence_id`, `media_url`, `media_type`, `source_domain`, `target_id`, `target_version`, `techniques`, `max_attempts`, `schema_version` | crawler bridge → fingerprint worker | The unit of work is (media, target): media re-embedded per target. A flat string map per stream entry. `schema_version` added late, exact-match only. |
| `integration/candidate.py::FingerprintCandidate` | the `Job` fields plus `priority` (`high`/`normal`/`low`) | crawler → submission | **D12:** priority selects the stream. `normal` maps to the stream named `default`, the configuration workaround recorded in ADR-007. |
| `work_queue/results.py::Result` / `ResultRecord` | `decision` (match / no_match / processing_failure), `algorithm`, `confidence`, `summary`, `evidence`, timings; record adds `job_id`, `attempt`, `worker_id`, `result_version` | worker → result stream/hash → crawler result consumer | Good: an explicit three-way outcome ("failure is not a non-match"), a versioned result, dedupe by (`job_id`, `attempt`). |

### Semantics worth keeping

- Failure ≠ no match → separate `encode.failed` event; non-matches are not
  emitted at all (ANN reduction makes per-pair negatives meaningless).
- Deduplication by a stable key rather than delivery → the catalog's
  idempotency keys.
- An opaque crawler reference in fingerprinter messages
  (`media_evidence_id`) → replaced by *shared* identities (`ContentId`,
  `MediaId`) that both sides can derive or validate.
- An explicit schema version with a defined absent-field rule → the
  envelope's `schema_version` with MAJOR.MINOR semantics.

### Semantics deliberately dropped

- (media, target) jobs, `techniques` chosen by the crawler, `max_attempts`
  in the message (retry policy belongs to the consumer), stream-per-priority
  banding, and flat string-map encoding.

## 3. P0 state relevant to P1

- `antipiracy-contracts` 0.0.1 was a skeleton (version string only, no
  dependencies). ADR-007 required stable semantics and additive schemas.
- `crawler2.core.identity.WorkerIdentity` string
  `{host_id}:{role}:{pid}:{uuid}` → carried as `Producer.instance`. The
  contract package does not import crawler2, so the field is an opaque,
  pattern-validated string.
- Host-side Python cannot reach Scylla through the published port (P0
  audit). P1 tests are pure and need no infrastructure; the in-container
  run validates the same suite inside Docker.
