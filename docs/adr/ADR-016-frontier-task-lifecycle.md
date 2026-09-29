# ADR-016 — Frontier task lifecycle: temporary dedup, scheduling, retry authority, admission limits

Status: Accepted (P3, 2026-09-29).

## Context

V1's frontier remembers every URL forever (`urls:known`,
`visited`/`skipped`/`failed_permanent` sets, D10), so nothing is ever
recrawled, and it admits without bound. V2 keeps long-term knowledge in
Scylla (ADR-002, ADR-012) and needs recrawl (P7), future-dated work
(`CrawlRequested.not_before`, P1), one retry authority (D4) and bounded
Redis memory (Redis also carries the event streams, ADR-013).

## Decision

1. **The frontier stores only active tasks.** One hash per URL,
   `task:{url_id}`, exists while the URL is `scheduled`, `ready` or
   `leased`; its existence is the dedup rule ("currently scheduled or in
   flight"). `complete` and an exhausting `fail` delete it; the URL is
   admittable again immediately. There are no terminal sets.
2. **Duplicate admission merges** per url_id as the P1 catalog specifies:
   higher priority wins; for a scheduled task an earlier due time wins; a
   leased task is never disturbed.
3. **One `scheduled` ZSET** (score = due time) holds future admissions,
   retry backoffs and deferrals; promotion to ready is atomic and happens
   in claim and recovery scripts.
4. **The frontier is the only retry authority**: workers call
   `complete`, `fail` (optionally naming the next queue) or `defer`; the
   frontier counts attempts and applies `min(base·2^(n−1), max)` backoff up
   to `max_attempts`; a lease expiry counts as a failure.
5. **Dead letters only where nobody is told**: a task exhausted by lease
   recovery becomes `dead` (hash TTL `dead_ttl_s`, `dead` ZSET capped at
   `dead_max`). An exhausting `fail` returns `exhausted` to its worker,
   which records the outcome durably. A dead letter never blocks
   re-admission.
6. **Admission limit per queue**: `depth[queue]` = scheduled + ready +
   leased tasks; a new task is refused with `rejected_full` when the queue
   is full; transitions of admitted tasks never check the limit.

## Consequences

- Recrawl is possible: history is in Scylla, the frontier only knows
  what is executable now.
- Redis memory is bounded by Σ `max_depth` plus capped dead letters.
- A refused admission is an explicit result the caller must keep (the URL
  is already in Scylla); it is never dropped by the frontier.
- A caller that loses its `rejected_full`/`admit` reply re-admits; admission
  is idempotent per url_id.
- Long-horizon recrawl calendars must not be parked in Redis; P7 keeps them
  in Scylla and feeds due work.
