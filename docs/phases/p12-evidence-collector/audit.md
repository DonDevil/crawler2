# P12 audit — evidence collector & finalizer (milestone M5)

Date: 2026-10-22. Scope: what already existed for evidence before P12, what
the inputs actually carry, and what is missing. Design:
[design.md](design.md). Decisions: [decisions.md](decisions.md).

## 1. Requirements source: ADR-005 is still OPEN

[ADR-005](../../adr/ADR-005-legal-compliance.md) lists the questions that
must come from the owner/counsel (jurisdictions, admissibility, screenshots
and clips, retention, WORM, personal data). **None has been answered.** The
plan says ADR-005 "blocks P12 design"; per the phase brief P12 implements a
conservative *technical* baseline (traceability, integrity, honest
gaps) and records the open owner decisions (decisions.md §3). Nothing in P12
claims legal admissibility, chain of custody or compliance with a
jurisdiction's evidence rules.

## 2. What existed (reused, not rebuilt)

| Existing piece | Where | Used by P12 for |
|---|---|---|
| `EvidenceCandidate`, `EvidenceSeal`, `EvidenceArtifact`, `BlobRef`, `ContentDigest` contracts | `contracts/.../models/evidence.py`, `blobs.py`, `digests.py` | the cross-component identity and the write-once seal (unchanged) |
| `evidence.candidate_created` / `evidence.finalized` events (catalog: collector → finalizer → exporter) | `contracts/.../events/evidence.py`, `catalog.py` | `evidence.finalized` is emitted through the outbox at version-1 seal |
| E1 `evidence_by_match` (LWT: one evidence id per match), E2 `evidence` (write-once candidate, seal `IF state='collecting'`), E4 `evidence_by_target` | V001, `storage/scylla/evidence.py` (P2) | evidence identity, seal, per-target/month index; one read method added (`opened`) |
| Content-addressed object store (`raw/sha256/<h>`; `If-None-Match: *` writes; reads verify size + SHA-256) | `storage/objectstore/` (ADR-014) | artifact verification and manifest storage |
| W4 `PageObservation` (requested/final URL, redirect chain, status, content type, capability, `observed_at`, `body_digest`, raw `snapshot` BlobRef) — every successful fetch archives its body (`crawlers/recorder.py`) | P4 | observed provenance + the page-snapshot artifact |
| W1 `FetchAttempt` (worker identity, start/finish) — 30-day TTL | P4 | collection method and worker |
| M3/M5 media registry (sightings, content keys, probe status) | P8 | attribution of a content id to pages |
| P11 attribution rule (M5 → M3 → W4; skip sightings that probed other bytes) | `intelligence/feedback_service.py` | same carrier set; P12 adds per-source basis and keeps W4/W1 detail |
| P2–P5 match/target projections (`record_match`, `target`, `matches_for_target`) | P2 | target identity, campaign match counts |
| `IdempotentConsumer` + Scylla processed-event markers, `RedisStreamReader` (XAUTOCLAIM) | `storage/events/` | consumer idempotency and restart recovery |
| `Component.EVIDENCE_COLLECTOR/FINALIZER/EXPORTER`, `WorkerRole.FINALIZER` | contracts / settings | producer identity |

## 3. What the inputs actually carry

- `match.found` → `MatchResult`: content id, representations, target ref,
  technique, verdict (`confirmed`|`probable`), confidence, segments,
  `decided_at`. **No URL, page or media id** — crawler2 must resolve
  sources through its registry (ADR-011).
- Snapshots are raw **response bodies**, not WARC records; full response
  headers are not retained (only status, content type, validators,
  redirect chain).
- **No screenshot capture exists** anywhere in crawler2 (browser engine
  renders, but never stores an image). **No media bytes** are retained by
  crawler2 (only the content-key digest of the probed sample); the
  fingerprinter keeps no evidence clips either.
- **No campaign/case concept** existed in either repository.
- **No object lock / versioning**: buckets are created plain
  (`S3ObjectStore.ensure_bucket`); immutability is application-level
  (content-addressed keys, `If-None-Match: *`, digest-verified reads).
- **No signing or RFC 3161** support or dependency.
- DuckDB is not a crawler2 dependency (planned for P13).

## 4. Gaps P12 had to close

1. A mutable working candidate (E2's candidate is write-once and needs ≥ 1
   media and observation, so it cannot hold an unattributed or evolving
   candidate) → E5.
2. Append-only finalized versions with supersession (E2's seal is
   write-once per evidence id) → E6.
3. An audit trail (plan: "attempted mutation fails and is audited") → E7.
4. Campaigns → E8/E9.
5. E4 was written only at E2 creation, i.e. at finalization; unfinalized
   candidates were invisible per target — found by the real-stack test,
   fixed by upserting E4 with every candidate (validation.md §3).
6. Canonical manifest, validation, export, report, CLI.

## 5. Dependencies and limits carried into P12

- **P10 / M3 NOT PASSED** (fingerprinter repo): no production matcher
  output exists; every match in P12 tests is a contract-valid fixture.
- **P11 / M4 NOT PASSED**, feedback off by default — unchanged by P12.
- The dev keyspace has not been migrated past V005 (P11 left V006
  unapplied); P12 adds V007 and applies it only to throwaway test keyspaces.
