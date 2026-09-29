# P3 — Frontier & scheduling

Status: **design** (implementation, tests and results follow in later
sections of this document as they land).
Audit: [audit.md](audit.md). Decisions: [ADR-015](../../adr/ADR-015-frontier-execution-model.md),
[ADR-016](../../adr/ADR-016-frontier-task-lifecycle.md).

## 1. Goal

Port V1's proven Redis frontier (atomic Lua claim/lease/heartbeat/
recovery, Redis `TIME`) into V2, reshaped for execution-class queues,
politeness shared across those queues, recrawl, scheduled work, bounded
admission and a single retry authority.

**Redis is hot execution state. Scylla is durable long-term knowledge.**
"Seen historically" is not "currently scheduled", and "visited" is not a
frontier state at all.

## 2. Scope

In: the Redis data model and Lua scripts; `admit`, `claim`, `heartbeat`,
`complete`, `fail`, `defer`, `recover`; execution queues `http`,
`browser`, `tor`, `selenium`; shared domain gate with per-domain interval
override; scheduled (future-dated) tasks; per-queue admission limits;
dead letters for recovery-exhausted tasks; a heartbeat helper for P4
workers; mapping from P1 `CrawlRequested` to an admission; consistency
audit; tests, property test, benchmarks.

Out (P4+): fetchers and worker runtimes of any kind, extraction,
filtering, admission *policy* (what to crawl, when to recrawl — P6/P7),
failure classification (P4), anything touching Scylla. The frontier does
not read or write Scylla.

## 3. Relationship to V1

V1 stays untouched and is the reference implementation and benchmark
source; V1's own benchmark scripts are run, read-only, against the same
Redis to produce comparable numbers. What is kept, changed and dropped,
with V1 locations, is in [audit.md](audit.md). In short: the atomic-script
discipline, token CAS, lease ZSET + recovery sweep, attempt/backoff rule,
deferral and heartbeat are ported; permanent terminal sets (D10), the
single queue, the K-bounded domain scan (D13) and unbounded admission are
removed.

## 4. Relationship to P1/P2

- P1 `UrlRef` (url, url_id, domain_id) is the unit of admission; Redis
  keys are built only from `UrlId`, `DomainId` and the closed
  `ExecutionQueue` registry — never from raw input.
- P1 `Priority` 0–100, **higher first**, default 50, is the only priority
  scale.
