# Media registry & identity (current state)

P8 separates URL identity from content identity (V1 defect D11). It owns
identity levels 1–3 only; levels 4–5 are the fingerprinter's (P9/P10).
Design, decisions and gate evidence:
[P8 documents](../phases/p08-media-registry/) ([design](../phases/p08-media-registry/design.md),
[decisions](../phases/p08-media-registry/decisions.md),
[validation](../phases/p08-media-registry/validation.md)). Package:
`crawler2/media`. CLI: `crawler2-media`.

| Level | Key | Asserts | P8 part |
|---|---|---|---|
| 1 | `MediaId.of_locator(canonical URL)` (query kept, P8-D2) | same address | resolver |
| 2 | probe row: length, ETag, Last-Modified, type, container | "probably the same file" (hint only) | probe pool |
| 3 | `ContentKey("full-bytes/v1", sha256(all bytes))` → `ContentId` | same exact bytes | hash reader |

## Service boundaries

- **Inputs:** `media.discovered` (P5), read from the crawler2 **outbox**,
  never from a Redis consumer group ([ADR-024](../adr/ADR-024-authoritative-outbox-readers.md)).
  `representation.ready` / `encode.failed` (fingerprinter streams) for the
  `representation_status` projection.
- **Outputs:** `MediaObservation` rows M2/M3 + `media.observed` (through P2's
  unchanged `record_observation`), the P8-owned tables of migration V005,
  M5/M6 rows from hash facts (P2 write rules), `encode.requested`.
