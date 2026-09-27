# antipiracy-contracts

The single versioned contract boundary between `crawler2` and the
`fingerprinter` service (see `docs/adr/ADR-007-contract-package.md`).

P0 status: package skeleton only (name, version, build). Event/entity
schemas land in P1. Rules that apply from the first schema onward:

- semantics of a field never change; schemas evolve additively;
- consumers ignore unknown fields;
- breaking changes need a new event type or a new major version with a
  migration window;
- both repositories pin an exact version and run the compatibility suite.