- P1 `CrawlRequested` maps to an admission via `admission_from_request`
  (capability → queue, `not_before` → scheduled, "merge per url_id,
  highest priority wins" as the catalog specifies). The
  `FetchCapability`/queue mismatch is recorded in audit.md and ADR-015; no
  P1 change.
- P2: Scylla keeps URL/page/attempt history (`UrlRepository`,
  `FetchAttemptRepository`); Redis Streams carry events; the frontier
  shares the Redis instance and must stay bounded (§16). The P2 10×
  latency gate is **open for an environment reason unrelated to P3**:
  development runs from an external 5 400-rpm USB HDD hosting the Ubuntu
  environment; the machine's Windows NVMe is intentionally not modified
  and nothing is moved to it. The P2 latency benchmark will be re-run
  under WSL on the NVMe or another high-IOPS environment. P3 does not
  depend on Scylla latency.

## 5. Architecture

```
 durable seen knowledge (Scylla, P2; policy P6/P7)
        │  admit(url, queue, priority, not_before)       ── rejected_full → caller keeps it (Scylla)
        ▼
 ┌─ admission ── depth[queue] < max_depth? ── duplicate/merge per url_id
 │      │ not_before > now                    │ due now
 │      ▼                                     ▼
 │  scheduled ZSET ──(due; lazy in claim/recover)──► q:{queue}:{domain}
 │      ▲                                     │   + ready:{queue} eligible-domain index
 │      │ retry backoff / defer               │   (only domains whose shared gate is open)
 │      │                                     ▼ claim(queue): best domain, pop head,
 │      │                                        close gate for ALL queues, lease
 │      │                                  leased (token, lease expiry in leases ZSET)
 │      │                                     │ heartbeat → extend lease
 │      ├────────── fail (attempt < max) ─────┤
 │      │                                     ├── complete → task deleted (URL admittable again)
 │      │                                     ├── fail (attempts exhausted) → task deleted, caller told
 │      └── recover: lease expired, attempt < max
 │                   lease expired, exhausted ──► dead letter (TTL, capped; not a dedup record)
```

Package: `crawler2/frontier/` (types, errors, config, heartbeat helper,
contract mapping) and `crawler2/frontier/redis/` (implementation, Lua),
per ADR-001.

## 6. Redis data model

All keys are prefixed `{redis.namespace}:fr:` (`crawler2:fr:` by
default).

| Key | Type | Content | Bounded by |
|---|---|---|---|
| `task:{url_id}` | HASH | `url dom q pri st att tok lex cat due seq rsn adm err` | active tasks (≤ Σ max_depth) + dead letters |
| `q:{queue}:{domain_id}` | ZSET | url_id → rank | ready tasks |
| `ready:{queue}` | ZSET | domain_id → rank of that domain's head in `queue` | domains with ready work and an open gate |
| `gate` | ZSET | domain_id → next allowed claim time | domains claimed within their interval (+ one sweep of lag) |
| `interval` | HASH | domain_id → minimum interval override (s) | operator/P7 overrides |
| `scheduled` | ZSET | url_id → due time | scheduled tasks |
| `leases` | ZSET | url_id → lease expiry | leased tasks |
| `dead` | ZSET | url_id → time of death | `dead_ttl_s`, `dead_max` |
| `depth` | HASH | queue → tasks in scheduled/ready/leased | `max_depth[queue]` |
| `seq` | STRING | admission sequence (INCR) | — |
| `stats` | HASH | monotonic counters (admitted, merged, duplicate, rejected, claimed, completed, retried, exhausted, deferred, recovered, dead, stale, anomaly) | fixed fields |

`rank = (100 − priority) · 10¹³ + seq`: priority is the major key (higher
priority → smaller rank → served first), `seq` keeps FIFO order inside a
priority. 10¹³ leaves room for 10¹³ admissions before bands could touch,
and every rank is an exact integer in a double (< 9·10¹⁵). V1's 10⁶ band
overflowed after a million admissions (audit, defect 1).

Task states (`st`): `scheduled`, `ready`, `leased`, `dead`. A
"retry-scheduled" task is `scheduled` with `att > 0`. There is no
`visited`, `skipped` or `failed` state.

## 7. Execution queues

`ExecutionQueue` = `http | browser | tor | selenium`: execution classes,
not implementations. Each queue has its own eligible-domain index, its own
per-domain task queues and its own `max_depth`; a worker claims from
exactly one queue. Adding a queue = adding an enum member (and its
`max_depth` default); every script receives the queue list as an argument
and loops over it, so no script changes. P1 capability → queue:
`http→http`, `browser→browser`, `tor_http→tor`, `tor_browser→tor`;
no capability → `http`. `selenium` is only reachable by explicit choice
(ADR-015).

A task belongs to one queue at a time; `fail(…, next_queue=…)` moves it
(capability escalation, V1's hybrid chain, now inside the one retry
budget).

## 8. Domain politeness

**Invariant:** for every domain, two successive claims — from any queue,
any worker, any host — are at least that domain's interval apart
(`interval[domain]` if set, else `default_interval_s`), measured on Redis
`TIME`.

Mechanism: the claim script, after popping domain `d`'s head, writes
`gate[d] = now + interval` and removes `d` from **every** queue's
`ready:{queue}` index. A domain is only ever claimable through a
`ready:{queue}` index, and it only re-enters those indexes when a claim or
recovery sweep finds `gate[d] ≤ now` (lazy promotion). Selection, gate
check, pop and gate update happen in one script, so no interleaving of
workers can observe an open gate twice. With interval 0 the gate is not
written at all (identical to V1's `rate_limit = 0`).

Work-conserving across queues: when the gate reopens, `d` re-enters every
queue that has work for it; whichever queue claims first wins. A queue
with no running workers never blocks the others. The consequence — no
FIFO order *between queues* on one domain — is measured in §21.

## 9. Temporary deduplication

The dedup unit is the **active task** `task:{url_id}`:

| Situation | `admit` result |
|---|---|
| no task | `ready` or `scheduled` (or `rejected_full`) |
| task `ready`/`scheduled`, request raises priority or (scheduled) moves due time earlier | `merged` |
| task `ready`/`scheduled`, nothing to improve | `duplicate` |
| task `leased` | `duplicate` (the running attempt is not disturbed) |
| task `dead` | the dead letter is dropped; admitted as new |

A URL becomes admittable again the instant its task is completed,
exhausted by `fail`, or dead-lettered. Lifecycle of the dedup key: created
by `admit`, deleted by `complete` / exhausting `fail`; dead-lettered tasks
expire after `dead_ttl_s` and never block admission. Nothing in Redis
records that a URL was ever crawled; that is Scylla's job, and whether to
admit it again is P6/P7 policy.

## 10. Scheduled work

`admit(…, not_before=t)` with `t > now` puts the task in `scheduled`
(score `t`). Retries and deferrals use the same ZSET. Promotion (`scheduled`
→ `q:{queue}:{domain}` + index) runs inside every `claim` and `recover`
call, bounded by `promote_batch` per call, and is one atomic script:
`ZREM scheduled` + enqueue + state change happen together or not at all,
so a crashed promoter cannot lose or half-move a task, a duplicate
promotion finds the task no longer `scheduled` and does nothing, and
concurrent promoters are serialised by Redis. A due task is claimable on
the first claim after its due time. P3 decides *how* scheduled tasks
become executable; *what* to schedule and when is P7.

Scheduled tasks count against `max_depth` (§16), so long-horizon recrawl
calendars belong in Scylla (P7) and are fed to the frontier near their due
time.

## 11. Lease lifecycle

1. `claim(queue)` (one script): promote due scheduled tasks and due gates
   (bounded); take the best domain from `ready:{queue}`; pop its head;
   close the domain's gate; `att += 1`; store a fresh uuid4 token, lease
   expiry `lex = now + lease_ttl`, `claimed_at`; `ZADD leases`. Returns a
   `Claim` (url_id, url, domain_id, queue, priority, attempt, token,
   lease_expires_at, claimed_at, reason).
2. The token is the only proof of ownership. At any instant a task stores
   at most one token, so at most one worker can own it.
3. `heartbeat(claim)` extends the lease iff the token is current.
4. Exactly one of `complete`, `fail`, `defer` ends the attempt, each
   token-CAS; a stale token is a no-op returning `stale`.
5. `recover()` handles leases whose expiry has passed (§13).

## 12. Heartbeat

`heartbeat` returns the renewed `Claim`, or `None` when the token is no
longer current (lease recovered, task completed) — the worker must stop
and must not report an outcome. A Redis error raises
`FrontierUnavailable`, never `None`, so an outage is not mistaken for a
lost claim (V1 semantics). `crawler2.frontier.heartbeat.run_with_heartbeat`
(port of V1 `claim_heartbeat.py`) runs a coroutine while renewing every
`lease_ttl / 3` and raises `ClaimLostError` when renewal returns `None`.
A heartbeat arriving after expiry but before recovery succeeds: the lease
was never reassigned, so ownership is still unique.

## 13. Crash recovery

`recover()` (run periodically by any process; safe to run from many at
once): `ZRANGEBYSCORE leases -inf now LIMIT batch`; for each expired lease
the token is cleared (the crashed or slow worker's later calls become
`stale`) and the expiry counts as a failed attempt: `att < max_attempts` →
scheduled after backoff; otherwise → dead letter (state `dead`,
`ZADD dead`, hash TTL `dead_ttl_s`, depth released). It also promotes due
scheduled tasks and trims dead letters older than `dead_ttl_s` or beyond
`dead_max`. Recovery latency after a crash ≤ `lease_ttl +
recovery interval + backoff`.

## 14. Retry authority

The frontier is the only retry mechanism (D4). Workers report; the
frontier decides:

| Worker call | Meaning | Frontier decision (P3 mechanics) |
|---|---|---|
| `complete(claim)` | the attempt finished; nothing to retry (any HTTP status, a policy skip, a 404) | task deleted |
| `fail(claim, reason, next_queue=None)` | the attempt failed in a way worth retrying | `att < max_attempts` → `scheduled` at `now + min(base·2^(att−1), max_backoff)`; else `exhausted`, task deleted, caller records it |
| `defer(claim, delay=None)` | not attempted (local network outage, N1–N7) | `att −= 1`, scheduled at `now + defer_delay_s` |
| lease expiry | worker vanished | same as `fail` |

P3 mechanics: attempt counting, backoff formula and cap, per-call queue
move, dead letters. Future P7 policy (extension points, not implemented):
choosing `max_attempts`/backoff per domain or fetch profile, choosing the
escalation queue, deciding to re-admit dead letters. Workers must not
retry internally beyond a single transport-level attempt budget defined
in P4.

## 15. Backpressure

`depth[queue]` counts that queue's tasks in `scheduled`, `ready` and
`leased`. Ready depth, scheduled work, in-flight leases and pending
retries are therefore all covered by one number per queue.

## 16. Admission control

**Invariant:** `admit` of a *new* task into `queue` succeeds only while
`depth[queue] < max_depth[queue]`; otherwise it returns `rejected_full`
and changes nothing. Transitions of already-admitted tasks (claim,
retry, defer, recovery, promotion, queue move) never consult the limit, so
backpressure can never lose or strand admitted work, and retries cannot
deadlock behind new work. A queue move may leave the target above its
limit; it only blocks further *new* admissions there. Global bound:
Σ `max_depth` tasks plus capped dead letters. `rejected_full` is an
explicit result, never a silent drop: the caller (P6/P7 admission
consumer) keeps the request — the URL is already durable in Scylla — and
re-requests later. Redis also runs `maxmemory` + `noeviction`, so a
misconfigured limit ends in an error, never in evicted tasks.

## 17. Eligible-domain index decision

Chosen: **eligible-domain index** (`ready:{queue}` + shared `gate`),
V1 Step 8A approach C+D adapted to several queues; `domain_scan_limit` is
not ported. Evidence and the measured comparison with V1's K-scan are in
§21–§22; the ADR is ADR-015.

- Key structure: `ready:{queue}` (domain → head rank), `gate`
  (domain → next allowed time). A domain is in at most one of
  {gate} ∪ {ready:*}: gated domains are in no ready index.
- Atomic update rules: admit/promote add a domain to `ready:{queue}` only
  if it has no gate entry; claim removes it from every index and gates it
  (interval > 0) or resyncs its head in the claimed queue (interval 0);
  gate promotion re-reads each queue's *current* head.
- Stale entries: a ready entry whose queue is empty is removed when met
  (counted as `anomaly`; none are expected); a gate entry for a domain
  with no work is dropped at promotion.
- Recovery: all structures are derivable from the task hashes; `audit()`
  checks every invariant (used by tests and the property test).
- Priority: each index orders domains by their head's rank, so the best
  claimable task in a queue is `ZRANGE ready:{queue} 0 0` → its domain's
  head — the same strict `(priority, seq)` order as V1, over all eligible
  domains instead of the top K.
- Bound: per claim, at most `promote_batch` scheduled tasks and
  `promote_batch` gates are promoted; selection itself is O(log N).

## 18. Atomicity / Lua guarantees

- Each operation is one `EVALSHA` of one script; Redis runs a script to
  completion without interleaving, so every multi-key transition is
  atomic with respect to all other clients. Effects replication/AOF write
  the script's effects as one `MULTI/EXEC`, so a Redis crash cannot
  persist half a transition (AOF `aof-load-truncated` drops an incomplete
  trailing transaction).
- Scripts compute keys from arguments (dynamic domain/url keys), which is
  valid for one Redis primary (ADR-006) and deliberately not Redis Cluster
  compatible.
- The clock is Redis `TIME`; tests may inject an explicit time argument
  (a constructor-only `clock`, never set in production).
- No client-side read-decide-write sequence exists anywhere in the
  frontier.

## 19. Redis failure semantics

Every operation raises `FrontierUnavailable` on a connection or timeout
error and never returns a result it did not receive from Redis:

| Call during outage | Behaviour | After Redis returns |
|---|---|---|
| `admit` | raises; the caller keeps the request | re-admit (idempotent: `duplicate`/`merged` if it had landed) |
| `claim` | raises; the worker owns nothing | normal |
| `heartbeat` | raises (not `None`); the worker keeps working and retries until its lease would lapse | renews if not yet recovered, else `None` → stop |
| `complete`/`fail`/`defer` | raises; the outcome is unknown to the worker | retry with the same token; `stale` then means applied or lease lost — either way the worker is done |
| `recover` | raises | next sweep catches up (bounded batches) |

An ambiguous failure (script ran, reply lost) is resolved by the same
rules: a lost `claim` reply leaves a lease nobody holds, which expires and
is recovered (one attempt consumed). The frontier provides **no
durability beyond Redis's own** (AOF `everysec` in compose: up to ~1 s of
transitions can be lost on a Redis crash; tasks lost that way are
re-derived from Scylla by P6/P7 admission, never from Redis).

## 20. Tests

_Filled in during validation._

## 21. Benchmarks

_Filled in during validation._

## 22. Results

_Filled in during validation._

## 23. Known limitations

_Filled in during validation._

## 24. Deferred decisions

_Filled in during validation._

## 25. Exit-gate status

_Filled in during validation._
