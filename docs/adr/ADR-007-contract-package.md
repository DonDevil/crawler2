# ADR-007 — Versioned cross-repository contract package

Status: Accepted (P0, 2026-09-28)

## Context

In V1 the crawler and the fingerprinter drifted apart; integration ended
up depending on a configuration workaround (priority banding pinned to
`-1/1000000` because the fingerprinter consumed only the `default`
stream — plan A.2 D12). The fingerprinter stays a separate service and
repository.

## Decision

- One package, **`antipiracy-contracts`**, is the only shared code between
  `crawler2` and `fingerprinter`. It contains JSON Schemas (source of
  truth) plus generated Python models, and nothing else — no clients, no
  business logic.
- It lives in `crawler2/contracts/` for now (P0 skeleton: name, version,
  build). Both repositories depend on an **exact pinned version**; it can
  move to its own repository without changing consumers.
- Versioning: **stable semantics, additive schemas** (plan B.5 #5). A
  field's meaning never changes; new optional fields are minor versions;
  consumers ignore unknown fields; removals/renames/meaning changes need a
  new event type or a new major version with a migration window where
  both are produced.
- Each repository runs a **compatibility suite** against the pinned
  version (golden sample payloads, round-trip, unknown-field tolerance).

## Consequences

- P1 defines the first schemas (IDs, events, media-centric contract) and
  event ownership. P0 deliberately defines none.
- The fingerprinter adopts the package in P9; until then V1's bridge is
  not ported (plan A.3).
