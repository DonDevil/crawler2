# P8 Design — media registry & identity

Status: **APPROVED for implementation (operator, 2026-10-09).**
Implementation is in progress; no implementation or exit gate is passed
by the approval. Gate status: [validation.md](validation.md),
[exit.md](exit.md). The resolver mechanism is
[ADR-024](../../adr/ADR-024-authoritative-outbox-readers.md).
History: revised 2026-10-09 after review: the
level-3 scheme of the first draft (`sha256(total length ‖ first 64 KiB)`)
could not pass the plan's crafted-collision test (§3). Revised again
2026-10-09: the resolver's concurrency race (§4.3) is closed by
per-observation LWT claims (§4.4). Audit:
[audit.md](audit.md). Decisions and the two proposed plan corrections
(PC-1, PC-2): [decisions.md](decisions.md).

## 1. Scope and boundaries

**P8 delivers** identity levels 1–3 only:

- the **resolver**: `media.discovered` → `MediaObservation` (M2/M3) +
  `media.observed`, with provenance (level 1);
- the **probe pool**: technical hints per media through P4's media probe
  (level 2);
- the **hash reader**: a full-content digest per media content version
  (level 3, `full-bytes/v1`; needs PC-1);
- the **encode requester**: at most one logical `encode.requested` per
  (`ContentId`, spec), and the crawler2 projection of
  `representation.ready` / `encode.failed`;
- the synthetic dedup benchmark and the different-content test set of the
  exit gate.

**P8 does not:**

| Not in P8 | Owner |
|---|---|
| storing media bytes, decoding, encoding, representations | P9 encoder (fingerprinter repo) |
| targets, target relevance | P9 target manager |
| perceptual similarity, verified equivalence (levels 4–5), the equivalence relation | P10 |
| alternative-source re-requests after `encode.failed` | P9 coordination (P8-D7) |
| manifest/rendition hashing (HLS/DASH content keys) | a later reviewed design (P8-D6) |
| match feedback, evidence, analytics | P11, P12, P13 |
| scheduling, priorities, recrawl, admission | P3, P6 (P7 is advisory; WHEN is not implemented) |
| SSRF hardening of page fetching; DNS-rebinding pinning | P14 (audit R-8) |

P8 needs nothing from P7: no source advice, no WHAT, no WHEN, no new
long-running crawl. Its gates are synthetic/fixture-based.

## 2. The four kinds of "sameness"

They are different claims and P8 keeps them apart:

| Kind | Key | Claim | Who decides | Reversible? |
|---|---|---|---|---|
| locator identity (level 1) | `MediaId` = `MediaId.of_locator(canonical URL)` | same address; nothing about bytes | P8, deterministically from the URL | not needed: never merged |
| technical hints (level 2) | probe row: length, ETag, Last-Modified, type, container, **head-sample digest** (P8-internal) | "probably the same file"; hint only | P8 probe | hints are facts about one probe, never shared keys |
| content identity (level 3) | `ContentKey(scheme="full-bytes/v1", digest=sha256(all bytes))` → `ContentId` | same exact byte sequence | P8 hash reader | equality is byte identity (under SHA-256 collision resistance) |
| verified media equivalence (level 5) | equivalence relation | same underlying work across different bytes | P10 | yes (plan §P8) |

**Deduplicated encode work** is a *use* of level 3, not a fifth identity:
`encode.requested` is keyed by `(content_id, required_spec)` (P1 catalog),
and `Representation.representation_id` is derived from `(content_id,
spec)` only (P1 `models/representations.py`). Every media sharing a
`ContentId` therefore shares **one** representation and every
`match.found` on it. Sharing a `ContentId` is, in effect, a merge for
everything downstream (matching, feedback, evidence). That is why §3 only
lets byte-identical content share one.

Other identity rules (unchanged from P1/P2):

| Question | Answer |
|---|---|
| URL canonicalization | P1 `canonicalize_url` v1; the query string is kept, so a token-bearing URL is another locator (P8-D2). Many locators with the same bytes share one `ContentId` and one encode |
| same URL, changed bytes | same `MediaId`, new `ContentId`; a new M6 version row |
| different encodings / packaging of one film | different bytes → different `ContentId`s; never linked by P8 |
| redirects | the referenced locator keeps its `MediaId`; final URL and hop count are probe provenance (P8-D4) |
| HLS/DASH | the manifest locator is registered at level 1 with hints; no content key in v1 (P8-D6) |
| provenance | every observation names `page_observation_id`, page `UrlRef`, `page_version_id`, `observed_at`; every probe/hash row names its request, final URL, worker and time |

