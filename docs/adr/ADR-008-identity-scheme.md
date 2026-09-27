# ADR-008 — Identity scheme: typed, prefixed UUIDs; derived vs allocated

Status: Accepted (P1, 2026-09-28)

## Context

V1 keyed page state by raw URL and marked it terminally *visited* per
crawl, so knowledge was target-centric and could not be reused (plan A.2).
V1 media assets were SQLite autoincrement integers (`media_assets.id`)
with a URL unique constraint: local, host-specific and meaningless to any
other process. V2 runs on several hosts from day one (ADR-006), so
independent writers must agree on identity without coordination, and the
crawler and the fingerprinter must agree on media identity without
sharing storage (ADR-002, ADR-011).

## Decision

1. **Wire form** `<prefix>_<32 lowercase hex>`; the hex is an RFC 9562
   UUID, so storage can use Scylla's 16-byte `uuid` type. The three-letter
   prefix makes IDs self-describing and lets validation reject an ID of
   the wrong type. Implemented in `antipiracy_contracts.ids`.
2. **Derived IDs (UUIDv8)** where convergence matters: SHA-256 over a
   length-prefixed input `("antipiracy/<entity>/v1", part, …)`, truncated
   to 122 bits with version/variant bits set. The same input gives the same
   ID on every host, and writes of the same entity are idempotent upserts.
   The versioned domain-separation tag keeps different entity types (and
   future derivation schemes) from ever colliding.
3. **Allocated IDs (UUIDv7)** for things that are unique occurrences:
   targets, fetch attempts, observations, evidence, events, correlations.
   The embedded time only gives rough ordering and storage locality. It is
   **not data**: consumers read time from timestamp fields, never from IDs.
4. **Entities and their IDs**

   | ID | Prefix | Family | Identifies | Derivation input |
   |---|---|---|---|---|
   | `DomainId` | `dom` | derived | a host name (not the registrable domain) | canonical host |
   | `UrlId` | `url` | derived | a web address; page knowledge is keyed by it | canonical URL v1 |
   | `PageVersionId` | `pgv` | derived | one content state of a page | (final `UrlId`, body digest) |
   | `MediaId` | `med` | derived | a media resource by locator (level 1) | canonical locator URL |
   | `ContentId` | `cnt` | derived | candidate content identity (level 3) | (sampling scheme, digest) |
   | `RepresentationId` | `rep` | derived | one representation of one subject under one spec | (subject, spec name, version, config digest) |
   | `MatchId` | `mat` | derived | one comparison outcome | (media rep, target rep, technique, version) |
   | `TargetId` | `tgt` | allocated | a protected work (stable across versions) | — |
   | `FetchAttemptId` | `fat` | allocated | one fetch execution | — |
   | `ObservationId` | `obs` | allocated | one page observation | — |
   | `EvidenceId` | `evd` | allocated | one evidence item (candidate → sealed) | — |
   | `EventId` | `evt` | allocated | one event (stable across redelivery) | — |
   | `CorrelationId` | `cor` | allocated | one causal chain | — |

5. **Deliberately no ID** for: *page* (it is the resource at a `UrlId`; a
   second derived name for the same input would drift), *link* (an edge
   `(PageVersionId, UrlId)`), *media observation* (the pair
   `(ObservationId, MediaId)`), *media version* (the pair
   `(MediaId, ContentId)`), *target version* (`(TargetId, version:int)`).
6. **Canonical URL v1** (`antipiracy_contracts.urls`) is purely syntactic
   RFC 3986 normalization: lowercase scheme/host, IDNA host, trailing-dot
   and default-port removal, dot-segment removal, escape normalization,
   fragment removal, `http`/`https` only, credentials rejected. It does
   **not** remove tracking parameters, sort queries, strip `www.` or fold
   mirrors. Those are policy judgements; getting one wrong in identity
   would merge distinct resources irreversibly. Later phases record such
   equivalences as relations.
7. **Media identity hierarchy** (plan P8). A cheap level never asserts
   what only a stronger level can. A `ContentId` equality means only "the
   sampled bytes under scheme S were identical". It justifies skipping
   duplicate work (encode once), never merging records irreversibly or
   declaring two media the same work. Different encodings or packagings of
   the same work have different `ContentId`s and are linked only by the
   fingerprinter's verified equivalence (level 5).
8. Models that carry a derived ID **together with its inputs** validate the
   pair (`UrlRef`, `Domain`, `Media`, `ContentKey`, `PageObservation`,
   `Representation`, `MatchResult`). A producer cannot send an inconsistent
   pair.

## Consequences

- Derivations are frozen for contract major 1; pinned-value tests
  (`tests/contract/test_ids.py`) fail on any accidental change. Changing
  canonicalization or a derivation is a data migration and needs a new ADR
  plus a new derivation tag (`…/v2`).
- IDs are safe to use across services: they carry no host-local state,
  and derived IDs can be recomputed by anyone who holds the inputs.
- Registrable-domain grouping (public suffix list) is an attribute derived
  by intelligence, not identity, because the list changes over time.
- Storage (P2) may store IDs as `uuid` and add the prefix at the boundary,
  or as `text`; both are lossless.
