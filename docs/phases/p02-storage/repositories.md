# P2 Repositories

Interfaces: `crawler2/storage/repositories.py` (typing `Protocol`s over P1
contracts and small frozen read-model dataclasses — no driver types).
Implementations: `crawler2/storage/scylla/` (`ScyllaStorage.open(...)`
bundles them over one session and refuses an outdated schema).

| Interface | Patterns | Writes | Reads |
|---|---|---|---|
| `FetchAttemptRepository` | W1–W3 | `record(attempt, event=FetchCompleted?)` | `get`, `recent_for_url`, `for_domain_day` |
| `PageObservationRepository` | W4–W8, W14 (+ maintains W11–W13) | `record(observation, event=PageObserved?)`; `rebuild_url` (Scylla impl.) | `get` (LOCAL_QUORUM), `latest`, `history`, `versions`, `recent_for_domain_day` |
| `LinkRepository` | W9, W10 | `record(urls_discovered, observed_at=, event=UrlsDiscovered?)` | `links_of`, `inlinks` (streams all shards) |
| `UrlRepository` | W11–W13 | `record_discovered(urls, seen_at=)` | `get`, `urls_of_domain` (streams), `domain` |
| `MediaRepository` | M1–M6 | `record_observation(obs, event=MediaObserved?)`; `rebuild_media` | `get`, `observation`, `observations_on`, `sightings`, `on_page_version`, `by_content`, `content_versions` |
| `ProjectionRepository` | P1–P5 | `mark_encode_requested`, `apply_representation_ready`, `apply_encode_failed`, `apply_target_registered`, `apply_target_retired`, `record_match(match, media_ids=, source_domains=)` | `representation_status`, `target`, `targets`, `matches_for_content/target/domain` |
| `EvidenceRepository` | E1–E4 | `open_candidate` (E1 LWT), `create_candidate` (write-once), `seal` (CAS) | `get` (LOCAL_SERIAL), `for_target`, `provenance` (E3) |

Infrastructure (not domain repositories): `ScyllaOutbox` (outbox rows,
relay store), `ScyllaProcessedEventStore` (consumer markers),
`S3ObjectStore` (`ObjectStore` interface).

## Rules every method follows

1. **Idempotent**: keyed by contract IDs; a replay writes identical cells.
   Tested per repository (`tests/integration/storage/`) and across hosts
   (`tests/integration/multihost.py`).
2. **`event=`** (optional) appends the envelope to the outbox with the
   authoritative rows. The payload must describe the same write, otherwise
   `ValueError` and nothing is written.
3. **Callers pass time ranges** for bucketed histories (`since`/`until`,
   e.g. from `url_state.first_seen`); repositories never scan.
4. **No joins**: `record_match` takes `media_ids`/`source_domains` already
   resolved by the caller (M5, then page → domain); `provenance` is the one
   documented composition (E2 + W7 point reads, P1 E3).
5. **Errors**: `StorageUnavailableError` (retryable backend failure),
   `ConflictError` (write-once/CAS violated), `IntegrityError` (digest/size
   mismatch), `SchemaError`. Contract validation errors propagate as
   pydantic `ValidationError`.

## Not in P2

Frontier admission, scheduling, `next_due` (P3/P7); source-intelligence
columns of `domains` (P7); W15 (P5/P7); media probing and encode-request
policy (P8); evidence completeness/sealing policy (P12). They are added as
new columns/tables through migrations, not by changing these semantics.