An observation never carries a level it did not measure: without a valid
hash it has no content key (P1 validator: content requires `probed`).

## 3. Level-3 collisions (blocking issue of the first draft) — P8-D5

### 3.1 What the gate says

Plan §P8 (frozen wording):

- Test: "Byte-identical video on 3 URLs → one level-3 identity, one encode."
- Test: "Crafted level-3 collision (same sampled bytes, different content)
  → not treated as the same media."
- Exit gate: "zero duplicate encode tasks for byte-identical content in a
  synthetic dedup benchmark; zero false merges in the different-content
  test set."
- Design text: "A level-3 hash collision between different content must
  be detectable: level-4 disagreement splits them."

### 3.2 What "false merge" means here

A **false merge** is any of these for two media whose byte content
differs, in the different-content test set:

1. the same `ContentId` (so one encode claim, one representation, shared
   match attribution through M5);
2. the same `MediaId` for different locators;
3. any P8 record asserting they are the same media (P8 writes no
   equivalence records at all).

A **duplicate encode task** is more than one encode claim, or more than
one distinct `encode.requested` idempotency key, for one group of
byte-identical media. Redelivered copies of the same event (same
`event_id`) are one task.

The different-content test set contains both the different-encodings
cases (re-encoded bitrate, other container, HLS vs MP4, other segmenting)
and the crafted collisions (equal length and equal sampled ranges, bytes
differ elsewhere). Both conditions must hold at zero.

### 3.3 Why the first draft fails

Under `sampled-bytes/v1`, a crafted collision gets one `ContentId` → one
claim → one encode → one representation, attributed to both media.
That is a false merge by §3.2 (1). The plan's escape, "level-4
disagreement splits them", **cannot happen under P1**: a representation
is keyed by `(content_id, spec)`, so the second file is never encoded
separately and no disagreement can be observed. Sampling more ranges does
not help: a file crafted to match every sampled range still collides.
Keeping `MediaId`s apart does not help either, because the encode work
and everything keyed by it are shared.

**No sampling scheme can satisfy both gate conditions.** Either the
crafted pair shares a `ContentId` (false merge), or byte-identical copies
do not (duplicate encodes). Telling them apart requires reading every
byte of each.

### 3.4 Decision (proposed): `full-bytes/v1`

- `ContentKey.scheme = "full-bytes/v1"`, `digest = sha256` of the entire
  byte sequence served for the locator (identity encoding: no
  `Accept-Encoding`, and the response must not be transfer-compressed).
- Computed by the **hash reader**: one streamed GET per media content
  version, bytes hashed and discarded, nothing stored. Bounded by
  `media.hash_max_bytes`; above it, no content key and no encode, with
  the reason recorded.
- The P4 head sample (first 64 KiB, `MediaProbe`) stays a **level-2
  hint**. It never becomes a `ContentId` and never decides sharing.
- Result against the gate:
  - byte-identical on 3 URLs → equal SHA-256 → one `ContentId`, one claim,
    one encode;
  - crafted same-sample collision → different SHA-256 → two `ContentId`s,
    two encodes, no shared attribution;
  - different encodings → different bytes → different `ContentId`s.
- The guarantee is **conditional on SHA-256 collision resistance**. A
  real SHA-256 collision is out of scope, as for every `ContentDigest` in
  P1. This is stated, not hidden.
- No P1 change: `ContentKeyScheme` is a free versioned name, and
  `ContentDigest` is documented as "SHA-256 of an exact byte sequence".
- No P4 change: the hash reader is P8 code using the same guarded httpx
  transport as the probe (§5.3). P4's 64 KiB invariant (D2) is untouched.

### 3.5 What this needs from the operator

This scheme reads each encodable media's full bytes inside crawler2. The
plan says "lightweight probe step … Full media download stays in the
fingerprinter service". That needs **plan correction PC-1** (decisions.md
§2). The plan sentence "level-4 disagreement splits them" cannot be
implemented under P1 and is replaced by "level-3 equality is byte
identity", which needs **PC-2**. Neither weakens the gate. Both were
approved in principle by the operator on 2026-10-09 (decisions.md §2).

Cost (stated, not measured): one extra full transfer per media content
version, on top of the fingerprinter's own acquisition of one source per
`ContentId`.

If PC-1 is rejected, P8 cannot pass the crafted-collision condition. The
only remaining paths are both formal:

- a gate exception that records the crafted-collision test as failing,
  with a rule that P11/P12 never rely on a level-3 share;
- a P1 major version moving byte verification into the fingerprinter.

