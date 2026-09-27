# P1 Design — core contracts, IDs and data model

P1 defines the language that every later V2 component uses to
communicate. It does **not** implement storage, transport or any
pipeline. Decisions are recorded in ADR-008 (IDs), ADR-009 (envelope and
evolution), ADR-010 (package scope) and ADR-011 (fingerprinter boundary).
This document explains how the pieces fit. The per-event reference is
[events.md](events.md); the storage input for P2 is
[access-patterns.md](access-patterns.md).

## 1. Principles applied

| Principle | How P1 applies it |
|---|---|
| Persistent, target-independent web memory | Web and media models contain no target fields; target relevance appears only as a hint on `crawl.requested` and in match results. |
| Observations separate from entities | Distinct model kinds (§3); observations are append-only, with allocated IDs. |
| Media-centric fingerprinting | Work keyed by `ContentId`; representations keyed by (content, spec). |
| Multi-host convergence | Derived IDs: every host computes the same identity for the same input. |
| Explicit contracts, no shared internals | One contract package, pydantic-only, import-restricted (ADR-010). |
| Room for V3 | Opaque representation specs, versioned derivation tags, additive evolution, an extension-only `metadata` field. |

## 2. Package layout (`contracts/src/antipiracy_contracts/`)

```
base.py            ContractModel (frozen, strict, extra=ignore), ContractKind,
                   UtcTimestamp, Priority, ShortText
digests.py         ContentDigest "sha256:<hex>"
urls.py            canonicalize_url (v1), CanonicalUrl, InvalidUrlError
ids.py             TypedId → DerivedId (UUIDv8) / AllocatedId (UUIDv7) + 13 ID types
ownership.py       ServiceName, Component (→ service), Producer
models/
  blobs.py         BlobRef
  web.py           UrlRef, Domain, FetchCapability, FetchOutcome, RedirectHop,
                   HttpValidators, FetchAttempt, PageObservation, LinkRelation, DiscoveredLink
  media.py         MediaKind, DiscoveryMethod, MediaReference, ContentKey,
                   TechnicalHints, Media, ProbeStatus, MediaObservation
  targets.py       TargetKind, TargetRef, Target
  representations.py RepresentationSpec, Representation
  matching.py      Technique, MatchVerdict, AlignedSegment, MatchResult
  evidence.py      ArtifactRole, EvidenceArtifact, EvidenceCandidate, EvidenceSeal
events/
  envelope.py      EventPayload, EnvelopeHeader, EventEnvelope[P], envelope_model
  web.py media.py fingerprinting.py targets.py evidence.py   (14 payloads)
  catalog.py       EventSpec, EventCatalog, CATALOG, contract errors
  codec.py         new_event, encode_event, decode_event, decode_event_as
schemas.py         deterministic JSON Schema export (+ CLI)
schema_json/       generated, committed schemas + catalog.json
compat/            golden fixtures (+ MANIFEST.json) and loaders
```

Dependency direction inside the package: `base/digests/urls` → `ids` →
`ownership` → `models` → `events.envelope` → payload modules →
`events.catalog` → `events.codec` → `schemas`/`compat`. There are no
cycles.

## 3. Model kinds

Every contract model declares `KIND` (tested). The kinds are not
interchangeable:

| Kind | Meaning | Identity | Mutability | Examples |
|---|---|---|---|---|
| ENTITY | persistent thing known to the system | stable ID | attributes may be re-published (versioned where it matters) | `Domain`, `Media`, `Target` |
| OBSERVATION | what was seen at one instant | allocated ID or natural pair | append-only | `PageObservation`, `MediaObservation` |
| ATTEMPT | one execution of work, success or failure | allocated ID | append-only | `FetchAttempt` |
| REPRESENTATION | derived computational artifact | derived from (subject, spec) | immutable, rebuildable | `Representation` |
| DECISION | judgement derived from representations | derived from inputs | immutable, reproducible | `MatchResult` |
| EVIDENCE | provenance supporting a case | allocated ID | write-once when sealed | `EvidenceCandidate`, `EvidenceSeal` |
| VALUE | embedded value object | none | immutable | `UrlRef`, `ContentKey`, `BlobRef`, `TargetRef` |
| EVENT | event payload | envelope `event_id` + idempotency key | immutable | `MediaDiscovered`, … |

All models are **frozen** and **strict** (no silent coercion); timestamps
must be timezone-aware and are normalized to UTC. Collections are tuples.
The only free-form map is the envelope's `metadata` (bounded, diagnostic
only).

## 4. Identity model

Summary (full table and rationale in ADR-008):

```
canonical URL ──► UrlId (url_) ──► PageVersionId (pgv_) = f(final UrlId, body digest)
     │                │
     └► DomainId (dom_)  (host)

media locator (canonical URL) ──► MediaId (med_)            level 1  "same address"
probe hints                   ──► TechnicalHints            level 2  hint only
sampled bytes + scheme        ──► ContentId (cnt_)          level 3  candidate; skips duplicate work
ContentId + spec              ──► RepresentationId (rep_)   level 4  (fingerprinter)
verified equivalence          ──► (fingerprinter, later)    level 5  same underlying work
rep_ × target rep_ × technique ─► MatchId (mat_)
allocated: tgt_ fat_ obs_ evd_ evt_ cor_
```