- **Never:** media bytes stored, targets, perceptual matching, equivalence
  records, the fingerprinter keyspace (ADR-011, B.5 #2).
- **The crawl never depends on P8 (B.5 #1).** No group exists on
  `media.discovered`, so under ADR-022 nothing the registry does (stopped,
  slow, failing) can hold back the relay or any crawl stream. Tested on the
  real stack (validation.md, I1-12).

## Resolver (increment 1)

`crawler2-media resolve run` (`crawler2/media/resolver.py`):

1. Reads whole outbox minute buckets in order, only once settled
   (`events.relay_settle_s`, 10 min), all 32 shards, through P2's read-only
   `OutboxStore` (`rows`, `earliest_bucket`).
2. Per `media.discovered` row: re-reads its lease (LOCAL_SERIAL) and renews
   it past half its life, then per distinct `MediaId` of the row:
   - builds the observation from the event (page, page version,
     `observed_at` = event `occurred_at`) and the latest probe/hash view, wraps it
     in a new `media.observed` envelope (causation = the discovered event);
   - **claims** it: `INSERT … IF NOT EXISTS` into `media_observation_claims`
     (partition = page observation) storing the complete envelope. A loser
     continues with the **stored** envelope;
   - if not committed: P2 `record_observation(stored observation, event=stored)`
     (M2 + M3 + outbox row in one logged batch), then
     `UPDATE … SET committed_at IF committed_at = null`;
   - asks the probe scheduler whether the media wants a probe.
3. Advances `next_bucket` b → b+1 with `IF epoch = E AND next_bucket = b AND
   state = 'running'`, only after every reference of b was committed.

The stored envelope must re-encode to its stored bytes exactly, or the
reader raises `IntegrityError` (copies are byte-identical by construction).

### Reader state, leases and epochs

One row in `media_reader_state` per reader name (`media.reader`, default
`resolver`): `epoch, owner, lease_until, next_bucket, state, gap_from, gap_to`.

| Transition | CAS condition |
|---|---|
| bootstrap (no row) | `IF NOT EXISTS`; starts at the oldest retained bucket |
| acquire | `IF epoch = E AND lease_until <= now` → `epoch = E + 1` |
| renew | `IF epoch = E AND owner = me` |
| advance | `IF epoch = E AND next_bucket = b AND state = 'running'` |
| enter gap | `IF epoch = E AND next_bucket = b AND state = 'running'` |
| accept-gap (operator) | `IF epoch = E AND state = 'gap' AND next_bucket = gap_from` → `epoch = E + 1` |

`now` is Redis `TIME`. Run `resolve run` on several hosts: one holds the
lease, the others stand by and take over `media.lease_ttl_s` (60 s) after
the owner stops renewing. Handover was tested on two hosts (C-11).

### Guarantees and limits (ADR-024)

- One decision, one authoritative observation (M2/M3) and **one logical
  event (`event_id`)** per (page observation, media).
- **At-least-once delivery.** A crash after `record_observation` and before
  `mark_committed` is recovered by writing the stored envelope again.
  Re-commits in the same minute upsert the same outbox row; each re-commit
  in a later minute adds another physical row with the same `event_id` and
  identical bytes. Repeated crashes can therefore create an **unbounded**
  number of physical rows (measured: k = 1, 3, 5 within one minute → 1 row;
  three minutes → 3 rows; validation.md I1-6).
- **Downstream duplicates remain possible:** `processed_events` is a
  non-atomic check-then-insert (ADR-013); P8 does not change it.
- **No strict lease fencing of single fact writes:** a stale owner can still
  win a claim before its next lease check; that claim is valid and every
  other reader commits the same stored envelope. Only reader-state and
  checkpoint transitions are fenced by the epoch.
- The checkpoint never passes uncommitted work.

### Gaps (halt, never skip)

If `next_bucket` is older than the oldest retained bucket
(`max(outbox creation − 1 h, now − 14 d + 1 h)`), the reader records the
range in `media_reader_gaps`, sets `state = gap` and **stops**. The process
stays up (metrics) and every later cycle, on any host, stays halted.
`resolve status` exits **2**. Nothing moves until an operator runs:

```bash
crawler2-media resolve status                       # level, lag, lease, gaps
crawler2-media resolve accept-gap --note "outage 2026-…, loss accepted by …" [--by NAME]
```

`accept-gap` writes the accepted range first, then moves the checkpoint to
the oldest retained bucket and bumps the epoch. Lost references are not
recovered (no backfill in v1; design §4.6, F-2).

### Outages

| Failure | Behaviour |
|---|---|
| Scylla unavailable / timeouts | `StorageUnavailableError`; the cycle aborts without advancing; `run_forever` retries with backoff up to `media.retry_max_s`; tested with a frozen Scylla container (I1-9) |
| Redis unavailable | the clock (`TIME`) and probe wants fail as `StorageUnavailableError`; same retry; no fact is lost (wants are re-derived) |
| resolver stopped / slow | crawl unaffected; lag alarms at 3 d (`warning`) and 7 d (`critical`, status exit 1) |
| malformed `media.discovered` row | skipped, counted (`media_observations_total{result="invalid"}`), logged |

### Resolver configuration (`CRAWLER2_MEDIA__*`)

| Setting | Default | Meaning |
|---|---|---|
| `reader` | `resolver` | reader-state row name |
| `lease_ttl_s` | 60 | resolver lease |
| `poll_interval_s` / `retry_max_s` | 15 / 300 | idle poll, longest retry pause |
| `max_buckets_per_cycle` | 60 | buckets per cycle |
| `outbox_retention_s` / `retention_margin_s` | 14 d / 1 h | must match the outbox TTL |
| `lag_warn_s` / `lag_critical_s` | 3 d / 7 d | status levels; critical must be < retention |
| `reprobe_after_s` | 7 d | probe wanted again after this |

### Metrics and logs

Metrics (prefix `crawler2_`): `media_reader_lag_seconds`,
`media_reader_checkpoint_bucket`, `media_reader_epoch`,
`media_reader_owner`, `media_reader_state{state}`, `media_reader_gap_total`,
`media_reader_conflicts_total`, `media_reader_buckets_total`,
`media_reader_cycle_failures_total`, `media_observations_total{result}`
(claimed, claim_lost, committed, replayed, invalid), `media_wants_total{stage,result}`.

Logs: `media_reader_bootstrap`, `media_reader_acquired`,
`media_reader_lease_lost`, `media_reader_advance_conflict`,
`media_reader_gap` (error), `media_reader_gap_accepted`,
`media_reader_retry`, `media_reader_cycle`, `media_discovered_invalid`.

### Troubleshooting

| Symptom | Meaning / action |
|---|---|
| `resolve status` exit 2, `media_reader_state{state="gap"}` = 1 | outbox retention gap; decide on the loss, then `accept-gap --note …` |
| status exit 1 (critical) | lag > 7 d: the resolver is not running or cannot keep up; the gap is near |
| `media_reader_conflicts_total` rising | two resolvers contend or a lease expired mid-cycle; harmless, check host clocks are not used (Redis `TIME` only) and `lease_ttl_s` |
| `media_reader_cycle_failures_total` rising | Scylla/Redis unavailable; the reader retries and skips nothing |
| `IntegrityError` in `media_reader_retry` | a stored claim does not round-trip; the reader stops at that bucket. Investigate the row; never delete claims |

## Probe pool and hash reader (increment 2)

Not implemented yet.

## Encode requester and representation projection (increment 3)

Not implemented yet.

## Storage (V005, P8-owned)

| Table | Kind | Purpose |
|---|---|---|
| `media_reader_state` | LWT state | epoch, lease, checkpoint, gap state per reader |
| `media_reader_gaps` | authoritative | detected and accepted gaps (never expire) |
| `media_observation_claims` | LWT, write-once | decided `media.observed` envelope per (page observation, media), `committed_at` |
| `media_probes` | authoritative | probe facts per media and month (increment 2) |
| `media_hashes` | authoritative | full-content hash attempts per media and month (increment 2) |
| `media_probe_latest` | derived | latest probe / hash / ok hash per media (increment 2) |
| `encode_claims` | LWT, write-once | one logical `encode.requested` per (content, spec) (increment 3) |
| `encode_claims_by_day` | index, TTL 14 d | sweeper index (increment 3) |

Apply with `crawler2-storage migrate` (additive; existing tables unchanged).
