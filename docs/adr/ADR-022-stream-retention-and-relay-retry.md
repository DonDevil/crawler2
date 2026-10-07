# ADR-022 — Event streams bounded by bytes, never trimmed past unread entries; relay retries in-process

Status: Accepted (2026-10-07, P7 pre-collection hardening; resolves P6 X-8,
X-11, X-16). Refines the transport bound of ADR-004 and the relay of
ADR-013. No contract change.

## Context

The relay published with `XADD MAXLEN ~ stream_maxlen`, which caps entries
whatever their size and whoever has read them. In M1 run 2 (P6 validation
§4.3) it went wrong twice:

- 10,000 `urls.discovered` entries of ~40 KB (up to ~4 MB) filled Redis
  (768 MB, `noeviction`). The relay could not publish, exited 780 times and
  stalled the closed loop for 80 min (X-16).
- After `stream_maxlen` was lowered, the outbox backlog replay trimmed
  ~1,850 `page.observed` entries before extraction had read them (lost
  events).

The relay also exited on every storage timeout and relied on its
supervisor (X-11).

## Decision

1. **Consumer-aware trimming.** Before each batch, the publisher trims a
   stream that has consumer groups (exact `XTRIM MINID`) to the oldest entry
   a group still needs: its oldest pending entry, or the entry after its
   last-delivered ID. An entry some group has not acknowledged is never
   trimmed.
2. **Byte budget enforced at the write.** `events.stream_max_bytes` (default
   64 MiB) bounds each stream. When the next event would push a grouped
   stream past it, the publisher delivers the prefix that fits and raises
   `StreamBackpressureError(published=n)`. The relay marks only those `n`
   rows and ends the cycle. The other rows stay unpublished in the outbox
   (the system of record) and are offered again when consumers catch up.
   An event larger than the whole budget passes once the stream has nothing
   unacknowledged left. The stream size is measured (`MEMORY USAGE … SAMPLES 0`)
   before every batch, and in-batch growth is estimated conservatively
   (envelope + 1 KiB per entry), so any overshoot is bounded by one batch's
   estimation error.
3. **Streams without groups** have no reader who could miss an entry. They
   keep at most `stream_maxlen` entries (approximate) and `stream_max_bytes`
   bytes, and the oldest entries go first.
4. **Relay retries in-process.** `run_forever` catches
   `StorageUnavailableError` (Scylla timeouts, Redis errors) and
   backpressure, pauses `relay_poll_interval_s` doubling up to
   `relay_retry_max_s` (30 s), and continues. Only start-up (connecting to
   Scylla) can still exit the process, and the supervisor restarts it.

## Consequences

- Redis memory for events is at most `stream_max_bytes` × (number of
  streams) plus one batch's estimation error. Size `maxmemory` for that plus
  the frontier.
- A slow or dead consumer group now **holds back publication** of its
  stream, and the relay's whole cycle stops at the first blocked stream.
  The loop slows down instead of losing events. Fetching continues and
  facts accumulate in Scylla and the outbox. An abandoned group must be
  removed (`XGROUP DESTROY`) by the operator. `outbox_backpressure_total`
  and the `outbox_backpressure` log line show it.
- A consumer group must exist **before** the entries it needs are
  published. A later group starts from what is retained, and older history
  is replayed from the outbox (`--replay-from`), which now also respects
  backpressure.
- The outbox retention (14 days) is the limit on how long a stalled
  consumer can be tolerated without loss.
- A consumer whose state is a rebuildable projection and must not be able
  to hold back the relay reads the outbox instead of joining a group
  ([ADR-023](ADR-023-projection-readers-outside-stream-retention.md), P7).
  This ADR's rules are unchanged by it.
