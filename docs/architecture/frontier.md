# Frontier (current state)

The frontier decides **what is executable now, on which execution queue,
and who owns it**. It is an execution scheduler over Redis, not a record
of the web: it forgets a URL as soon as its task ends. Full design,
data model and measured results:
[P3 phase document](../phases/p03-frontier-scheduling/p3-frontier-scheduling.md).
Decisions: [ADR-015](../adr/ADR-015-frontier-execution-model.md) (queues,
shared gate, eligible-domain index), [ADR-016](../adr/ADR-016-frontier-task-lifecycle.md)
(task lifecycle, dedup, retries, admission), [ADR-019](../adr/ADR-019-frontier-domain-inflight-limit.md)
(per-domain in-flight limit).

```
 P6/P7 admission (reads Scylla history)          P4 workers (one per queue)
        │ admit(UrlRef, queue, priority, not_before)     │ claim(queue) ─ heartbeat ─ complete | fail | defer
        ▼                                                ▼
 ┌──────────────────────────── Redis  {ns}:fr:* ─────────────────────────────┐
 │ task:{url_id}  one hash per ACTIVE task (the only dedup record)           │
 │ scheduled      future work, retry backoff, deferrals (due-time ZSET)     │
 │ q:{queue}:{domain} + ready:{queue}   ready work + eligible-domain index   │
 │ gate           one politeness gate per domain, shared by all queues       │
 │ inflight/full  leased tasks per domain (all queues); domains at the limit │
 │ leases         lease expiry per claimed task          depth  per-queue   │
 │ dead           recovery-exhausted tasks (TTL, capped)  stats counters     │
 └────────────────────────────────────────────────────────────────────────────┘
        ▲ recover(): expired leases → retry / dead letter; due work → ready (any process)
```

## Interface (`crawler2.frontier`)

| Call | Caller | Result |
|---|---|---|
| `admit(Admission)` / `admit_many` | P6/P7 admission, seeds | `ready`, `scheduled`, `merged`, `duplicate`, `rejected_full` |
| `admission_from_request(CrawlRequested)` | event consumer | P1 event → `Admission` (capability → queue) |
| `claim(queue)` | worker | `Claim` (token, attempt, lease) or `None` |
| `heartbeat(claim)` / `run_with_heartbeat` | worker | renewed `Claim`, or `None` = claim lost |
| `complete(claim)` | worker | `completed` / `stale` |
| `fail(claim, reason, next_queue=None)` | worker | `retry_scheduled` (with time) / `exhausted` / `stale` |
| `defer(claim)` | worker (local outage) | `deferred` / `stale` |
| `recover()` | periodic sweeper, any host | counts of recovered, dead, promoted |
| `set_domain_interval(domain_id, s)` | operator / P7 | politeness override |
| `set_domain_inflight_limit(domain_id, n)` | operator / P7 | in-flight override (0 = unlimited) |
| `stats()`, `dead_letters()`, `audit()` | ops, tests | counts; invariant check |

Every call is one Lua script on Redis `TIME`; every Redis failure raises
`FrontierUnavailableError`.

## Guarantees

- At most one valid owner per task (token CAS); stale reports are no-ops.
- Politeness: two claims on a domain are ≥ its interval apart across all
  queues, workers and hosts, **and** at most `max_inflight_per_domain`
  (default 2) of its tasks are leased at once — one counter for all
  queues and hosts; a crashed worker's slot returns with its lease.
- Strict `(priority, admission order)` among eligible domains of a queue,
  with no visibility window (no `domain_scan_limit`).
- Admitted work is never dropped: new admissions beyond `max_depth` are
  refused explicitly; retries and recoveries never are.
- A crashed worker's task returns after `lease_ttl` + sweep + backoff;
  after `max_attempts` it becomes a dead letter.
- Redis is the only durability: AOF `everysec` in compose. After Redis
  loss, the frontier is rebuilt by admission from Scylla, not restored.

## Operating

- Run `recover()` every `recovery_interval_s` (30 s) from at least one
  process per deployment; more are harmless.
- `stats().depth` per queue is the backpressure signal; `rejected` in
  `stats().counters` shows refused admissions.
- `audit()` scans the whole namespace; use it offline or in tests.
