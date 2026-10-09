# ADR-024 — Authoritative idempotent consumers may read the outbox instead of joining a stream group

Status: Accepted (2026-10-09, P8 design review, decision P8-D3). Extends
the mechanism of ADR-023 to a consumer whose writes are authoritative.
**ADR-022 and ADR-023 are not changed**, and neither are the P2
publisher, the relay, the stream settings or any P1 contract.

## Context

The P8 media registry consumes `media.discovered` and writes
authoritative facts: the `MediaObservation` rows M2/M3 and their
`media.observed` outbox row (P8 design §4). Under ADR-022 a consumer
group on `media.discovered` would hold back publication of that stream
when the registry falls behind or stops, and the relay's cycle stops at
the first blocked stream, so a dead registry could stall the crawl loop
(the X-8/X-16 failure mode; B.5 #1).

ADR-023 solves this for **rebuildable projections**: they read the
outbox with their own checkpoint and restart from the oldest retained
bucket after a gap. Neither half fits an authoritative consumer:

- its facts cannot be re-derived from the outbox, so "new generation
  from the oldest retained bucket" would silently lose facts;
- a lease alone cannot fence a paused owner: Scylla cannot condition a
  write in one partition on a lease in another, so two readers can both
  decide one fact (P8 design §4.3).

## Decision

An **authoritative idempotent consumer** may read the outbox through
P2's read-only `OutboxStore` interface (`rows(shard, bucket)`,
`earliest_bucket()`), with its own checkpoint, instead of joining a
stream consumer group, when:

- it is off the crawl loop;
- its writes are deterministic and idempotent per fact;
- it can stop for days without affecting crawling.

Rules:

1. **Each fact is decided once by a write-once claim in the fact's own
   partition** (`INSERT … IF NOT EXISTS`, `LOCAL_SERIAL`), and the claim
   stores the **complete event envelope** to emit. The first claim wins
   linearizably. A reader that loses the claim uses the stored envelope.
2. **Every commit, by any reader and on every retry, writes the stored
   envelope**, never a newly constructed replacement. A conditional
   `committed_at` marker (`IF committed_at = null`) records the commit.
   A crash between the authoritative write and the marker is recovered
   by replaying the stored envelope.
3. **Reader state lives in one partition** (epoch, owner, lease,
   checkpoint, gap state). Acquire, renew, checkpoint advance and gap
   transitions are LWTs conditioned on the epoch. Lease times come from
   Redis `TIME` (B.4 #3).
4. **The checkpoint never advances beyond uncommitted work.** It moves
   from bucket `b` to `b + 1` only after every fact of `b` is claimed and
   committed, and only by the epoch's owner.
5. **A retention gap halts the reader.** If the checkpoint is older than
   the outbox's retained range, the reader enters an explicit `gap` state,
   stops, raises a metric and an alarm, and its status command exits
   non-zero. It never jumps to the oldest retained bucket on its own.
   Lag alarms fire well below the retention.
6. **Only an operator accepts a gap**, with an explicit `accept-gap`
   command that records the accepted range in a durable gap record before
   the checkpoint moves.
7. **No generations and no rebuild** for this reader: authoritative facts
   are never re-derived from the outbox.

## Stated limits (not guaranteed)

| Property | What holds |
|---|---|
| decisions | exactly one decision, one authoritative fact and one logical event (`event_id`) per fact key |
| delivery | **at-least-once.** Each re-commit after a crash appends another physical outbox row carrying the same `event_id` and the identical envelope. Repeated crashes can create an **unbounded** number of such rows |
| downstream duplicates | **still possible.** Consumers dedupe through `processed_events` with a non-atomic check-then-insert (ADR-013), so concurrent redeliveries of one `event_id` can be processed twice. This ADR does not fix that consumer-layer limitation; consumers stay idempotent (B.4 #5) |
| fencing | **no strict lease fencing of individual fact writes.** A stale owner can still win a claim before its next lease check. That claim is a valid single decision, and every other reader commits the same stored envelope. Only checkpoint and reader-state transitions are fenced (by the epoch) |
| recovery after a gap | not lossless; the operator accepts the loss explicitly. A backfill tool is a separate decision |

## Unchanged

ADR-022 (stream retention, relay retry), ADR-023 (rebuildable
projections), the P2 publisher and relay, stream settings, the outbox
schema and every P1 contract. The consumer writes its authoritative rows
through the existing P2 repository methods (P8: `record_observation`);
the claim and reader-state tables are additive and owned by the
consumer's phase.

## Consequences

- A stopped or slow consumer cannot hold back the relay, because no group
  exists on its input stream.
- Per fact: one Paxos round (claim), one logged batch (P2 write) and one
  conditional update (marker). Throughput is measured, not assumed.
- Outbox reads grow by one reader: 32 partition reads per minute bucket.
- Freshness is bounded below by the relay's settle window
  (`events.relay_settle_s`, 10 min).
- Facts written into a bucket after the reader passed it (the relay's
  "late rows") are missed exactly as for ADR-023; `settle_s` is sized so
  that this does not happen in practice, and `outbox_late_rows_total`
  shows it if it does.

## First user

The P8 resolver (`crawler2-media resolve`, tables `media_reader_state`,
`media_reader_gaps`, `media_observation_claims`; P8 design §4,
[docs/phases/p08-media-registry/](../phases/p08-media-registry/)). This
ADR is not a general framework: a further consumer needs its own design
review that shows it meets the conditions above.

## Monitoring

`media_reader_lag_seconds`, `media_reader_state{state}`,
`media_reader_gap_total`, `media_reader_checkpoint_bucket`,
`media_reader_conflicts_total`, `media_observations_total{result}`.

## Tests required

Real Scylla, deterministic injected time and named pause hooks (P8
design §10.1, C-1…C-11): a claim race between readers that construct
different envelopes; a stale owner's late claim and checkpoint attempt;
crash between claim and commit; repeated crash between commit and marker
(k ∈ {1, 3, 5}: one distinct `event_id`, byte-identical copies, physical
row count recorded and not bounded); storage failure; a retention gap
(halt, metric, non-zero status, `accept-gap`); a two-host lease
handover; the crawl's event path running with the consumer stopped.
