# What P1 built

An inventory of what exists after P1 and how to use it. Rationale:
[design.md](design.md) and ADR-008…011. Event reference:
[events.md](events.md). P2 input: [access-patterns.md](access-patterns.md).
Evidence that it works: [validation.md](validation.md).

## 1. `antipiracy-contracts` 1.0.0 (`contracts/`)

| Item | Details |
|---|---|
| `contracts/pyproject.toml` | version `1.0.0` (contract major 1); sole runtime dependency `pydantic>=2.13,<3` |
| `antipiracy_contracts/base.py` | `ContractModel` (frozen, `strict`, `extra="ignore"`), `ContractKind`, `UtcTimestamp` (aware → UTC), `Priority` (0–100), `DEFAULT_PRIORITY=50`, `ShortText` |
| `digests.py` | `ContentDigest` (`sha256:<64 hex>`), `.of_bytes()`, `.hex` |
| `urls.py` | `canonicalize_url()` (canonical form v1), `CanonicalUrl` (rejects non-canonical input; `.host`), `InvalidUrlError` |
| `ids.py` | `TypedId`, `DerivedId` (UUIDv8), `AllocatedId` (UUIDv7, `.new()`); `DomainId.of_url`, `UrlId.of`, `PageVersionId.of`, `MediaId.of_locator`, `ContentId.of`, `RepresentationId.for_content/for_target`, `MatchId.of`; `TargetId`, `FetchAttemptId`, `ObservationId`, `EvidenceId`, `EventId`, `CorrelationId`; `.uuid`, `.from_uuid()`; `InvalidIdError` |
| `ownership.py` | `ServiceName`, `Component` (`.service`), `Producer` (validates component ∈ service) |
| `models/` | web, media, targets, representations, matching, evidence, blobs (see design §2) |
| `events/` | envelope, 14 payloads, `CATALOG`, codec, contract errors |
| `schemas.py` | `build_schemas()`, `write_schemas()`, `stale_schemas()`, CLI `python -m antipiracy_contracts.schemas [--check] [DIR]` |
| `schema_json/` | generated: `catalog.json` + `events/<type>.v1.json` (14) |
| `compat/` | `event_fixtures()`, `model_fixture()`, `fixture_digests()`, `released_digests()`; fixtures: 14 base + 4 forward event fixtures, 2 model fixtures (`target`, `domain`), `MANIFEST.json` |

### Usage

```python
from datetime import UTC, datetime
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import new_event, encode_event, decode_event_as
from antipiracy_contracts.events.fingerprinting import EncodeRequested
from antipiracy_contracts.models.media import ContentKey, Media, MediaKind
from antipiracy_contracts.models.web import UrlRef
from antipiracy_contracts.ownership import Component, Producer, ServiceName

producer = Producer(
    service=ServiceName.CRAWLER2, component=Component.MEDIA_REGISTRY, instance=str(worker_identity)
)  # crawler2 WorkerIdentity
media = Media.of(UrlRef.of("https://cdn.example/v.mp4"), MediaKind.VIDEO_FILE)
content = ContentKey.of("sampled-bytes/v1", ContentDigest.of_bytes(sampled))
event = new_event(
    EncodeRequested(content=content, source=media, priority=70),
    producer=producer,
    occurred_at=datetime.now(UTC),
)
wire = encode_event(event)  # bytes (JSON)

# fingerprinter side
request = decode_event_as(wire, EncodeRequested)
request.payload.content.content_id, request.payload.source.locator.url
```

Compatibility suite in another repository (fingerprinter CI):

```python
from antipiracy_contracts.compat import event_fixtures
from antipiracy_contracts.events import decode_event

for fixture in event_fixtures():
    decode_event(fixture.raw)  # every fixture of the pinned version must decode
```

## 2. crawler2 repository changes

| File | Change |
|---|---|
| `pyproject.toml` | `[tool.pydantic-mypy] init_forbid_extra = true, init_typed = true`. Producers cannot pass unknown fields (static check), while runtime consumers ignore them. |
| `Makefile` | `make schemas` regenerates the committed JSON Schemas |
| `uv.lock`, `requirements*.txt` | `antipiracy-contracts` 0.0.1 → 1.0.0 and its pydantic dependency. No versions changed. |
| `tests/contract/` | new contract suite (see validation) |
| `docs/adr/ADR-008…011` | identity, envelope/evolution, package scope, fingerprinter boundary |
| `docs/adr/ADR-007` | status line: amended by ADR-010 |
| `docs/v2-phase-plan.md` | P10's index ADR renumbered to "next free ADR number" (ADR-008 is now taken) |
| `docs/phases/p01-contracts/` | this phase's documents |
| `README.md`, `contracts/README.md` | current phase, layout, contract usage |

No crawler2 runtime code changed. The Docker image picks the new package
up through the existing `COPY contracts/` step; no Docker or Compose files
changed.

## 3. Tests (`tests/contract/`)

| File | Covers |
|---|---|
| `test_ids.py` | unique prefixes; **pinned derived values**; no cross-type collisions; injective derivation encoding; determinism (hypothesis); v7/v8 version and variant bits; allocation uniqueness; malformed/foreign IDs rejected; JSON round-trip keeps the type |
| `test_urls.py` | canonicalization table (case, ports, dots, escapes, IDNA, IPv6, fragments); policy non-normalization (query order, tracking params); rejections; `CanonicalUrl` strictness; `.host`; idempotence (hypothesis) |
| `test_models.py` | every model declares `KIND` and is frozen; kinds of key models; immutability; derived-ID consistency validators (URL, domain, page version, content key, match); aware/UTC timestamps; no coercion; fetch-attempt invariants; content vs media identity; required/optional behaviour; model fixtures |
| `test_events.py` | envelope filling; ownership enforced when producing and consuming; producer/service consistency; payload/type mismatch; unknown type/major; schema_version format; newer minor + unknown fields accepted; missing required rejected; optional defaults; `decode_event_as`; aware `occurred_at`; metadata bounds; priority bounds |
| `test_catalog.py` | ownership table as agreed; every payload class cataloged; the exact set of cross-service events; specs documented; invalid catalogs rejected |
| `test_schemas.py` | committed schemas current; deterministic generation; stale detection; no schema forbids unknown fields; catalog.json complete |
| `test_compat.py` | released fixtures unchanged (manifest); every event has a fixture; decode + round-trip + byte-level equality with current producers; forward fixtures ≡ base; fingerprinter-side and crawler-side consumption; causal join encode → representation → match → target; failure answers its request |
| `test_boundaries.py` | contract package imports only stdlib + pydantic |

## 4. Not built in P1 (by design)

Scylla keyspaces/tables/repositories, outbox and Redis Streams transport,
consumer dedupe stores, frontier/leases/retries, workers and fetchers,
extraction, media probing, encoders, vector index, matcher, evidence
pipeline, analytics, GPU support. P1 only defines the contracts these use.
