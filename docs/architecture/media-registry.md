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

```bash
crawler2-media probe run      # any number of hosts; per-media leases
crawler2-media hash run       # any number of hosts; per-media leases, per-host slots
crawler2-media show <media id | locator URL>
crawler2-media rebuild <media id | locator URL> --since 2026-09-01T00:00:00+00:00
```

**Wants (Redis, never facts).** After each committed observation the
resolver wants a probe when the media has none or its latest probe is older
than `reprobe_after_s` (7 d): `SET NX queued:probe:<media>` then `LPUSH
q:probe`, so a locator on 227 pages is queued once. A probe that shows a
hashable media wants a hash (`q:hash`, with the probe id). A lost queue only
loses wants; the next sighting or re-probe wants the media again.

**Probe (level 2).** P4's `HttpFetcher` with `Expectation.MEDIA_PROBE`,
unchanged: `Range: bytes=0-65535`, at most 64 KiB read. No proxy (a proxy
would bypass the transport). Result → `ProbeStatus`:

| P4 result | `probe_status` | `fetch_detail` |
|---|---|---|
| media response (`Outcome.MEDIA`) | `probed` | — |
| manifest body (`body_kind = manifest`) | `probed` | `manifest` (hints only, never hashed, P8-D6) |
| SSRF guard refused any hop | `forbidden` | `unsafe_destination` |
| HTTP 401/403, block page, captcha | `forbidden` | `http_<status>` |
| other HTTP error, timeout, DNS, TLS, network, redirect error | `unreachable` | `http_<status>` or outcome |
| any other 2xx body (HTML, …) | `not_media` | outcome |

**Hash (level 3, `full-bytes/v1`).** `crawler2/media/hashing.py`: one
streamed GET with `Accept-Encoding: identity` and the observation page as
`Referer`; redirects by hand (≤ `workers.fetch.max_redirects`), each hop
through the guard; SHA-256 over `aiter_raw()` (transfer framing removed,
no content decoding); bytes discarded. Outcomes:

| Outcome | When | ContentId / encode |
|---|---|---|
| `ok` | 200, identity encoding, validators match the probe before and after (HEAD) the read, all declared bytes read | yes |
| `too_large` | declared or streamed bytes > `hash_max_bytes` | no |
| `timeout` | `hash_timeout_s` total, or `hash_read_timeout_s` without data | no |
| `unreachable` | network/DNS, 404/410, redirect loop, short read | no |
| `forbidden` | 401/403, or the SSRF guard (`detail = unsafe_destination…`) | no |
| `changed_during_read` | validators differ from the probe, or a HEAD after the read shows other validators | no |
| `http_error` | any other non-200 (incl. 206 to a plain GET) | no |
| `content_encoded` | the server applied a Content-Encoding anyway | no |

**Which media are hashed:** `video_file`, `audio_file`, and `unknown` whose
probe shows a `video/*`/`audio/*` type or a sniffed container; never
manifests (P8-D6). **Re-hash** when the probe's validators (total length,
ETag, Last-Modified) differ from the last hash attempt's, or — without any
validator, or after a failed attempt — when the last attempt is older than
`rehash_after_s` (30 d).

**Freshness on observations (design §6.2).** An observation carries a
`ContentKey` only if the latest ok hash's validators equal the latest
probe's. A re-probe that shows a change hides the old key until the re-hash.
Not detected: a content change with unchanged validators inside
`rehash_after_s` (stated limitation).

**SSRF.** `crawler2/media/ssrf.py`, ported from the fingerprinter's guard:
loopback, private, link-local, unspecified, multicast, reserved,
100.64.0.0/10 (IPv4-mapped IPv6 included) are refused, as is any non-http(s)
scheme; one unsafe DNS record refuses the host. It is an httpx transport,
so every request — every redirect hop — is checked on both paths before any
byte is sent. `media.ssrf_allowed_networks` (default empty) is the only
opt-out, for fixtures and the compose test network. **DNS rebinding is not
solved** (the guard and the connection resolve separately); pinning is P14.

