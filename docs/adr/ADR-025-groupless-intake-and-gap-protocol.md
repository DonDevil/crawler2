# ADR-025 — The fingerprinter reads `encode.requested` without a consumer group; losses are detected by stream counts and closed only by replay or explicit acceptance

Status: Accepted (2026-10-10, P9 final design approval, decisions P9-D6 and
P9-D27). **ADR-013, ADR-022, ADR-023, ADR-024, the crawler2 relay/publisher
and every P1 contract are unchanged.**

## Context

crawler2 publishes `encode.requested` to `events:encode.requested.v1`. A
consumer group there would make crawler2's relay wait for the fingerprinter
at the 64 MiB stream budget (ADR-022): a stopped fingerprinter could stall
crawling (B.5 #1). The fingerprinter may not read crawler2's outbox (B.5 #2,
service-owned data). An ungrouped stream is trimmed by crawler2's publisher
(`MAXLEN ~`, byte cap), so unread entries can be lost while the
fingerprinter is behind. Redis stream IDs are not contiguous, so "first
retained ID ≠ cursor + 1" cannot detect a loss.

## Decision

1. One leased reader (Scylla `intake_state`: owner, epoch LWT, CAS cursor)
   follows the stream with `XRANGE` from a durable cursor.
2. Stream IDs are compared only as `(ms, seq)` pairs. Loss is detected by
   **counts**: `T = entries-added − length` entries were ever removed; with a
   consumed ordinal `o`, `T > o` means `T − o` unread entries were trimmed.
   `max-deleted-entry-id > cursor` opens a conservative tombstone gap.
3. On a gap the reader records it and **continues** (halting would let
   crawler2 trim more). Gap records: `OPEN → REPLAY_REQUESTED →
   REPLAY_OBSERVING → CLOSED_REPLAYED | REPLAY_INCOMPLETE`, or
   `REPLAY_UNAVAILABLE`, or `CLOSED_ACCEPTED` — the last **only** by an
   explicit, audited operator `accept-gap`. Nothing closes a gap
   automatically except the observed end of a replay.
4. Repair = crawler2's existing `crawler2-storage relay --replay-from/--replay-to`
   (new IDs at the tail; consumers are idempotent).
5. `HALTED` only for a stream reset or an uninitialized cursor; explicit
   `init --from-start|--from-now` and `reinit --after-reset`.

## Consequences

- crawler2 never waits for the fingerprinter.
- An accepted gap can **permanently** lose `encode.requested` events until a
  future crawler2 mechanism re-issues them (crawler2 never re-requests,
  P8-D7). The loss is visible (gap record, `fingerprinter_intake_open_gaps`,
  non-zero `intake status`) and an accepted gap is never reported as
  recovered (approved trade-off, decisions.md §0).
- The fingerprinter cannot enumerate missed event IDs; the replay window is
  the evidence. Replay re-publishes all crawler2 event types in the window.
- Implementation: fingerprinter `fingerprinter/intake.py`; tests TG-1…TG-14
  (`tests/v2/test_intake.py`).
