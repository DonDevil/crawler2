# ADR-010 — Contract package scope and schema source of truth

Status: Accepted (P1, 2026-09-28). Amends ADR-007 (scope and schema source).

## Context

ADR-007 planned `antipiracy-contracts` as crawler2 ↔ fingerprinter only,
with hand-written JSON Schemas as the source of truth and generated Python
models. ADR-001 planned `crawler2/core/{contracts,events,models,ids}` for
crawler-internal contracts. Designing P1 showed two problems:

1. Components inside crawler2 (workers, extraction, media registry,
   intelligence, evidence) run as separate processes on separate hosts.
   Their events are wire contracts with the same evolution needs. Two
   contract homes would mean two envelopes, two catalogs and two
   compatibility regimes.
2. Hand-written JSON Schema plus code generation adds a toolchain but no
   expressiveness. Cross-field invariants (derived-ID consistency, fetch
   outcome rules) cannot be expressed in JSON Schema anyway.

## Decision

- `antipiracy-contracts` holds **all V2 inter-component contracts**: IDs,
  canonical URLs, digests, domain models, the envelope, every event
  payload, the ownership catalog, JSON Schema export and golden fixtures.
  crawler2 has no separate `core/contracts|events|models|ids` packages.
- **Pydantic models are the source of truth.** JSON Schemas are generated
  deterministically (`python -m antipiracy_contracts.schemas`, `make
  schemas`) into `antipiracy_contracts/schema_json/` and committed. A test
  fails when they are stale, so every contract change shows up as a schema
  diff in review. Non-Python consumers use these files.
- Runtime dependency: **pydantic only** (already pinned by crawler2).
  Imports are restricted to stdlib, `pydantic`, `pydantic_core` and the
  package itself; `tests/contract/test_boundaries.py` enforces this. This
  is the first enforced import boundary promised by ADR-001.
- Package version **1.0.0** = contract major 1. Minor releases are
  additive (ADR-009). Consumers pin an exact version (ADR-007).
- Fixtures and the compat helpers ship **inside** the package
  (`antipiracy_contracts.compat`), so the fingerprinter's CI runs exactly
  the fixtures of the version it pins.

## Consequences

- The fingerprinter depends on event types it never consumes (for
  example `page.observed`). They are inert data classes, and a change to
  one is additive by policy, so this costs nothing at runtime.
- Moving the package to its own repository later is a mechanical move
  (ADR-007 still applies).
- ADR-001's tree is amended: the P1 row `core/ contracts/, events/,
  models/, ids` now lives in `contracts/src/antipiracy_contracts/`.
