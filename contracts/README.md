# antipiracy-contracts

The versioned contract language of Anti-Piracy V2: identities, domain
models, the event envelope, every inter-component event, the ownership
catalog, generated JSON Schemas and golden compatibility fixtures.
Shared by `crawler2` and the `fingerprinter` (ADR-007, ADR-010).

- Version **1.0.0** = contract major 1. Sole runtime dependency: pydantic.
- Design: `docs/phases/p01-contracts/design.md`; events:
  `docs/phases/p01-contracts/events.md`; decisions: ADR-008 (IDs),
  ADR-009 (envelope and evolution), ADR-010 (scope), ADR-011 (boundary).

Rules (ADR-009):

- a field's meaning never changes; schemas evolve additively (minor);
- consumers ignore unknown fields and accept newer minors of their major;
- each event type has exactly one producing component (enforced);
- breaking changes need a new major payload with a dual-publish window;
- released fixtures in `compat/fixtures` are immutable (SHA-256 manifest);
- JSON Schemas in `schema_json/` are generated (`make schemas`), never edited.

Both repositories pin an exact version and run the compatibility suite:

```python
from antipiracy_contracts.compat import event_fixtures
from antipiracy_contracts.events import decode_event

for fixture in event_fixtures():
    decode_event(fixture.raw)
```
