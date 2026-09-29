# ADR-015 — Frontier execution model: execution queues, shared domain gate, eligible-domain index

Status: Accepted (P3, 2026-09-29).

## Context

V1's Redis frontier has one queue per domain, one cross-domain head index
(`domain_heads`) and a per-domain `next_time` gate; `claim_next` scans the
top `domain_scan_limit` (K) heads and skips gated ones. V2 needs work
routed to execution classes (plain HTTP, browser, Tor, Selenium — plan
P3/P4) whose worker pools scale independently, while politeness towards a
site must stay global: four pools must not each be allowed one request per
interval. V1 measured that the K-scan makes domains ranked below K
invisible (D13) and that its worst case is linear in K; V1 postponed the
eligible-domain index (Step 8A) until one of its triggers held, among them
several independent crawler systems sharing one frontier.

## Decision

1. **Execution queues** `http`, `browser`, `tor`, `selenium` from a closed
   enum (`ExecutionQueue`). Each queue has per-domain task queues
   `q:{queue}:{domain_id}` and an eligible-domain index `ready:{queue}`.
   Scripts receive the queue list as an argument, so adding a queue is a
   configuration/enum change. Key names are built from the enum,
   `UrlId` and `DomainId` only.
2. **One domain gate shared by every queue**: `gate` ZSET, domain →
   next allowed claim time, written by the claim script of *any* queue,
   which also removes the domain from *every* `ready:{queue}`. Interval =
   per-domain override (`interval` HASH) or the configured default.
3. **Eligible-domain index instead of `domain_scan_limit`**: a domain is in
   `ready:{queue}` iff it has ready work in that queue and no gate entry;
   gates are promoted lazily (bounded `promote_batch`) inside claims.
   Selection is `ZRANGE ready:{queue} 0 0` — strict `(priority, seq)`
   order over *all* eligible domains, O(log N).
4. **P1 capability → queue mapping** lives in the frontier
   (`http→http`, `browser→browser`, `tor_http→tor`, `tor_browser→tor`,
   none → `http`). `FetchCapability` (what a fetch can do) and
   `ExecutionQueue` (which pool executes it) stay distinct concepts; P1 is
   not changed. `selenium` has no capability until P4 decides D14; naming
   it in `CrawlRequested` would be a P1 minor change via ADR-009.

## Consequences

- Politeness holds across queues, workers and hosts by construction
  (one script decides and closes the gate).
- No visibility window: a claim returns work whenever any eligible domain
  has work in that queue (D13 closed). Measured against V1's K-scan in
  the P3 phase document §21–§22.
- Cross-queue order on one domain is work-conserving, not FIFO: when a
  gate opens, the first queue to claim wins. A queue with no workers never
  blocks other queues on a shared domain.
- Promotion lag: a gate that expires is noticed by the next claim; if more
  than `promote_batch` gates expire at once the rest wait one more claim.
- Scripts touch keys computed at run time: one Redis primary only
  (ADR-006), not Redis Cluster.
