# ADR-019 — Frontier per-domain in-flight limit, global across queues and hosts

Status: Accepted (P3 correction found by P4, 2026-09-29). Extends ADR-015;
does not supersede it.

## Context

ADR-015 gave every domain one politeness gate shared by all queues: two
successive **claims** of a domain are at least its interval apart. That
bounds the *start rate*, not the number of requests a domain is serving
at once. P4's exit-gate run (P4 phase document §30) exposed the gap: with
a 0.3 s interval and a host answering in 3–15 s, V2's pipeline had many
requests to one host in flight at the same moment, and 36 pages that V2
fetches in ~3 s when unloaded timed out on every attempt. Politeness
towards a site is a hard safety constraint of the frontier, independent
of learned pacing (P7), and it must hold for every capability, worker and
host at once — which only the shared frontier can see.

## Decision

1. The frontier counts **leased tasks per domain** in one Redis hash
   (`inflight`), shared by every execution queue. There are no
   per-capability counters.
2. `claim` increments the count atomically with the lease. When the count
   reaches the domain's limit (`max_inflight_per_domain`, or a per-domain
   override in `ilimit`), the domain joins the `full` set and leaves every
   queue's ready index, exactly like a gated domain.
3. The slot is released exactly once per lease, inside the same scripts
   that end the lease: `complete`, `fail` (retry, queue move, exhausted),
   `defer`, and lease recovery (retry or dead letter). All four are
   token-checked, so stale or zombie reports release nothing. Releasing
   below the limit removes the domain from `full` and re-indexes it in
   every queue where it is otherwise eligible (not gated, not yielding).
4. A crashed worker's slot returns when its lease expires and `recover()`
   runs — the same bound as the task itself (`lease_ttl` + sweep).
5. `0` means unlimited (P3's original behaviour). The default is **2**,
   chosen by a bounded experiment on the P4 W691 workload (P3 phase
   document §26): limits 1 and 2 cut timeout attempts from 71 to 8; 2 did
   so in 80 s against 111 s for 1; 4 let timeouts rise to 21.
6. `audit()` checks that `inflight` equals the leased tasks per domain,
   that no domain at or over its limit is claimable, and that no
   saturated domain is idle or indexed.

## Consequences

- Politeness is now two constraints, both global: start rate (gate) and
  concurrency (in-flight limit). A slow site is never hit by more than the
  limit, whatever the number of pools, workers or hosts.
- Throughput on a single domain is bounded by `limit / response time`;
  across many domains the frontier cost is ~2 % of script time (A/B at
  8 workers: 11.9k vs 11.6k claims/s, within run spread; P3 §26).
- A limit lowered at run time applies as leases end; a raised one at the
  next claim or release.
- P7 may later set per-domain limits (`set_domain_inflight_limit`) from
  observed response times; the frontier enforces whatever value is set.
