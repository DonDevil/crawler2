# P3 audit — V1 frontier semantics kept, changed, dropped

Inputs read (V1 at `~/anti_piracy/crawler`, unmodified):
`core/redis_frontier.py` (all six Lua scripts), `core/claim_heartbeat.py`,
`docs/architecture/frontier-adr.md`,
`history/{domain-scan-window-design,domain-scan-limit-decision,domain-starvation-audit,throughput-ceiling-audit}.md`,
`tests/redis_frontier_test.py`, `tests/frontier_redis_failure_semantics_test.py`,
`tests/benchmarks/{distributed_benchmark,crash_recovery,heartbeat_endurance,priority_ratelimit,domain_starvation,common}.py`,
`docs/benchmarks.md`. V2 inputs: P1 `base.py` (`Priority`), `ids.py`,
`models/web.py` (`UrlRef`, `FetchCapability`), `events/web.py`
(`CrawlRequested`), `events/catalog.py`; P2 `storage/repositories.py`
(`UrlRepository`, `KnownUrl`), `core/configuration/settings.py`,
ADR-001/-006/-013, the compose Redis configuration.

## What V1 got right (ported as-is in spirit)

| Mechanic | V1 location | Kept because |
|---|---|---|
| Every state transition is one Lua script, one round trip, Redis `TIME` as clock | `_init_lua_scripts` | atomic under N processes; measured 0 duplicate claims at 1–16 workers |
| Claim token (uuid4) as the only proof of ownership; completion/renewal are token-CAS, stale calls are no-ops | `complete_claim`, `renew_claim` | prevents a reclaimed worker from completing a newer claim |
| Lease = `inflight` ZSET scored by expiry; recovery sweep `ZRANGEBYSCORE -inf now LIMIT batch` | `reclaim_and_promote` | O(batch), independent of URL/domain count |
| Frontier-level attempt counter; `attempt < max_retries` → exponential backoff `min(base·2^(n−1), max)`, else terminal | `complete_claim`, `reclaim` | one retry authority (D4) |
| Lease expiry is treated exactly like an explicit failure | `reclaim_and_promote` (a) | crashed workers consume one attempt |
| `mark_deferred`: requeue with fixed delay, attempt budget net zero | `mark_deferred` | local-network outage ≠ target failure (N1–N7) |
| Renewal/heartbeat at `lease_ttl/3`; `None` = claim lost, error = Redis unavailable | `claim_heartbeat.py` | decouples crash detection from fetch duration |
| Rate-gated domain is skipped, never blocks a lower-priority eligible domain | `claim_next` | measured (starvation audit §4.2) |
| Strict global `(priority, seq)` order across domains | `domain_heads` | the documented policy (starvation audit §6) |
| Redis errors raise `FrontierUnavailableError`; never "empty"/"None" | failure-semantics step | fail closed |

## A. "visited = forever" (D10)

Every place V1 makes a URL permanently terminal:

| # | V1 construct | Effect |
|---|---|---|
| A1 | `urls:known` SET: `add_url` returns 0 if member; members are **never removed** | known ≡ queued ≡ in-flight ≡ retrying ≡ visited ≡ skipped ≡ failed for dedup; a URL can be crawled once per Redis lifetime |
| A2 | `urls:visited` SET, permanent | success is terminal |
| A3 | `urls:skipped` SET, permanent | blacklist/robots skip is terminal |
| A4 | `urls:failed_permanent` SET, permanent (also from lease reclaim) | exhausted retries are terminal forever |
| A5 | `has_pending`/`get_status_counts` derive "queued" as `known − visited − skipped − failed − inflight − retry` | the accounting *requires* the permanent sets |
| A6 | `terminal_meta_ttl_seconds` only TTLs metadata; membership stays | no path back from terminal |

**This is D10.** It is why V1 cannot recrawl (`/movies/` changes between
S1 and S2 are never seen). V2 removes all six:

- The frontier stores **only active tasks**. The dedup key is the task
  record `fr:task:{url_id}`, which exists exactly while the URL is
  scheduled, ready, leased or waiting for a retry, and is deleted by the
  transition that ends the task (complete, terminal failure). There are no
  visited/skipped/failed sets.
- "Seen historically" lives in Scylla (`UrlRepository.record_discovered`,
  `KnownUrl.first_seen/last_observed_at`, fetch attempts, observations —
  P2). Whether a URL *should* be crawled again is admission policy
  (P6 default, P7 recrawl), decided from Scylla, not from Redis.
- A URL is admittable again the moment its task ends.
- Status counts come from structure cardinalities and per-queue depth
  counters, not from set differences over permanent sets.
- The only terminal record Redis keeps is the **dead-letter** entry for a
  task whose attempts were exhausted by *lease recovery* (no live worker
  exists to report it). It has a TTL, is capped, and does **not** block
  re-admission.

## B. Single-queue assumptions

| # | V1 construct | Consequence |
|---|---|---|
| B1 | one `domain:{d}:queue` per domain, one `domain_heads` index | any worker can take any URL; no way to route JS pages to browsers |
| B2 | one global `rate_limit` float, `next_time` STRING per domain | politeness is per domain (good) but not configurable per domain |
| B3 | `get_next_url()` takes no argument | a worker cannot ask for "work I can do" |
| B4 | `has_pending`/`pending_count` are global | no per-capability depth, no backpressure signal |
| B5 | no admission bound at all | `add_url` grows Redis without limit (only `maxmemory` stops it, as an error) |

