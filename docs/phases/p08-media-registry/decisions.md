# P8 Decisions, open questions and deferred work

Audit: [audit.md](audit.md). Design: [design.md](design.md).

Status vocabulary:

- **FOLLOWS:** restates frozen P1/P2 semantics and changes nothing.
- **PROPOSED:** awaits the operator at the B.1 design review.
- **APPROVED:** the operator approved it.
- **DEFERRED:** assigned to a later phase.

**2026-10-09: the operator approved the P8 design for implementation**
(B.1 review): P8-D2…P8-D8, Q2, Q3, the ADR-024 scope (§3) and the plan
corrections PC-1/PC-2 (§2), which are now applied to the plan text.
Approval of the design is not a pass of any implementation or exit gate;
gate status is tracked in [validation.md](validation.md) and
[exit.md](exit.md).

## 1. Decisions

| # | Decision | Status | Date | Reason |
|---|---|---|---|---|
| P8-D1 | **Scope = identity levels 1–3:** resolver (observations, `media.observed`), probe (hints), hash reader (content key), `encode.requested` emitter, the `representation_status` projection, and the exit-gate benchmark. Encoder, targets, levels 4–5, equivalence, feedback, evidence and analytics are out (P9–P13). P8 needs nothing from P7 and no new crawl | FOLLOWS (plan §P8, ADR-011, P7 exit.md §8) | 2026-10-09 | design §1 |
| P8-D2 | **Level 1 = `MediaId.of_locator` over P1 `canonicalize_url` v1, query kept.** No token stripping: the locator must stay fetchable for `encode.requested.source`, M1 shows 3 query-bearing references of 3,304, and a later rule would be a new ID scheme. Token churn is absorbed at level 3 (byte-identical → one `ContentId`, one encode) and measured as level-1 growth | FOLLOWS (P1), **APPROVED** | 2026-10-09 | audit §2.1, §5; design §2 |
| P8-D3 | **`media.discovered` is read from the outbox (ADR-023 mechanism: own checkpoint, settle rule), not through a consumer group**, so a stopped registry cannot stall the crawl loop under ADR-022. Registry rows are authoritative, so it differs from ADR-023 in four ways. (1) **Each observation is decided once by a single-partition LWT claim** (`media_observation_claims`, P8-owned, additive) that stores the full `media.observed` envelope; every commit, by any reader, writes that stored envelope through P2's unchanged `record_observation`. (2) **Reader state is one partition** (epoch, lease, checkpoint, gap state); acquire/renew/advance/gap are LWTs conditioned on the epoch, and the checkpoint advances only after every reference of the bucket is claimed and committed. (3) No generations and no rebuild. (4) A retention gap **halts** the reader in state `gap` with alarm, metric and non-zero status until the operator records it with `accept-gap`; lag warnings at 3 d / 7 d; no backfill in v1; lossless recovery is not claimed. **Guaranteed:** one authoritative decision, one observation and one logical event (`event_id`) per (page observation, media); every physical copy carries the identical stored envelope; no checkpoint past uncommitted work; idempotent replay; crash-safe at every step. **Not guaranteed:** exactly-once *delivery*: each re-commit after a crash appends another outbox row with the same `event_id`, unbounded under repeated crashes, so delivery is at-least-once; downstream duplicate processing: `processed_events` is a non-atomic check-then-insert, an existing, separate consumer-layer limitation that P8's claim does not solve and P8 does not fix; strict lease fencing of individual fact writes, which single-partition LWT cannot provide: a stale owner can still win a claim before its next lease check, and that claim is a valid single decision. Recorded in [ADR-024](../../adr/ADR-024-authoritative-outbox-readers.md) (scope in §3) | **APPROVED** | 2026-10-09 | design §4.2–§4.6, §10.1 |
| P8-D4 | **Redirects are probe provenance, not identity.** The referenced locator keeps its `MediaId`; the final URL and hop count are stored on probe/hash rows. No `MediaId` is created or merged from a redirect target | **APPROVED** | 2026-10-09 | design §2, §5.4 |
| P8-D5 | **Level-3 scheme `full-bytes/v1` = SHA-256 of the entire byte sequence**, computed by a P8 hash reader (streamed, discarded, bounded by `hash_max_bytes`/timeout, SSRF-guarded). Supersedes the first draft's `sampled-bytes/v1` (length ‖ first 64 KiB): that scheme gives a crafted collision one `ContentId`, so one encode, one representation and shared attribution. That is a false merge, and P1 keys representations by `(content_id, spec)`, so level 4 could never split it (design §3.3). The head sample stays a level-2 hint. No P1 change and no P4 change. Guarantee conditional on SHA-256 collision resistance. **Requires PC-1 and PC-2** (both approved and applied) | **APPROVED** | 2026-10-09 | design §3 |
| P8-D6 | **HLS/DASH manifests are registered at level 1, probed for hints, and get no content key in v1.** Manifest/rendition hashing is a separate future design. Progressive `video_file`/`audio_file` (and `unknown` with a media type) are hashed | **APPROVED** | 2026-10-09 | design §5.1 |
| P8-D7 | **No alternative-source re-request after `encode.failed` in P8.** The P1 idempotency key `(content_id, required_spec)` conflicts with the payload docstring ("may re-request with another"). The conflict is assigned to P9; P8 changes no contract semantics | **APPROVED**; conflict DEFERRED to P9 | 2026-10-09 | design §7.2 |
| P8-D8 | **Probe and hash results are separate authoritative facts** (V005 `media_probes`, `media_hashes`). Each observation is decided once (P8-D3 claim) and written with the latest probe and the latest valid hash (validators equal to the latest probe) known at resolution time. Later probes never rewrite it. M5/M6 are also updated from `ok` hash rows with P2's write rules, and `rebuild_media` replays both sources | **APPROVED** | 2026-10-09 | design §5.4, §6 |

