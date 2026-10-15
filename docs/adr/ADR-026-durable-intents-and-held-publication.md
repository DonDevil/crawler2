# ADR-026 — Fingerprinter facts commit by one LWT; durable intents drive follow-ups; events are held until their consumers exist

Status: Accepted (2026-10-10, P9 final design approval, decisions P9-D13,
P9-D20, P9-D21, P9-D22). **ADR-022's retention rules are applied by the
fingerprinter to its own streams; no crawler2 ADR or contract changes.**

## Context

A representation needs an object (MinIO), a row (Scylla), index entries, a
request transition, target bindings and a `representation.ready` event. A
crash between any two writes must not lose work, duplicate an authoritative
fact or leave an event unpublished forever. Redis locks have no fencing.
Target events have no consumer yet (P7 follow-up / P10); publishing them to an
ungrouped stream lets trimming destroy the only copy.

## Decision

1. Artifacts use attempt-unique keys; the **only commit point** is
   `INSERT INTO representations … IF NOT EXISTS`; first committer wins and no
   code path updates artifact columns. Request transitions are fenced by
   `attempt_id`.
2. A durable **intent** (no TTL) is written before every fact LWT; follow-ups
   are idempotent, flagged on the authoritative row and completed by the
   worker or by the reconciler from the row; a daily full pass recreates
   anything an intent could miss; the orphan sweeper runs after
   reconciliation, checks only the authoritative row, open intents and live
   attempts, and waits `orphan_grace_s` (> `encoder.attempt_timeout_s`).
3. A **stream registry** lists each fingerprinter stream's required groups and
   hold flag. Nothing is published before its required consumers exist;
   `representation.ready`/`encode.failed` require crawler2's `media-registry`;
   target streams are held until a consumer registers
   (`publisher register-consumer`); `republish` bootstraps late consumers from
   Scylla facts. XADD is acknowledged as "appended", never as "processed".

## Consequences

- At-least-once delivery: a crash between XADD and acknowledgement
  re-publishes the same `event_id`; consumers must stay idempotent.
- One extra write per fact and a reconciler process.
- Held events accumulate (bounded by facts) until consumers register.
- Implementation: `commit.py`, `reconcile.py`, `publish.py`; tests TC-1…TC-14,
  TE-1…TE-8.