Why a page has no separate ID: page knowledge is keyed by the address
(`UrlId`). The content state is the `PageVersionId`, and each sighting is
an `ObservationId`. Why media identity is a locator hash and not the
bytes: bytes behind a URL change (new version = new `ContentId` for the
same `MediaId`), and the same bytes appear at many URLs (one `ContentId`,
many `MediaId`s). Keeping the two axes separate is what makes "encode once,
match many" and "never merge on a cheap hash" possible.

## 5. Event envelope and codec

```python
from antipiracy_contracts.events import new_event, encode_event, decode_event_as
from antipiracy_contracts.events.fingerprinting import EncodeRequested

envelope = new_event(
    payload, producer=producer, occurred_at=now, correlation_id=cid, causation_id=cause_event_id
)
wire: bytes = encode_event(envelope)  # JSON
request = decode_event_as(wire, EncodeRequested)  # consumer side
```

`decode_event` parses the header first, resolves (`event_type`, major) in
the catalog, checks producer ownership, then parses the full envelope with
the payload model. Errors: `UnknownEventTypeError`,
`UnsupportedSchemaVersionError`, `OwnershipViolationError`,
`UnexpectedEventTypeError` (all `ContractError` → `ValueError`) and
pydantic `ValidationError` for malformed content.

## 6. Evolution rules (ADR-009, condensed)

| Change | Class | Action |
|---|---|---|
| new optional field with default | minor | bump catalog `minor`, add a new fixture |
| new event type | minor | new payload + catalog entry + fixture |
| new enum member | consumer-first minor | upgrade consumers before the producer emits it |
| widen a constraint | minor | — |
| add a required field / make optional required | **major** | new payload class `SCHEMA_MAJOR=n+1`, dual-publish window |
| remove or rename a field, change type, unit or meaning | **major** | same |
| change canonicalization or an ID derivation | **major + migration** | new derivation tag, new ADR |
| deprecate a field | none immediately | document; keep producing until the next major |
| edit a released fixture | **forbidden** | add a new fixture |

Runtime: consumers ignore unknown fields and accept a higher minor.
Static: producers cannot pass unknown fields (mypy `init_forbid_extra`).

## 7. Event set

14 event types. Each has exactly one producer (full reference:
[events.md](events.md)):

```
crawl_intelligence ─ crawl.requested ─► frontier
crawler_worker ──── fetch.completed ──► crawl_intelligence
crawler_worker ──── page.observed ────► extraction, crawl_intelligence
extraction ──────── urls.discovered ──► crawl_intelligence, frontier
extraction ──────── media.discovered ─► media_registry
media_registry ──── media.observed ───► crawl_intelligence, evidence_collector
media_registry ──── encode.requested ─► encoder                       [crosses to fingerprinter]
encoder ─────────── representation.ready ► media_registry, matcher     [crosses back]
encoder ─────────── encode.failed ────► media_registry                 [crosses back]
target_manager ──── target.registered / target.retired ► crawl_intelligence, matcher [crosses]
matcher ─────────── match.found ──────► feedback, evidence_collector   [crosses]
evidence_collector ─ evidence.candidate_created ► evidence_finalizer
evidence_finalizer ─ evidence.finalized ► evidence_exporter
```

Changes from the plan's draft table: `page.fetched` → `page.observed` and
`fetch.outcome` → `fetch.completed` (consistent `<noun>.<past verb>`
naming). `urls.discovered` and `media.observed` are added because they
have clear producers and consumers. `page.changed` is **deferred to P5**:
its producer (page intelligence) and its change definition (normalized
content) are designed there, and adding an event type later is a minor
change. `evidence.candidate` → `evidence.candidate_created`.

Priority: one field (0–100, default 50, higher = sooner) on
`crawl.requested` and `encode.requested`. It orders work inside a consumer
and is never routed to per-priority streams (D12).

## 8. Fingerprinter boundary

See ADR-011. In short: crawler2 sends `encode.requested` keyed by
`ContentId` with a fetchable `Media` source and provenance. The
fingerprinter answers with `representation.ready`/`encode.failed`,
publishes targets, and reports `match.found` keyed by `ContentId` + target
version. Representation specs are opaque. Each side persists only its own
keyspace and keeps projections of the other side's facts, built from
events.

## 9. What P1 leaves to later phases

| Topic | Phase | Contract hook already present |
|---|---|---|
| Outbox, stream names, consumer groups, dedupe store | P2 | idempotency keys, JSON wire form, transport-free envelope |
| Scylla tables, TTLs, consistency levels | P2 | [access-patterns.md](access-patterns.md) |
| Object key layout | P2 | `BlobRef` (opaque URI + digest) |
| Leases, claims, retry scheduling | P3 | `crawl.requested` (priority, not_before) |
| Fetch escalation | P4/P7 | `FetchCapability`, `FetchOutcome`, `fetch.completed` |
| `page.changed`, normalized content digests | P5 | additive event/field |
| Probe schemes (`sampled-bytes/v1`), HLS variant hints | P8 | `ContentKeyScheme`, `TechnicalHints` |
| Spec names, sampling, GPU batching | P9 | opaque `RepresentationSpec` |
| Level-5 equivalence relation, match retraction | P10 | additive event types |
| Legal content of evidence packages | P12 (ADR-005 open) | `EvidenceCandidate`, `EvidenceSeal` |