## 2. Phase-plan corrections

Neither correction lowers or reinterprets the exit gate. Both are needed
for P8-D5. **Status: APPROVED (operator, 2026-10-09) and applied** to
[the plan](../../v2-phase-plan.md) §P8 with the design approval.

| # | Plan text now | Proposed text | Why |
|---|---|---|---|
| PC-1 | "lightweight probe step (HEAD / range / first-segment hash …) run by a media-probe pool" and "Full media download stays in the fingerprinter service" | "a probe for level-2 hints **and a full-content hash for level 3** (bytes streamed and discarded, never stored), with the SSRF guard. Full media **acquisition for encoding and storage** stays in the fingerprinter" | a crafted collision matches every sampled range; only a full read tells it from a byte-identical copy (design §3.3). Cost: one extra full transfer per media content version |
| PC-2 | "A level-3 hash collision between different content must be detectable: level-4 disagreement splits them." | "Level-3 equality is byte identity (SHA-256 of the full content); a sampled-byte match is a level-2 hint and never shares a `ContentId`." | P1 derives `representation_id` from `(content_id, spec)`, so two media sharing a `ContentId` get one representation and level 4 cannot disagree. The sentence is unimplementable under frozen contracts |

**If PC-1 is rejected,** P8 cannot meet "zero false merges" for the
crafted-collision set. Two formal routes remain:

- **(a)** a recorded gate exception: the crafted-collision condition is
  marked FAIL, and P11/P12 must never rely on a level-3 share;
- **(b)** a P1 major version that moves byte verification into the
  fingerprinter.

Recommendation: approve PC-1 and PC-2.

## 3. ADR-024 (created 2026-10-09 after the P8-D3 approval)

**[ADR-024](../../adr/ADR-024-authoritative-outbox-readers.md):
"Authoritative idempotent consumers may read the outbox instead of
joining a stream group."** Accepted. The scope below is what the ADR
records.

- **Scope:** it extends ADR-023's mechanism to a consumer whose writes are
  authoritative. It applies when:
  - the consumer is off the crawl loop;
  - its writes are deterministic and idempotent per fact;
  - it can stop for days without affecting crawling.
- **Rules:**
  - each fact decided once by an LWT claim in the fact's own partition,
    storing the full envelope; commits write only the stored envelope;
  - reader state in one partition; lease, epoch and checkpoint
    transitions by LWT conditioned on the epoch;
  - checkpoint advance only after every fact of the bucket is committed;
  - stated limits: one logical `event_id`, at-least-once delivery,
    physical outbox rows unbounded under repeated crashes; downstream
    dedup is not made atomic; no strict fencing of fact writes by the
    lease;
  - no generations, no rebuild;
  - a retention gap halts the consumer in an explicit `gap` state;
  - no automatic skip;
  - an operator `accept-gap` record;
  - lag alarms below the retention.
- **Unchanged:** ADR-022, ADR-023, the publisher, relay, stream settings
  and P1 contracts.
- **First user:** the P8 resolver.

## 4. Open questions

| # | Question | Recommendation | Status |
|---|---|---|---|
| Q1 | Approve, amend or reject P8-D3…P8-D8 and the ADR-024 scope; confirm PC-1/PC-2 in the plan text | approve all | **APPROVED** 2026-10-09 (all approved) |
| Q2 | Re-probe interval and rate limits | `reprobe_after` 7 d; `rehash_after` 30 d without validators; probe concurrency 8, 2 s per host; hash concurrency 2, 1 per host; `hash_max_bytes` 8 GiB (assumption). Per `MediaId`, not per sighting (design §5.1–§5.2). All values configurable; none is performance-validated | **APPROVED** |
| Q3 | Exclude media matched by P6 ad/tracker rules from probing? | no, not in v1: the registry stays complete. A later filter needs its own evidence and decision | **APPROVED** |

## 5. Deferred and rejected alternatives

| # | Item | Status |
|---|---|---|
| F-1 | Verify-on-collision: full-hash only when head samples collide, saving one full transfer per unique content. Rejected for v1: it needs two schemes per group, group anchors and LWT races, and is a separate review if bandwidth requires it | rejected for v1 |
| F-2 | Outbox-gap backfill from `page_extracts` + `observations_by_domain_day` (≤ 30 days; older needs a full scan). Not designed or validated | DEFERRED, only if a gap occurs |
| F-3 | Re-request semantics; default spec identity before the first `representation.ready` | DEFERRED to P9 (P8-D7) |
| F-4 | Level-4/5 identity and the equivalence relation | DEFERRED to P10 |
| F-5 | Manifest/rendition content keys | DEFERRED (P8-D6) |
| F-6 | SSRF hardening of page fetching; DNS-rebinding pinning | DEFERRED to P14 |
| F-7 | P4 `MediaProbe.sample_sha256` (head-sample hint) | optional; not needed for correctness after P8-D5 |
