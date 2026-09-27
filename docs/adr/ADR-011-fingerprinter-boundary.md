# ADR-011 — crawler2 ↔ fingerprinter boundary and data ownership

Status: Accepted (P1, 2026-09-28)

## Context

In V1 the fingerprinter's unit of work was a `(candidate media, target)`
pair (`work_queue.jobs.Job`: `media_evidence_id`, `media_url`,
`target_id`, `target_version`, `techniques`). Media were re-embedded for
every target, and priority was the choice of stream (D12). V2 makes
fingerprinting media-centric: encode each piece of content once, then
match representations against targets through ANN candidate reduction
(plan P9/P10, B.5 #4).

## Decision

### Ownership

| Data | Owner (keyspace) | The other side learns it via |
|---|---|---|
| domains, pages, observations, fetch attempts, links | crawler2 | — |
| media entities, media observations, content identities | crawler2 (media registry) | `encode.requested` |
| targets and target versions, reference media | fingerprinter (target manager) | `target.registered`, `target.retired` |
| representations (vectors, metadata), target representations | fingerprinter (encoder) | `representation.ready`, `encode.failed` |
| vector index (derived, rebuildable) | fingerprinter | — |
| match results (authoritative) | fingerprinter (matcher) | `match.found` |
| evidence | crawler2 | — |

**One shared Scylla cluster, service-owned keyspaces** (`crawler2`,
`fingerprinter`; ADR-002). A service never reads or writes the other's
keyspace. Where one side needs the other's facts (the registry's view of
representation status, crawler2's view of matches and targets), it keeps
its own **projection**, built from events in its own keyspace. There is no
giant shared schema. Object storage follows the same rule: separate
buckets per service, and a `BlobRef` is dereferenced only by its owner
unless the reference was explicitly handed over in a contract.

### Messages

```
crawler2 (media registry) ── encode.requested ─────────────► fingerprinter (encoder)
crawler2 (media registry) ◄─ representation.ready / encode.failed ── encoder
crawl intelligence        ◄─ target.registered / target.retired ─── target manager
feedback, evidence        ◄─ match.found ────────────────────────── matcher
```

- Work is keyed by **`ContentId`** (level-3 candidate identity), not URL
  and not target. The same bytes at many URLs are encoded once. A new
  target is matched against existing representations without re-encoding.
- `encode.requested` carries only what is needed to fetch and process
  plus provenance: `content` (scheme, digest, id), `source` (`Media`:
  id, locator, kind), optional `referer`, optional `required_spec`, and
  `priority`. It carries no crawler internals and no target.
- A representation spec (`name`, `version`, `config_digest`) is **opaque**
  to crawler2. Model family, sampling, precision, tensor layout, index
  type and GPU details stay inside the fingerprinter and can change behind
  a new spec version without a contract change.
- `match.found` carries content ID, both representation IDs, the target
  version, technique + version, verdict, confidence and aligned segments.
  crawler2 maps content back to media, observations and pages through its
  own registry. Non-matches are not events.
- `encode.failed` is never a "no match". V1's three-way distinction
  (match / no match / processing failure) is preserved: failure is its own
  event, with `retryable`.
- **Priority** is an explicit 0–100 field (higher = sooner) with one
  meaning for every consumer. It orders work inside the consumer's queue
  and is never mapped to separate streams (fixes D12).

## Consequences

- The V1 bridge and job schema are not ported. The fingerprinter adopts
  these contracts in P9 (its repository pins `antipiracy-contracts`).
- The registry must keep a representation-status projection (content ×
  spec → requested/ready/failed) so that it emits `encode.requested` only
  for missing representations (P8 exit gate).
- Evidence (P12) must reconstruct provenance entirely from crawler2 data
  plus the `match.found` payload. It never queries the fingerprinter's
  keyspace.
