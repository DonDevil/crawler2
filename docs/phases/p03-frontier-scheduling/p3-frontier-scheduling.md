# P3 — Frontier & scheduling

Status: **implemented and validated** — all exit-gate items met; see
§25 for the one environment caveat on absolute throughput.
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
| `gateq` | HASH | domain_id → queue that closed the gate | gated domains |
| `yield` | ZSET | `queue\|domain_id` → end of that queue's turn-yield | domains whose gate just reopened |
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

**Turn-taking across queues.** When the gate reopens, `d` re-enters
every queue that has work for it, except that the queue which claimed
last (`gateq[d]`) is held back for one more interval (`yield` entry) if
another queue has work on `d`. Without this the first measurement showed
real starvation: the gate is reopened lazily *inside* whichever claim
arrives first, so four busy-polling `http` workers won all 300 gate
openings on a shared domain and a `browser` task waited forever (§21).
With it, queues alternate on a contended domain; a queue with no running
workers delays the others by at most one interval and never blocks them
(the hold expires into a normal claimable state). Politeness is
unaffected: every path into a ready index still checks the gate.

## 9. Temporary deduplication

The dedup unit is the **active task** `task:{url_id}`:

| Situation | `admit` result |
|---|---|
| no task | `ready` or `scheduled` (or `rejected_full`) |
| task `ready`/`scheduled`, request raises priority, or moves a never-attempted scheduled task earlier | `merged` |
| task `ready`/`scheduled`, nothing to improve | `duplicate` |
| task `leased` | `duplicate` (the running attempt is not disturbed) |
| task waiting for a retry backoff, request is earlier | `duplicate` (backoff is the frontier's decision, D4; priority may still be raised) |
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
`FrontierUnavailableError`, never `None`, so an outage is not mistaken for a
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
  (domain → next allowed time), `yield` (per-queue turn hold, §8). A
  gated domain is in no ready index; a held `queue|domain` is not in
  `ready:{queue}`.
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

Every operation raises `FrontierUnavailableError` on a connection or timeout
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

Integration tests run against the compose Redis (db 9, a namespace per
test, injected clock, `audit()` asserted on teardown); unit tests need
nothing. Commands: [development.md](../../development.md#test-tiers).

| # | Required coverage | Where |
|---|---|---|
| 1 | enqueue/claim/complete; admitted → ready → claimed → heartbeat → completed | `test_frontier.py::test_admitted_ready_claimed_heartbeat_completed` |
| 2 | priority (P1 direction, FIFO within priority, across domains) | `test_higher_priority_first_fifo_within_priority`, `test_priority_across_domains_added_later` |
| 3 | deduplication (active only, merge, concurrency, no tombstone) | `test_duplicate_admission_while_active`, `test_completed_url_is_admittable_again`, `test_exhausted_url_is_admittable_again`, `test_merge_*`, `test_concurrent_admissions_store_one_task` |
| 4 | per-domain politeness, skip-not-block, per-domain override | `test_domain_interval_gates_second_claim`, `test_gated_domain_does_not_block_lower_priority_domain`, `test_per_domain_interval_override` |
| 5 | cross-queue politeness (deterministic and 8 threads on Redis TIME), turn-taking | `test_domain_gate_is_shared_by_all_queues`, `test_concurrent_claimers_respect_domain_interval`, `test_queues_take_turns_on_a_shared_domain`, `test_yield_is_bounded_when_other_queue_has_no_workers` |
| 6 | lease ownership, stale owner rejected | `test_stale_owner_rejected_after_lease_recovery`, `test_no_duplicate_claims_under_concurrency` |
| 7 | heartbeat (keeps lease through sweeps; helper renews, cancels, survives outage) | `test_heartbeat_keeps_lease_through_recovery`, `unit/.../test_heartbeat_*`, `test_lost_claim_cancels_work`, `test_outage_during_heartbeat_is_not_a_lost_claim` |
| 8–9 | lease expiry, crash → recovery → requeue; exhaustion → dead letter | `test_expired_lease_is_recovered_and_reclaimable`, `test_recovery_exhaustion_dead_letters`, `test_dead_letters_are_bounded` |
| 10 | claimed → failure → retry → scheduled → ready → claim; backoff; escalation; defer | `test_fail_retries_with_growing_backoff_then_exhausts`, `test_scheduled_retry_joins_ready_queue_by_priority`, `test_failure_can_escalate_to_another_queue`, `test_defer_keeps_attempt_budget` |
| 11–12 | scheduled ZSET: not early, due, duplicate promotion, concurrent promoters | `test_future_task_not_claimable_early`, `test_past_not_before_is_ready_immediately`, `test_duplicate_promotion_is_harmless`, `test_concurrent_promoters_move_each_task_once`; promoter crash: 1M run (§21, sweeper SIGKILLed 12×) |
| 13–14 | queue depth limits, admission control, saturation | `test_full_queue_rejects_new_work_explicitly`, `test_saturation_never_loses_admitted_tasks` |
| 15 | distributed concurrent claims | `test_no_duplicate_claims_under_concurrency` (8 clients), `test_concurrent_promoters_*`; 1M run with 8 processes (§21) |
| 16 | Redis down: claim/admit/heartbeat/complete/fail/defer/recover/stats, recovery after return, lease lapse during outage | `test_redis_failure.py` (TCP proxy that is cut and restored) |
| 17 | starvation | `test_eligible_domain_is_found_behind_many_gated_domains`; benchmark `starvation.py` (§21) |
| 18 | priority × rate limit | `test_gated_domain_does_not_block_lower_priority_domain`; benchmark `priority_ratelimit.py` |
| — | contract mapping, settings, admission validation | `tests/unit/frontier/test_frontier_unit.py` |

**Property-based state machine** (`test_state_machine.py`, Hypothesis,
60 examples × 40 steps, ~35 s): rules admit / claim / heartbeat /
complete / fail (optionally to another queue) / defer / advance clock /
recover, over 7 URLs on 3 domains and two queues with tiny depth limits.
A reference model predicts every outcome; after every step it checks
`audit()` (no stranded or lost task, depth counters exact, no gated
domain eligible, lease records consistent), per-queue depth and lease
counts against the model, dead-letter count, one owner per task,
politeness on Redis-side claim times, attempt numbers, that a finished
URL is admittable again, and that a second recovery sweep is a no-op. At
teardown, time is advanced until every active task has been claimed and
completed (liveness). A mutation check (shared gate reduced to the
claiming queue) is caught by the machine.

Results: 15 unit + 40 integration frontier tests pass (also inside the
rebuilt app container, i.e. the compose environment); the full default
suite (`make check`) passes.

## 21. Benchmarks

Scripts and raw JSON: `benchmarks/p3-frontier/` (`run.sh all` reproduces
everything; results of the final code in
`results/20260929T053513Z/`). V1 numbers come from **V1's own scripts,
run read-only against the same Redis in the same session**, so V1 and V2
are compared on identical infrastructure.

**Environment.** Intel i5-11400H (6 cores / 12 threads), 15 GB RAM,
Ubuntu 24.04, Python 3.12, Redis 7.4.2 (`redis:7.4.2-alpine`). Two Redis
configurations:

- *benchmark Redis* — host network, AOF off, default `save`, no CPU cap:
  the setup V1's ~13.7k ceiling was measured on (V1: host Redis 7.0,
  localhost TCP). All benchmarks below use it unless stated.
- *compose Redis* — the P0 stack as is: AOF `everysec`, `cpus: 1.0`,
  reached through Docker's port proxy.

| Benchmark | V1 origin | What it checks |
|---|---|---|
| `throughput.py` | `distributed_benchmark.py` | claims/s at 1–16 worker **processes**, 200 000 URLs / 40 domains, no rate limit, no retries, 30 s cap; claim operation = one `claim` + one `complete` script; metric = claims / wall time from spawn to exit (V1's definition); Redis CPU as time-normalised delta |
| `distributed_1m.py` | new (plan exit gate) | 1M tasks, 8 processes, random SIGKILL / SIGSTOP / sweeper kills; lost, duplicated, simultaneous ownership |
| `crash_recovery.py` | `crash_recovery.py` | kill -9 of a real holder process → lease expiry → recovery → reclaim; bulk (50 holders) |
| `heartbeat_endurance.py` | `heartbeat_endurance.py` | 200 claims working 10× the lease with/without `run_with_heartbeat` under a 0.5 s recovery sweep |
| `starvation.py` | `domain_starvation.py` | 7 V1 scenarios + cross-queue; fairness definition from V1's audit §2 |
| `priority_ratelimit.py` | `priority_ratelimit.py` | claim order under priority × 2 s interval, one queue and two queues |
| `eligible_index.py` + `v1_scan_probe.py` | Step 8A/8B probes | victim visibility and claim cost behind N gated domains, V2 index vs V1 K-scan (K = 250) |

**Definitions used by the 1M run.** *Lost*: admitted but neither
completed, exhausted, dead-lettered nor still active. *Duplicate
completion*: two accepted completions of one task (each URL is admitted
once, so `completed` must equal `admitted`). *Simultaneous ownership*: a
claim of task X issued (Redis TIME) before an earlier claim of X stopped
being valid — its last lease expiry (claim or heartbeat) or its explicit
fail/defer. *Legitimate reclaim*: a later claim after the earlier owner's
lease lapsed without an outcome (killed or paused worker); counted, not an
error. *Stale report*: a resumed zombie's outcome rejected as `stale`;
counted, not an error — an **accepted** outcome from a superseded token
would be an error. Workers log each claim/heartbeat/outcome with an
unbuffered write before their next Redis call, so a SIGKILL loses at most
the reply of the in-flight call.

## 22. Results

### Throughput (gate: ≥ V1 ceiling, ~13k claims/s at 8 workers, no rate limit)

| Workers | V2 claims/s | V1 claims/s (same Redis) | V2 Redis CPU |
|---:|---:|---:|---:|
| 1 | 5 742 | 3 964 | 39 % |
| 2 | 10 339 | 7 490 | 73 % |
| 4 | 13 924 | 11 212 | 95 % |
| **8** | **13 224 / 13 525 / 13 659** (3 runs) | 11 509 / 11 325 / 11 455 | 99.5 % |
| 16 | 12 214 | 10 958 | 99 % |
| 8, compose Redis | 9 292 | 8 394 | 92 % |

At 8 workers V2 is **+18 %** over V1 on identical infrastructure and
**13.2–13.7k claims/s** absolute, 0 duplicate completions, 0 lost tasks,
audit clean after every run. Like V1, the ceiling is Redis's single
thread (99.5 % CPU; ~28 µs server time per script); the client fleet is
far from saturated. V2 latency at 8 workers: claim p50/p95/p99 =
321/456/528 µs, complete p50/p99 = 265/464 µs (queueing at Redis, as
V1's audit showed). V1 measures 11.5k on this host today versus its
historical 13.7k (Redis 7.0 host service); both frontiers lose ~30 % on
the compose Redis (AOF + 1-CPU cap + Docker proxy), where neither reaches
13k.

### 1M-claim distributed run with chaos

| | |
|---|---|
| tasks / claims | 1 000 000 / 1 005 167 (8 workers, 1 000 domains, random priorities, 0.5 % failures, 0.1 % slow work with heartbeat) |
| chaos | 68 worker SIGKILLs, 14 SIGSTOP pauses of 3.5 s (> 2 s lease), 12 sweeper SIGKILLs |
| **lost tasks** | **0** (completed = admitted = 1 000 000; active at end 0; audit clean) |
| **duplicate completions** | **0** (counters and logs) |
| **simultaneous ownership** | **0** |
| legitimate lease-expiry reclaims | 69 logged (76 recoveries; 7 claims whose reply died with the worker) |
| retries through the frontier | 5 091 |
| zombie reports rejected as stale | 9; accepted outcomes by superseded tokens: 0 |
| elapsed | 189 s (5.3k claims/s under chaos, logging and pauses) |

### Eligible-domain index vs `domain_scan_limit` (decision evidence)

N better-ranked domains, all gated, each still holding work; the correct
answer is a low-priority victim with an open gate. 500 claims per N.

| N gated | V2 victim found | V2 server µs/claim | V1 (K=250) victim found | V1 server µs/claim |
|---:|---:|---:|---:|---:|
| 50 | 500/500 | 35 | 500/500 | 205 |
| 250 | 500/500 | 32 | **0/500** | 783 |
| 260 | 500/500 | 31 | **0/500** | 799 |
| 1 000 | 500/500 | 32 | **0/500** | 812 |
| 5 000 | 500/500 | 31 | **0/500** | 828 |
| 20 000 | 500/500 | 32 | **0/500** | 787 |

V2's claim cost is flat in N; V1 pays its linear worst case and still
returns nothing once N ≥ K. V2's own worst case is the promotion burst
(N gates expiring in the same instant): 1.2 ms (N = 50) to 3.6 ms per
claim, bounded by `promote_batch` = 256, independent of N — the same
order as V1's K = 1000 worst case, paid only on bursts.

### Starvation, priority, crash, heartbeat

| Benchmark | Result |
|---|---|
| finite priority | high ×5 then low ×3, all claimed ✅ |
| rate-limit skip | gated `hot` skipped, `cold` claimed ✅ |
| replenish | interval 0: B 0/300 (strict priority, by design, identical to V1); interval 0.05 s: B 10/10 ✅ |
| scan window (corrected, 260 replenished fillers, 1 s interval) | **V2 victim 5/5 claimed, max wait 1.02 s; V1 victim 0/800 (starved)** ✅ |
| retries | A 15 attempts (3 × 5), B 10/10 ✅ |
| multi-worker 1/2/4/8 | 220 claims, 0 duplicates each ✅ |
| recovery (repeatedly abandoned A) | B 5/5 ✅ |
| cross-queue (4 busy http workers + 1 browser task, one domain, 0.1 s) | first run, before the fix: browser never claimed in 300 gate openings ❌ → turn-taking (§8) → claimed after 1 opening (0.11 s) ✅ |
| priority × 2 s interval | urgent, normal, bulk at t=0; urgent, normal at t=2.0 s; min same-domain gap 2.005 s on one queue and across http+browser ✅ |
| crash recovery | attempt 2 reclaimed 2.53 s after claim (lease 2 s + sweep + 0.5 s backoff); dead worker's heartbeat → None, complete → stale; bulk: 50/50 killed holders reclaimed and completed within 2.5 s ✅ |
| heartbeat endurance | enabled: 0/200 recovered, 200/200 completed; disabled: 200/200 recovered, 200 stale ✅ |

## 23. Known limitations

- **Absolute throughput depends on the Redis deployment.** ≥ 13k claims/s
  holds on a V1-equivalent Redis; the compose Redis (AOF, 1 CPU, Docker
  proxy) gives 9.3k for V2 (V1: 8.4k). Real crawling needs far less (V1
  audit: 10 ms of work per claim drops Redis to ~6 % CPU).
- **Redis is the only frontier durability.** AOF `everysec` can lose ~1 s
  of transitions on a Redis crash; lost tasks are re-admitted from Scylla
  by P6/P7, which P3 does not implement.
- **One Redis primary** (ADR-006): scripts compute keys at run time and are
  not Redis Cluster compatible; HA is P14.
- **Promotion lag**: due gates/scheduled tasks are promoted lazily by
  claims (≤ `promote_batch` per call) and by `recover()`; a burst larger
  than the batch waits one more call.
- **Strict priority** can starve lower priorities when higher-priority
  work is unbounded *and* no politeness interval applies (V1 mechanism J,
  kept by design; any interval > 0 resolves it, measured).
- **Turn-taking** can delay the claiming queue by one interval on a
  domain that also has work in a queue with no running workers.
- **Scheduled work counts against `max_depth`**; long-horizon recrawl
  calendars must live in Scylla (P7).
- `audit()` is O(state) with SCAN — offline/test use only.
- P2's 10× storage-latency gate remains **open for a development
  environment reason, not a P3 one**: the Ubuntu environment runs from an
  external 5 400-rpm USB HDD; the Windows NVMe is intentionally not
  modified; the P2 benchmark will be re-run under WSL/NVMe or another
  high-IOPS environment.

## 24. Deferred decisions

| Decision | Owner |
|---|---|
| Admission policy: what to admit/recrawl and when, re-admission after `rejected_full`, rebuilding the frontier from Scylla after Redis loss | P6/P7 |
| Retry policy per domain/fetch profile (`max_attempts`, backoff, escalation queue), re-admitting dead letters | P7 (mechanism exists in P3) |
| Per-domain politeness values beyond the default and overrides | P7 source intelligence |
| Whether `selenium` stays an execution queue and whether P1 requests may name it (P1 minor change) | P4 (D14) |
| Worker runtime: where `recover()` runs, heartbeat cadence, transport-level retry budget inside one attempt | P4 |
| Redis HA / failover preserving Lua atomicity | P14 |
| Absolute throughput on production Redis hardware | P14 |

## 25. Exit-gate status

| Gate | Status | Evidence |
|---|---|---|
| execution queues work | ✅ | §20 rows 1, 13–14; `test_queues_are_independent` |
| shared cross-queue politeness | ✅ | §20 row 5; priority × rate limit on two queues (§22) |
| temporary dedup | ✅ | §20 row 3; state machine |
| scheduled ZSET | ✅ | §20 rows 11–12; sweeper kills in 1M run |
| lease / heartbeat / recovery | ✅ | §20 rows 6–9; crash + heartbeat benchmarks |
| retry authority centralised | ✅ | §14, §20 row 10; 5 091 retries in 1M run |
| backpressure / admission control | ✅ | §20 rows 13–14 |
| Redis failure semantics defined and tested | ✅ | §19; `test_redis_failure.py` |
| eligible-domain design benchmarked and decided | ✅ | §17, §22 (index kept; `domain_scan_limit` not ported) |
| starvation benchmark | ✅ | §22 (after the turn-taking fix) |
| 1M distributed run, random kills, 0 lost, 0 simultaneous ownership, reclaims distinguished | ✅ | §22 |
| ≥ V1 ceiling (~13k claims/s, 8 workers, no rate limit) | ✅ on V1's measurement setup (13.2–13.7k; V1 11.3–11.5k on the same Redis); on the compose Redis 9.3k (V1 8.4k) | §22 |
| crash recovery, heartbeat endurance, state machine, priority/rate-limit | ✅ | §20, §22 |
| documentation, ADRs, limitations, P2 environment note | ✅ | this document, ADR-015/016, architecture/, development.md, benchmarks.md |

**P3 is complete.** The one qualification: the absolute ≥13k figure is
met on a Redis configured like V1's measurement setup, not on the
compose Redis (AOF on, 1-CPU cap), where V1 is slower as well.