V2: one ready index per **execution queue** (`http`, `browser`, `tor`,
`selenium`), per-(queue, domain) task queues, one **shared** domain gate,
per-queue depth limits. Queues come from a closed registry
(`ExecutionQueue`), never from input strings.

## C. Domain politeness (V1 Lua, audited)

`claim_next`: `GET domain:{d}:next_time`; eligible iff absent or
`≤ now` (strict `>` gate); on claim `SET next_time = now + rate_limit`
(float, not floored — the Revision-1 precision bug is fixed). Correct and
atomic because selection, gate check, pop and gate update are one script.
`rate_limit = 0` is not special-cased and behaves as "no gate".

V2 keeps the rule (gate = last claim time + interval, checked atomically
inside the claim script) but makes the gate **one per domain, shared by all
queues**: a claim on `http` closes the domain for `browser`, `tor` and
`selenium` too. The gate lives in one ZSET (`fr:gate`, score = next allowed
time) instead of a STRING per domain, which is what makes the
eligible-domain index possible (E).

## D. Future scheduled work

V1 has `retry_scheduled` (a due-time ZSET) but only for backoff; `add_url`
has no `not_before`. P1 `CrawlRequested.not_before` needs it. V2 generalises
`retry_scheduled` into `fr:scheduled`: backoff retries, deferrals and
future-dated admissions are all "task becomes ready at due time".

## E. `domain_scan_limit` and D13

V1 `claim_next` examines only `ZRANGE domain_heads 0 K−1` (K = 250 since
Step 8B). Measured in V1: a domain ranked outside the top K is **never
examined** while K better-ranked domains stay non-empty (0/400 claims at
K=10, 0/600 at K=250 with 260 replenished domains), even when all K are
rate-gated — so `claim_next` returns nothing while eligible work exists.
Worst-case cost is linear in K (≈2.7 µs/candidate, 0.67 ms at K=250).
V1 deferred the fix (the eligible-domain index, Step 8A, prototyped and
validated) and listed triggers to revisit it, one of which is
"multiple concurrent crawler systems sharing one Redis frontier".

V2 hits that trigger by construction, and the shared gate makes it worse:
with four queues sharing one gate, a domain gated by an `http` claim
occupies a slot in the top-K of *every* queue's head index that has work
for it. A K-scan over a shared gate would pay its worst case far more
often than V1 did. Decision method: implement the index, then measure it
against V1's own K-scan code on the same Redis
(`benchmarks/p3-frontier/`, see the phase document §17).

## F. Retry authority (D4)

V1 already centralises frontier retries, but three retry loops nest
(`fetch()` internal loop × frontier `max_retries` × hybrid engine
escalation, up to 9+ attempts, invisible to the frontier). V2 P3 provides
the only retry mechanism; P4 workers must not loop. Capability escalation
(the hybrid chain) becomes `fail(claim, next_queue=…)`, so every attempt,
whichever queue it ran on, consumes the one attempt budget.

## G. Backpressure

V1 has none (B5). Redis runs with `maxmemory 768mb`, `noeviction`, AOF on
(compose), so unbounded admission ends in write errors for *every* Redis
user, including event streams. V2 bounds admission per queue (§16 of the
phase document).

## Defects found in V1 while auditing (not fixed in V1 — reference only)

1. **Priority band overflow.** Score = `priority·1e6 + seq`, `seq` a global
   INCR. After 10⁶ admissions `seq` crosses into the next priority band, so
   old priority-10 work sorts after new priority-11 work. A 1M-claim run
   triggers it. V2 uses a 10¹³ band (exact in a double up to 9·10¹⁵).
2. **Lost task on missing claim hash.** `reclaim_and_promote` (a) removes
   an expired `inflight` member and does nothing else when `claim:{url}`
   is absent — the URL is neither queued nor terminal. V2 keeps all task
   state in one hash and treats an inconsistent lease as an audit error,
   never as a silent drop.
3. **No per-domain politeness configuration** (B2).
4. **V1 benchmark "duplicate" = completed by two processes.** It cannot
   distinguish a legitimate reclaim from simultaneous ownership; V2's
   distributed test defines both (phase document §21).

## P1/P2 facts that constrain P3

- `Priority` is `0..100`, **higher is served sooner**, default 50
  (V1: lower number first). V2 keeps P1's scale and direction.
- `UrlRef` carries `url`, `url_id`, `domain_id`; `DomainId` is derived
  from the canonical host. Redis keys use these IDs only.
- `CrawlRequested` (producer: intelligence, consumer: frontier; "the
  frontier decides admission"; "merges requests per url_id, highest
  priority wins") carries `priority`, `capability: FetchCapability | None`,
  `not_before`.
- **Contract gap (documented, not changed):** P1 `FetchCapability` is
  `{http, browser, tor_http, tor_browser}` — what a fetch *could do*. The
  P3 execution queues are `{http, browser, tor, selenium}` — execution
  classes. They are different concepts; the frontier maps capability →
  queue (`http→http`, `browser→browser`, `tor_http→tor`,
  `tor_browser→tor`). `selenium` has no P1 capability; it is reachable only
  by an explicit queue choice until P4 decides whether Selenium earns a
  place (D14). No P1 change is needed for P3; if P4 wants requests to name
  Selenium, that is a P1 minor-version change through ADR-009.
- ADR-006: Redis `TIME` is the distributed clock; one Redis primary;
  frontier scripts assume single-primary atomic Lua (no Cluster key
  spreading).
- ADR-013: Redis also carries event streams; frontier memory must be
  bounded so it cannot starve them.