The rejected optimization is recorded in decisions.md §4 (F-1): verify
with a full read only when head samples collide.

## 4. Consuming `media.discovered` — P8-D3

### 4.1 Mechanism

The resolver reads `media.discovered` rows from the **outbox** through
P2's read-only `OutboxStore` (`rows(shard, bucket)`, `earliest_bucket()`),
in whole minute buckets older than `events.relay_settle_s`, the same
mechanism as ADR-023. It joins no consumer group. So a stopped or slow
registry cannot hold back the relay under ADR-022, and crawling is
unaffected.

Unlike a P7 projection, the registry's rows are **authoritative**, so:

- **no generations and no rebuild**: authoritative facts are never
  re-derived from the outbox, and a policy change does not restart from
  the oldest bucket;
- a retention gap is a **halt**, not a restart (§4.6).

This needs an ADR (P8-D3), because ADR-023 covers rebuildable projections
only. The proposed scope is in decisions.md §3. ADR-022 and ADR-023 stay
unchanged.

### 4.2 Storage guarantees this relies on (verified in P2 code)

| Primitive | What it guarantees | Source |
|---|---|---|
| `record_observation(observation, event=envelope)` | one **logged batch**: M2 + M3 + the `media.observed` outbox row, all or nothing (batchlog). Cross-partition; **not isolated, no condition possible**. The derived M1/M4–M6 writes follow outside the batch. M2/M3 are plain `INSERT`s: concurrent writers get last-write-wins | `scylla/media.py`, `scylla/web.py write_authoritative` |
| outbox row | partition `(shard_of(event_id), outbox_bucket(enqueued_at))`, key `event_id`; `enqueued_at` = write time. Re-writing the same envelope later creates **another row with the same `event_id`** in a newer bucket | `scylla/outbox.py statement` |
| single-partition LWT (`IF NOT EXISTS`, `IF col = ?`), `LOCAL_SERIAL` | linearizable compare-and-set **within one partition**. A conditional statement cannot join a multi-partition batch | `scylla/evidence.py` (E1/E2, "pattern B"), `intelligence/store.py` (P7 checkpoint CAS), `migrations.py` |
| outbox pattern B | write-once LWT row first, outbox row after; identical replays succeed, a different value raises `ConflictError` | `scylla/evidence.py create_candidate` |
| consumer dedup (`processed_events`) | `is_processed` then a plain `INSERT`: check-then-act, not atomic. Two *different* events with one idempotency key can both be processed under concurrent delivery | `storage/events/consumer.py`, `scylla/outbox.py` |

Not covered by existing tests: concurrent `record_observation` calls for
one key (the P2 media test covers read models and replay only).

### 4.3 The race in the previous draft

That draft did a lease, an M2 existence check, `record_observation`, and
a checkpoint CAS after the bucket. The race:

1. A holds the lease and folds bucket `b`. It reads "no M2 row" for
   reference `r`, then pauses (GC, network) past `lease_until`.
2. B takes the lease and folds `b`. It also reads "no M2 row" for `r`, and
   writes M2 with its own probe view and a new `media.observed` (event
   `e_B`).
3. A resumes and writes M2 with its probe view and `e_A`.

Result:

- M2 holds A's document.
- Two **different** events exist for one idempotency key. Downstream dedup
  is not atomic, so both may be processed, and the first one consumers
  saw (`e_B`) disagrees with M2.
- The checkpoint CAS on B's side is correct, but it fences nothing written
  before it.

A lease alone cannot stop a paused owner. Scylla cannot condition a write
in one partition on a lease in another.

### 4.4 Correction: decide each observation once, by its own LWT

Since cross-partition fencing is unavailable, the decision is moved into
the fact's own partition. The pattern is P2's evidence pattern B.

**Additive P8 storage primitive** (V005, P8 repository; P2 code
unchanged):

```
media_observation_claims  PRIMARY KEY ((page_observation_id), media_id)   -- no TTL, like M2
  envelope text      -- the full media.observed envelope (event_id, occurred_at, producer, payload)
  claimed_at, claimed_epoch, committed_at
```

```
claim_observation(envelope) -> Claim(won: bool, envelope: stored, committed: bool)
    INSERT … IF NOT EXISTS at LOCAL_SERIAL; on loss returns the stored row
mark_committed(page_observation_id, media_id)
    UPDATE … SET committed_at = ? IF committed_at = null   (idempotent)
```

**Fold of one reference `r`** in bucket `b` by the reader with epoch `E`:

1. Build the observation from the outbox row and `media_probe_latest`.
   Wrap it in a new envelope (`EventId.new()`).
