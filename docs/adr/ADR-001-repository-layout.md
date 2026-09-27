# ADR-001 — Repository layout and logical boundaries

Status: Accepted (P0, 2026-09-28); P1 contract location amended by
[ADR-010](ADR-010-contract-package-scope.md)

## Context

V1 grew as one flat tree in which engines, frontier, storage and
"intelligence" import each other freely (plan A.2 D3, D9). V2 must let
individual intelligence, ML, indexing and storage implementations be
replaced in V3 without rewriting the crawler execution layer, and it must
run the same code on one host or many (ADR-006).

## Decision

One importable package, `crawler2`, whose sub-packages are the logical
boundaries of the plan. Direction of dependency is fixed:

```
crawler2/
  core/            configuration, identity, observability  (P0)
                   contracts/, events/, models/, ids        (P1)
  storage/         Scylla repositories, object store, outbox (P2)
  frontier/redis/  Redis frontier, leases, Redis TIME       (P3)
  crawlers/        worker runtime + fetcher plugins:
                   http/, async/, playwright/, tor/, selenium/ (P4)
  extraction/      single-parse HTML, URL + media extraction (P5)
  filtering/       filter engine (+ discovery/)              (P6)
  intelligence/    crawl/source intelligence (producers only) (P7)
  media/           media registry and identity               (P8)
  evidence/        evidence collector/finalizer              (P12)
  analytics/       derived datasets, DuckDB                  (P13)
  orchestration/   process/role supervision per host         (P4/P14)
  diagnostics/     operational checks                        (P0)
contracts/         antipiracy-contracts package (ADR-007)
```

Rules:

1. `core` depends on nothing else in `crawler2`.
2. Execution layers (`frontier`, `crawlers`, `extraction`, `storage`)
   never import `intelligence`, `analytics` or the fingerprinter (plan
   B.5 #1). Intelligence only produces tasks/profiles through the frontier
   and Scylla.
3. Cross-service data (crawler2 <-> fingerprinter) crosses only through
   `antipiracy-contracts` types over Redis Streams (ADR-004, ADR-007).
4. Every replaceable implementation sits behind an interface declared by
   its consumer; implementations are selected by configuration.
5. **A sub-package is created by the phase that first puts code in it.**
   P0 creates only `core/configuration`, `core/observability`,
   `core/identity.py` and `diagnostics`; the rest of the tree above is the
   agreed target, not empty directories.

Tests mirror the boundaries: `tests/unit`, `tests/integration` (compose
stack), `tests/fixtures` (local fixture web server; no live internet in
CI), and, from P1, `tests/contract` (shared suites each implementation of
an interface must pass).

## Consequences

- Import-direction rules are checkable; a lint rule (import-linter or a
  ruff `banned-api` rule) is added in P1, once there are two boundaries
  to enforce.
- Single package keeps packaging and the Docker image simple; splitting a
  boundary into its own distribution later is a mechanical move.