**Politeness and concurrency.** Media hosts are third parties, outside P3's
per-domain gate, so P8 has its own limits on Redis `TIME`, keyed by the
locator's host (redirect targets are not separately limited):

| Setting | Default | Meaning |
|---|---|---|
| `probe_concurrency` | 8 | probe loops per process |
| `probe_per_host_interval_s` | 2 | one probe start per host per interval, across all hosts |
| `probe_lease_s` | 300 | per-media probe lease |
| `hash_concurrency` | 2 | hash loops per process |
| `hash_per_host_concurrency` | 1 | concurrent full transfers per host, across all hosts (a busy host's job is put back) |
| `hash_max_bytes` | 8 GiB | size bound (assumption, not performance-validated) |
| `hash_timeout_s` / `hash_read_timeout_s` | 3,600 / 60 | whole read / inactivity |
| `rehash_after_s` | 30 d | re-hash without validators or after a failure |
| `queue_block_s` | 2 | queue pop wait |
| `ssrf_allowed_networks` | `[]` | CIDRs exempt from the guard (tests only) |

**Bandwidth.** Each media content version is transferred in full once by
the hash reader (plus the fingerprinter's own acquisition of one source per
`ContentId`, P9). At the defaults one process moves at most 2 transfers at
a time and one per host. Not measured on real media; the 8 GiB / 1 h bounds
are approved assumptions.

**Facts.** `media_probes` and `media_hashes` (per media and month, no TTL)
hold every attempt with worker, final URL, hop count, HTTP status and time.
`media_probe_latest` is derived (latest-wins). Every `ok` hash also writes
M5 `media_by_content` and M6 `media_content_versions` with P2's own
statements and timestamp rules (earliest-wins `first_seen`, latest-wins
`last_seen`), so the hash path and the observation path commute.
`crawler2-media rebuild` re-derives M1/M4/M5/M6 from M3 → M2 (P2) and the
latest view and hash-side M5/M6 from the probe/hash rows. Like every
convergent derived row, rebuild repairs *lost* derived writes; it cannot
override a manual CQL `DELETE` (a newer tombstone).

**Metrics:** `media_probes_total{status}`, `media_hashes_total{outcome}`,
`media_hash_bytes_total`, `media_hash_seconds`,
`media_ssrf_rejections_total{path}`, `media_work_skipped_total{stage,reason}`
(leased, fresh, not_due, host_busy), `media_wants_total{stage,result}`.
**Logs:** `media_probed`, `media_hashed`, `media_queue_unavailable`,
`media_job_retry`, `media_job_failed`.

| Symptom | Meaning / action |
|---|---|
| `media_ssrf_rejections_total` rising | locators or redirects point at internal addresses; recorded as `forbidden`/`unsafe_destination`, nothing fetched |
| many `host_busy` skips | one host has many media; raise `hash_per_host_concurrency` only with that host's limits in mind |
| `media_hashes_total{outcome="too_large"}` | media above `hash_max_bytes`: no ContentId, no encode; raise the bound deliberately |
| `content_encoded` | the host compresses media despite `identity`; no ContentId (by design) |

## Encode requester and representation projection (increment 3)

```bash
crawler2-media hash run               # issues encode.requested after every ok hash
crawler2-media encode sweep           # loop; re-emits stalled claims (or --once)
crawler2-media representations run    # representation.ready / encode.failed → projection
```

**One logical request per (ContentId, spec)** (`crawler2/media/encode.py`).
After an `ok` hash:

1. if `representation_status` shows the `default` row `requested`/`ready`,
   or any spec `ready`, nothing is emitted (see the interim rule below);
2. the `event_id` is allocated, the envelope built, the sweeper's index row
   written (`encode_claims_by_day`, TTL 14 d), then `encode_claims` is
   claimed with `INSERT … IF NOT EXISTS` (partition = ContentId) storing
   the **complete envelope**; a loser emits nothing;
3. the winner appends the **stored** envelope to the outbox (pattern B),
   marks the P1 projection `requested` and sets `emitted_at IF emitted_at =
   null`.

`source` is the hashed media, `referer` its observation page,
`required_spec` absent (spec key `default`). Byte-identical media on many
URLs therefore produce one claim and one `event_id`; the others are
`suppressed` or `claimed_elsewhere` (`media_encode_claims_total{result}`).

**Crash recovery.** A claim without `emitted_at` older than
`claim_emit_grace_s` (300 s) is re-emitted by `encode sweep` (scans the
last `sweep_lookback_days` = 2 days of the index, every `sweep_interval_s`
= 60 s) with the **stored** envelope: a redelivery of the same event, never
a second claim. A crash between an ok hash and its claim is recovered when
the media is next observed: the resolver's sink calls `ensure`, which
requests a current content key that has no claim (one EC projection read
first; a SERIAL claim read only if needed).

**Interim suppression rule (P9 handoff).** `representation.ready` always
carries a concrete spec and lands on that spec's row, never on `default`
(the row `encode.requested` marks). Which concrete spec is the default is
P9's to define (design §12). Until then any ready representation
suppresses a request. The claim guarantees one logical request regardless.

**No re-request after `encode.failed`** (P8-D7): the failure is recorded in
the projection; a later request for the same content finds the claim and
emits nothing. The idempotency-key conflict of re-requests is P9's.

**Representation projection.** `representations run` reads the
fingerprinter's streams `representation.ready.v1` and `encode.failed.v1`
with the consumer group `media.consumer_group` (`media-registry`), applies
them with P2's idempotent `apply_representation_ready` /
`apply_encode_failed` behind `IdempotentConsumer` (`processed_events`), and
acknowledges. Invalid entries are logged and acknowledged; storage failures
leave entries pending (reclaimed after `claim_idle_ms`). These events are
never in crawler2's outbox, so the crawler2 relay never carries them and
this group cannot hold back any crawl stream. Whether the fingerprinter's
own publisher applies ADR-022 to this group is P9's design.

| Setting | Default | Meaning |
|---|---|---|
| `claim_emit_grace_s` | 300 | age before the sweeper re-emits an unemitted claim |
| `sweep_interval_s` / `sweep_lookback_days` | 60 / 2 | sweeper cadence and index window |
| `consumer_group` | `media-registry` | group on the fingerprinter streams |
| `batch_size` / `block_ms` / `claim_idle_ms` | 50 / 2000 / 60000 | stream reading |

**Metrics:** `media_encode_claims_total{result}` (emitted, suppressed,
claimed_elsewhere, reemitted), `consumer_events_total{consumer,outcome}`.
**Logs:** `media_encode_request`, `media_encode_reemitted` (warning),
`media_encode_sweep_retry`, `representation_event_invalid`,
`representation_storage_unavailable`.

| Symptom | Meaning / action |
|---|---|
| `media_encode_claims_total{result="reemitted"}` > 0 | a requester died between claim and outbox write; the sweeper repaired it. Frequent: look for crashing hash workers |
| a ContentId never requested | check `crawler2-media show <locator>`: no ok hash, a stale hash (validators changed), or suppressed by a ready representation |

## Storage (V005, P8-owned)

| Table | Kind | Purpose |
|---|---|---|
| `media_reader_state` | LWT state | epoch, lease, checkpoint, gap state per reader |
| `media_reader_gaps` | authoritative | detected and accepted gaps (never expire) |
| `media_observation_claims` | LWT, write-once | decided `media.observed` envelope per (page observation, media), `committed_at` |
| `media_probes` | authoritative | probe facts per media and month |
| `media_hashes` | authoritative | full-content hash attempts per media and month |
| `media_probe_latest` | derived | latest probe / hash / ok hash per media |
| `encode_claims` | LWT, write-once | one logical `encode.requested` per (content, spec), with the stored envelope and `emitted_at` |
| `encode_claims_by_day` | index, TTL 14 d | sweeper index (written before the claim) |
| (existing, P2) `representation_status` | projection | `requested` / `ready` / `failed` per (content, spec key) |

Apply with `crawler2-storage migrate` (additive; existing tables unchanged).