2. `claim_observation(envelope)`. **The first claim wins, linearizably.**
   On loss, the stored envelope replaces the one just built. A reader
   never commits anything but the stored envelope.
3. If not `committed`: `record_observation(stored.payload.observation,
   event=stored)` (P2, unchanged; the caller supplies the envelope), then
   `mark_committed`.
4. Want a probe (§5.1).

**Reader state** (`media_reader_state`, one partition): `epoch`, `owner`,
`lease_until`, `next_bucket`, `state`, gap range. Every transition is an
LWT on that partition:

| Transition | Condition |
|---|---|
| acquire | `SET owner = me, epoch = E+1, lease_until = t IF epoch = E AND lease_until < now` (`now` from Redis `TIME`) |
| renew | `IF epoch = myE` |
| advance `b → b+1` | `IF epoch = myE AND next_bucket = b`, only after **every** reference of `b` passed step 3 (claimed and committed) in this process |
| enter gap / `accept-gap` | `IF epoch = myE` |

The owner re-reads its lease (SERIAL) before each outbox row (≤ 1,000
references) and stops at the first failure, which bounds stale work.

### 4.5 What this guarantees, and what it does not

| Requirement | Guarantee |
|---|---|
| one authoritative observation | **yes.** Exactly one envelope is ever decided per (page observation, media), by single-partition LWT. Every M2/M3 write, by any reader, writes that envelope's observation: identical values, so the final M2/M3 is that observation |
| one logical `media.observed` | **yes: one decision and one `event_id` per (page observation, media).** Physical outbox rows are **not** bounded: every re-commit after a crash between `record_observation` and `mark_committed` appends another row carrying the same stored envelope and `event_id`, and repeated crashes at that point append more. Delivery is **at-least-once** (B.4 #5). **Exactly-once delivery is not claimed** |
| downstream duplicate processing | **not solved by P8.** Consumers dedupe through `processed_events` with a non-atomic check-then-insert (§4.2), so redeliveries of one `event_id` delivered concurrently can be processed more than once. That is an existing, separate limitation of the consumer layer; consumers must stay idempotent (B.4 #5). P8's claim only guarantees that every copy is the same envelope |
| stale owner cannot commit after ownership changes | **checkpoint:** yes (epoch CAS). **Facts:** a stale owner cannot override or duplicate a decided fact. It can still *win the claim* for a reference the new owner has not reached yet, inside the window before its next lease check. That claim is a valid, single decision, and the new owner commits the same envelope. **Strict lease fencing of each fact write is impossible with single-partition LWT and is not claimed** |
| crash between commit and checkpoint advance | safe: re-fold finds `committed`, skips |
| crash between claim and commit | safe: re-fold finds the claim uncommitted and commits the stored envelope |
| checkpoint never past uncommitted work | yes: advance requires every reference of `b` committed, and it is conditioned on the epoch |
| replay idempotent | yes: claims are never re-decided; commits rewrite identical rows |
| crawling never depends on the registry | unchanged (§4.1) |

*Implementation note (2026-10-09, increment 1):* an outbox row's key is
(shard of `event_id`, minute bucket of the write, `event_id`). Re-commits
inside one minute bucket therefore upsert the same physical row; each
re-commit in a later bucket adds one. The bound statement above stays
true: physical copies are unbounded over time, never asserted
(validation.md I1-6).

**Cost:** per reference, one LWT (Paxos), one logged batch and one
conditional update. M1 had 3,304 references in ~46 h. Throughput is
measured in increment 1 and is not assumed.

**Possible with P2 as is:** steps 3–4. `record_observation` accepts a
caller-built envelope, and identical rewrites are idempotent.

**Needs an additive primitive:** the claim table and its two methods,
owned by P8. No P2 code or schema changes, no P1 change.

### 4.6 Retention gap: halt, never silent loss

- **Lag alarm.** `media_reader_lag_seconds` is exported. Status turns
  `warning` when lag exceeds `media.reader_lag_warn` (default 3 d) and
  `critical` beyond 7 d, half the 14-day outbox retention.
- **Gap detection.** If `next_bucket` is older than the oldest retained
  bucket (`earliest_bucket()` or `now − 14 d + margin`), facts may have
  expired unread. The reader then:
  - sets `state = gap` with the missing range;
  - **stops** (it does not jump forward);
  - logs `media_reader_gap`;
  - sets `media_reader_gap_total` and `media_reader_state{state="gap"}`;
  - makes `crawler2-media resolve status` exit non-zero.

  Crawling continues (§4.1).
- **Operator resolution (v1).** `crawler2-media resolve accept-gap` records
  the range as accepted loss in `media_reader_gaps` and moves
  `next_bucket` to the oldest retained bucket. Nothing is skipped without
  that record.
- **Backfill source (not built in v1):**
  - P5's `page_extracts` (no TTL) holds the media list per
    `page_version_id`.
  - P2's `observations_by_domain_day` (30-day TTL, per domain × day ×
    shard) lists sightings with their `page_version_id`.
  - Together they could reconstruct the equivalent of `media.discovered`
    for sightings up to 30 days old, with bounded queries.
  - Older sightings are only in `page_observations_by_url` per URL, so a
    full scan would be needed.
  - The reconstruction is neither designed nor validated, so **lossless
    recovery is not claimed**.
  - A repair tool is a separate decision if a gap ever occurs (F-2).

## 5. Probing (level 2) and hashing (level 3)

### 5.1 What is probed and hashed (Q2)

- Per `MediaId`, never per sighting (one M1 locator was on 227 pages).
  A probe is wanted when a media has no probe, or its latest probe is
  older than `media.reprobe_after` (**7 d**, configurable).
- Kinds: `video_file` and `audio_file` are probed and hashed. Manifests
  are registered and probed for hints only, with no content key
  (P8-D6). `unknown` is probed. It is hashed only if the probe reports a
  media content type or container.
- **Re-hash** after a probe when its validators (total length, ETag,
  Last-Modified) differ from those of the last hashed probe. Without any
  validator, re-hash when `media.rehash_after` (**30 d**) has passed since
  the last hash.
- **No P6 ad/tracker exclusion in v1 (Q3).** The registry is complete. A
  later filter needs its own evidence and decision.
- Work queues: Redis lists `media:probe` and `media:hash` of `media_id`s,
  plus a `SET NX` lease per media and stage, so wants are deduplicated
  across hosts. A lost queue loses no fact: the next observation, or a
  re-probe, wants the media again.

### 5.2 Rate limits (Q2; defaults to be confirmed)

| Setting | Default | Why |
|---|---|---|
| `media.probe_concurrency` | 8 | 64 KiB reads; matches P4's http slot scale |
| `media.probe_per_host_interval_s` | 2 s | probes bypass P3's per-domain politeness; one request per 2 s per host |
| `media.hash_concurrency` | 2 | full transfers; laptop bandwidth (operator constraint 2026-10-09) |
| `media.hash_per_host_concurrency` | 1 | one full transfer per host at a time |
| `media.hash_max_bytes` | 8 GiB | above typical single-file films (assumption, to be checked on fixtures) |
| `media.hash_timeout_s` | 3,600 | bounded transfer time |

Intervals use Redis `TIME` (B.4 #3).

### 5.3 Execution and SSRF

- **Probe:** P4's `HttpFetcher` with `Expectation.MEDIA_PROBE` (D2: at
  most 64 KiB read). No P4 change.
- **Hash reader:** P8 code; a streamed httpx GET with manual redirects
  (≤ P4's `max_redirects`), the size and time bounds, and SHA-256 over
  the bytes as they arrive.
- **SSRF guard (mandatory on both paths):**
  - The fingerprinter's `ssrf_guard` logic is ported. It rejects
    loopback, private, link-local, unspecified, multicast, reserved and
    100.64/10 addresses.
  - It runs as an **httpx transport wrapper**, injected into
    `HttpFetcher`'s existing `transport` parameter and into the hash
    reader.
  - Redirects are manual, so every hop is a new request through the
    transport. **Every redirect hop is re-checked.**
  - Only `http`/`https`.
  - A rejection is recorded as `forbidden` with
    `fetch_detail = unsafe_destination`, and nothing is fetched.
- **Ported tests:** the guard's test suite plus redirect-to-private
  fixtures for both paths.
- **Not in P8:** the DNS-rebinding gap (resolve-then-connect), which
  stays documented for P14, as in the fingerprinter.

### 5.4 Probe and hash facts (P8-D8, V005)

```
media_probes   ((media_id, month), probed_at DESC, probe_id)   authoritative
  probe_status, final_url, redirect_count, http_status, content_type,
  declared_length, total_length, accept_ranges, range_honoured, etag,
  last_modified, bytes_read, container, head_sample_sha256?, worker, fetch_detail
media_hashes   ((media_id, month), hashed_at DESC, hash_id)    authoritative
  outcome (ok | too_large | timeout | unreachable | forbidden | changed_during_read),
  scheme, digest, content_id, byte_count, final_url, probe_id, worker
media_probe_latest (media_id)   derived: latest probe + latest ok hash (+ its probe validators)
```

`head_sample_sha256` is recorded only if a later reviewed P4 addition
provides it. It is a hint and is not needed for correctness.

Partitions are month-bucketed, as M3/M6. Retention is the same as M2/M3:
none.

## 6. Observations, versions and M5/M6

### 6.1 Written once (P8-D8)

`media.observed` is keyed by (page observation, media), so it is emitted
once per pair. The resolver decides each observation once by its claim (§4.4) and never
rewrites it after a later probe or hash. The first sighting of a new media
is `not_probed`. Later sightings carry whatever was known at their
resolution time.

### 6.2 Content on an observation

An observation carries `content` only when `media_probe_latest` has an
`ok` hash **whose probe validators equal the latest probe's**. If the
latest probe shows a change (other length, ETag or Last-Modified), the
hash is stale. The observation then carries hints but no content key
until a re-hash. It never carries a content key P8 has not verified for
the current version.

### 6.3 M5/M6 from hash facts

- Every `ok` hash row also updates the existing M5 and M6 tables, using
  P2's write rules:
  - **M5** `media_by_content`: `first_seen` earliest-wins = the earliest
    of hash time and observation time;
  - **M6** `media_content_versions(media_id, month(hashed_at), content_id)`:
    first earliest-wins, last latest-wins.
- Observations with content keep updating M5/M6 through P2's existing
  `_derived` path. Both paths write the same keys with the same
  timestamp rules, so they commute.
- M6 versions mean "the registry verified this content at this locator
  between first and last".
- `rebuild_media` replays M2 (existing) **and** `media_hashes` (new).
- This is additive to P2's "Derived (M2)" note. The table schemas are
  unchanged.
- "Same URL, changed content → new version": the next probe shows
  different validators, the re-hash gives a new `ContentId`, and a new
  M6 row appears.

## 7. Encode requests and representations

### 7.1 One logical request per (content, spec)

1. After an `ok` hash, the requester reads
   `representation_status(content_id)` (P1 projection). If the default
   spec is `ready` or `requested`, it emits nothing.
2. Otherwise it claims:
   `INSERT INTO encode_claims (content_id, spec_key, event_id, claimed_at,
   source_media_id, referer) … IF NOT EXISTS`. The event ID is allocated
   (`EventId.new()`) before the claim, and the keyspace has tablets
   disabled for LWT.
3. **Only the winner** writes the `encode.requested` outbox row, with the
   **stored** `event_id` (`new_event(event_id=…)`), then sets
   `emitted_at`.
4. **Crash recovery.** `encode_claims_by_day (day, shard)` (TTL 14 d)
   indexes claims. A sweeper re-emits claims without `emitted_at` that are
   older than `media.claim_emit_grace_s` (default 300 s), with the same
   `event_id`. The result is a duplicate *event*, never a second claim.
5. `source` = the hashed `Media`; `referer` = the page of the observation
   that triggered the probe; `required_spec` absent (the fingerprinter's
   default, `spec_key` = `default` in P2).

### 7.2 Failures (P8-D7)

`encode.failed` updates the projection only. Re-requesting with another
source is not implemented. The idempotency key `(content_id,
required_spec)` makes such a re-request a logical duplicate, while the
payload docstring allows it. That conflict goes to P9. P8 does not
change the semantics.

### 7.3 Representation projection

`representation.ready` and `encode.failed` are applied with the existing
idempotent `ProjectionRepository` methods.

- **Delivery:** these streams come from the fingerprinter and are not on
  the crawl loop. They are read by a consumer group `media-registry`.
- **Halt risk:** none for crawler2. The crawler2 relay publishes only
  crawler2 outbox rows (`relay.py`), and the catalog makes the encoder the
  sole producer of these events, so they are never in crawler2's outbox.
  A lagging `media-registry` group can only hold back the fingerprinter's
  own publisher, if that publisher applies ADR-022. That is P9's design.
- **Testing:** nothing produces these events until P9, so P8 tests use
  contract fixtures.

## 8. Storage, ownership and retention

- **New migration V005 (P8-owned):**
  - `media_reader_state` (epoch, lease, checkpoint, gap state) and
    `media_reader_gaps`;
  - `media_observation_claims` (write-once decision per observation,
    §4.4);
  - `media_probes`, `media_hashes` and `media_probe_latest`;
  - `encode_claims` and `encode_claims_by_day`.
- **Existing tables:** M1–M6 and `representation_status` are reused
  through their repositories, with unchanged schemas.
- **Ownership:** everything is in the crawler2 keyspace. Other components
  read it only through the media repository. The fingerprinter learns P8
  facts only from `encode.requested` (ADR-011).
- **Not used:** no MinIO objects (no media bytes stored), no graph, no
  vector store. Redis holds only the work queues, leases and rate-limit
  state.

## 9. Failure semantics

| Failure | Behaviour |
|---|---|
| duplicate / concurrent `media.discovered` | the claim LWT decides once; losers commit the stored envelope (identical rows, same `event_id`) |
| reader loses its lease mid-bucket | its checkpoint CAS fails (epoch); its claims stay valid single decisions; the new owner re-folds the bucket (§4.4–§4.5) |
| crash after claim, before or during commit | re-fold commits the stored envelope; possibly more outbox rows with the same `event_id` and envelope (redeliveries, unbounded under repeated crashes) |
| Scylla unavailable | typed `StorageUnavailableError`; checkpoint not advanced; retry with backoff |
| reader stopped / slow | crawl unaffected; lag alarm at 3 d / 7 d |
| outbox retention gap | `state = gap`, reader halts, operator `accept-gap` (§4.6) |
| probe unreachable / 403 / not media | `probe_status` recorded; re-probed after `reprobe_after` |
| SSRF guard rejects (any hop) | `forbidden`, `unsafe_destination`; never fetched |
| hash too large / timeout / bytes changed during read | `media_hashes.outcome` recorded; no content key, no encode |
| crash between claim and outbox write | sweeper re-emits with the stored `event_id` |
| probe/hash queue lost | refilled by later observations or re-probes |

## 10. Increments, tests and exit gate

| Inc | Content | Tests |
|---|---|---|
| 1 | resolver, outbox reader, reader state (epoch, lease, checkpoint), observation claims, gap state, `crawler2-media resolve run\|status\|accept-gap` | the concurrency/failure suite of §10.1; duplicate/concurrent delivery; replay of a bucket emits nothing new; crash before advance; gap → halt + metric + non-zero status, no silent skip; provenance preserved; one locator on many pages and domains; token URL → distinct `MediaId`; real stack: registry stopped while the relay publishes `page.observed`/`urls.discovered` (crawl loop unaffected) |
| 2 | probe pool, hash reader, SSRF transport, V005 probe/hash tables, M5/M6 from hashes | ported SSRF suite + redirect-to-private for both paths; fixture server: Range honoured/ignored, redirects (provenance), 403, non-media, oversize, changed during read; same URL changed bytes → new `ContentId` + M6 row; stale hash not attached (§6.2); one probe/hash per media under concurrency |
| 3 | encode requester, claims, sweeper, representation projection | claim race between two hosts → 1 claim; crash after claim → same `event_id`; `ready`/`requested` suppresses a request; contract fixtures for `representation.ready`/`encode.failed` |
| exit | synthetic benchmark (fixture server) | **byte-identical:** the same file on 3 URLs (and more groups) → 1 `ContentId` and 1 claim per group: **0 duplicate encode tasks**. **Different-content set:** re-encoded bitrate, other container, HLS vs MP4, other segmenting, and **crafted collisions** (equal length and equal first/middle/last 64 KiB, different bytes elsewhere) → pairwise distinct `ContentId`s and claims, no shared `MediaId`, no P8 equivalence record: **0 false merges** (§3.2) |

### 10.1 Resolver concurrency and failure tests (increment 1)

These run against the real Scylla (integration tier, throwaway keyspace).
Two reader instances (A, B) run in one process with:

- an injected Redis-`TIME` clock;
- named pause hooks between the steps of §4.4 (after build, after claim,
  after `record_observation`, before `mark_committed`, before advance).

No sleeps decide the outcome. Every test ends with the same assertions,
called **the invariant** below:

- exactly one M2 row and one M3 row per (page observation, media);
- that row equals the claimed envelope's observation;
- the outbox holds exactly one distinct `event_id` with that idempotency
  key; every row with that `event_id` (≥ 1, no upper bound asserted)
  holds the identical stored envelope;
- the checkpoint is never above a bucket with an uncommitted claim.

| # | Scenario | Expected |
|---|---|---|
| C-1 | A claims `r` in `b`, pauses before `record_observation`; its lease expires; B acquires (epoch +1), folds `b` | B loses the claim on `r`, commits **A's** stored envelope, commits the rest of `b`, advances `b → b+1` |
| C-2 | continue C-1: A resumes, runs `record_observation` and `mark_committed` for `r`, then tries to advance `b` | A's rows are identical (no-op); A's advance CAS fails (epoch); A's next lease check fails and it stops. The invariant holds: one `event_id`; every outbox row carrying it holds the identical envelope |
| C-3 | A pauses after build, before claim; B takes over, claims and commits `r`; A resumes and claims | A's claim loses; A commits B's stored envelope (identical) or stops at its lease check. Exactly one envelope (B's); A's different envelope is never written to M2 or the outbox |
| C-4 | A and B race the claim for `r` (released together) with **different** probe views | one winner; M2 equals the winner's document; no second `event_id` |
| C-5 | crash (instance discarded) after claim, before `record_observation`; restart | re-fold commits the stored envelope; M2 appears; one `event_id` |
| C-6 | **repeated** crash-and-recovery at the same point: crash after `record_observation`, before `mark_committed`; restart; repeat k times (k ∈ {1, 3, 5}); then let the run finish | after each cycle and at the end: exactly one distinct `event_id` for the key; every outbox row carrying it holds a byte-identical envelope equal to the claim; M2/M3 equal the claimed observation; `next_bucket` never above `b` while the claim is uncommitted. Eventually `committed_at` is set and the checkpoint advances once. The number of physical rows is recorded (expected to grow with k), **not** bounded |
| C-7 | crash after every reference of `b` committed, before advance; restart | re-fold claims nothing new and writes no new outbox rows; advance succeeds once |
| C-8 | `record_observation` raises `StorageUnavailableError` for one reference | no advance; retry commits it; then advance |
| C-9 | stale A tries to advance while B is at `b` | CAS fails; `next_bucket` unchanged |
| C-10 | replay of an already-advanced bucket (the test sets `next_bucket` back directly in `media_reader_state`) | no new claims, no new outbox rows |
| C-11 | two hosts (two-host compose profile, real lease timing) | one active reader; the invariant holds over a lease handover |

- **Coverage:** ≥ 85 % of new code (B.3), measured per increment and at
  exit.
- **Two-host (B.4 #8):** the two-host compose profile runs:
  - two resolvers (one active, one standby; lease handover mid-bucket);
  - probe and hash workers on both hosts (per-media lease);
  - concurrent encode claims.
- **Metrics:** `media_reader_lag_seconds`, `media_reader_gap_total`,
  `media_observations_total`, `media_probes_total{status}`,
  `media_hashes_total{outcome}`, `media_hash_bytes_total`,
  `media_ssrf_rejections_total`, `media_encode_claims_total{result}`.

## 11. Invariants (B.4 / B.5)

- B.4:
  - no authoritative local state;
  - leases and rate limits in Redis, using Redis `TIME`;
  - idempotent handlers;
  - LWT only for the reader state (lease, epoch, checkpoint), observation
    claims and encode claims; each is single-partition.
- B.5 #1: the crawler never depends on P8. Under §4 this holds
  operationally too.
- B.5 #2: no fingerprinter keyspace access.
- B.5 #3: M1/M4/M5/M6 and `media_probe_latest` are rebuildable from
  M2/M3 + `media_probes` + `media_hashes`.
- B.5 #5: no contract change.

## 12. Limitations and handoffs

- **Assumption:** SHA-256 collision resistance (§3.4).
- **Not detected:** content changed with unchanged validators within
  `rehash_after`.
- **Bandwidth:** one full transfer per media content version, plus the
  fingerprinter's acquisition (PC-1).
- **Recovery:** no lossless recovery of an outbox gap beyond 14 days; the
  operator accepts it explicitly.
- **Delivery:** `media.observed` is one logical event (one `event_id`)
  delivered at-least-once, with an unbounded number of physical outbox
  rows under repeated crashes; exactly-once delivery is not claimed.
- **Downstream dedup:** `processed_events` check-then-insert is not
  atomic, so concurrent redeliveries can be processed twice. It is a
  separate consumer-layer limitation, outside P8 scope; P8 does not fix
  it.
- **Fencing:** individual fact writes are not fenced by the reader lease
  (single-partition LWT); correctness rests on per-fact claims (§4.5).
- **Manifests:** no content identity in v1 (P8-D6).
- **To P9:**
  - `encode.requested` semantics for re-requests (P8-D7);
  - the default spec identity before the first `representation.ready`;
  - P9 may acquire from any media of the `ContentId`, since all are byte
    identical.
- **To P10:** level-5 equivalence across different `ContentId`s. P8
  gives P10 no level-3 collision to split.
- **To P14:** SSRF hardening of page fetching; DNS-rebinding pinning.
