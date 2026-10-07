# ADR-023 — Rebuildable projections read the outbox, not a consumer group

Status: Accepted (2026-10-07, P7 increment 2 review). Adds a consumer
class. **ADR-022 is not changed**, and neither are the P2 publisher, the
relay, the stream configuration or any P1 contract.

## Context

P7 keeps a per-host source profile (P7 design §4), derived from every
`fetch.completed` fact. The draft (design §3, before this ADR) proposed a
Redis consumer group `intel-sources` on `fetch.completed`, with groups
named `intel-*` exempt from the ADR-022 trim floor.

What the code does today (`storage/events/publisher.py`, ADR-022):

- `trim_floor` and the byte-budget check treat **every** group on a stream
  the same way. A grouped stream whose unacknowledged entries fill
  `stream_max_bytes` refuses new entries. The relay then stops its whole
  cycle at that stream (`StreamBackpressureError`).
- `fetch.completed` has **no consumer group** in the runtime today. Only
  `page.observed` (extraction) and `urls.discovered` (discovery) are read
  through groups. So `fetch.completed` is trimmed by length and bytes
  (ADR-022 rule 3), and it can never hold the relay back.

An ordinary P7 group would make `fetch.completed` a grouped stream. If P7
fell behind or died, the relay would stall on `fetch.completed`. Because
the relay's cycle stops at the first blocked stream, `page.observed` and
`urls.discovered` would stall with it. That is the X-8/X-16 failure mode,
and it violates B.5 #1 ("the crawler runs with P7 stopped").

The draft's exemption (`intel-*` ignored by retention) would need a new
group class in the P2 publisher. A reader would then hold a group whose
entries can vanish under it. Its gap detection would depend on Redis entry
IDs, which a Redis flush resets, and it would still need a durable source
to rebuild from. That is two mechanisms where one is enough.

## Decision

1. **A rebuildable projection does not join a consumer group.** It reads
   the **outbox** (ADR-013), the 14-day system of record of every event,
   through P2's read-only `OutboxStore` interface (`rows(shard, bucket)`,
   `earliest_bucket()`). It never writes the outbox, its relay
   checkpoints or `published_at`.
2. **Its own checkpoint.** The reader processes whole minute buckets in
   order. It reads only buckets older than `events.relay_settle_s`, the
   same settle rule the relay uses. Its checkpoint (`next_bucket`) and
   generation are stored in a table it owns. The checkpoint advances with
   one conditional write (LWT) per bucket.
3. **Exactly-once fold per (row, bucket).** Every projection row records
   the last bucket folded into it (`through_bucket`). Re-reading a bucket
   after a crash skips rows already past it. Duplicate copies of a fact
   (relay re-publication does not apply; producer retries can create two
   outbox rows) are dropped by idempotency key within a bounded in-memory
   window.
4. **Gap = the checkpoint fell out of retention.** If `next_bucket` is
   older than what the outbox still retains (`now − 14 d + margin`, or the
   outbox's creation), facts were lost to the projection. The reader then
   starts a **new generation** from the oldest retained bucket. The same
   happens when the projection's policy version changes, and on an
   operator `rebuild`. Readers of the projection ignore rows of other
   generations and policies.
5. **Consumers of the projection treat it as optional.** While the
   checkpoint lags more than a configured bound (rebuild, outage,
   bootstrap), the projection gives no advice, and its users fall back to
   the behaviour they had without it.

The class covers projections whose rebuild cost is acceptable from 14
days of outbox. Groups remain the right tool for consumers that must
process every event and act on it (extraction, discovery).

## Semantics

| Question | Answer |
|---|---|
| Can events be dropped for the projection? | Only if the reader is down or behind longer than the outbox retention (14 d), and for rows enqueued into a bucket after the reader passed it (the relay's "late rows", `outbox_late_rows_total`) |
| Does a drop affect authoritative state? | No. Facts are in `fetch_attempts` (retained) and the outbox. The projection is derived |
| What detects a gap? | `next_bucket < retained_from` at the start of a cycle |
| How is it rebuilt? | New generation, fold every retained `fetch.completed` from the outbox, oldest bucket first. The code path is the normal one |
| While rebuilding? | The checkpoint lags, so advice is withheld. Admission uses its default |
| Effect on the event path | None. No group exists, so no trim floor and no backpressure. Redis is not touched |
| Scylla unavailable | The reader retries in-process with backoff. Admission keeps its last snapshot, then falls back |
| Reader permanently dead | The projection goes stale, advice is withheld, the crawl runs as without P7. After 14 d a restart detects the gap and rebuilds |

## Consequences

- **No change** to ADR-022, the publisher, the relay, stream settings or
  existing groups. Extraction and discovery are unaffected.
- Outbox reads grow by one reader: 32 partition reads per minute bucket.
  A full rebuild reads 14 d × 1,440 × 32 ≈ 645k partitions. That is an
  operator-scale operation, measured in P7 benchmarks.md.
- Freshness is bounded below by the settle window (10 min). Source health
  works on hours (probe interval ≥ 6 h), so this is not a constraint for
  P7. A projection that needs seconds would need a group, and with it this
  ADR's question again.
- The reader depends on P2's outbox layout through the `OutboxStore`
  protocol only. A transport change (ADR-004: Kafka) does not affect it.
  An outbox change must keep `rows`/`earliest_bucket`.
- Only **one** reader per projection may run. A second instance is
  detected by the failing checkpoint LWT (`intel_sources_conflicts_total`),
  and its effect (double folds) is removed by `rebuild`.

## Rollback

Stop the reader and disable advice (`intelligence.advice_enabled=false`,
the default). The projection tables are P7-owned and can be left in
place or dropped. Nothing else depends on them.

## Monitoring

`intel_sources_lag_seconds`, `intel_sources_checkpoint_bucket`,
`intel_sources_generation`, `intel_sources_gaps_total`,
`intel_sources_rebuilds_total`, `intel_sources_conflicts_total`,
`intel_sources_facts_total{result}`, `intel_sources_profile_writes_total`,
`intel_sources_cycle_failures_total`. On the admission side:
`intel_source_advice_total{result}` and `intel_source_snapshot_age_seconds`.
A stale projection is visible in these metrics. It never shows up as a
crawler failure.

## Tests required

Unit: fold idempotence on bucket replay, duplicates, out-of-order facts,
gap and policy-change rebuild, checkpoint conflict, malformed rows,
storage errors. Integration (real Scylla and Redis): the reader stopped
while the relay publishes and the main groups consume. Then
`fetch.completed` has no group and the relay is not held back, the reader
restarts and catches up, a forced gap rebuilds, and the result equals the
pure fold of the same facts.
