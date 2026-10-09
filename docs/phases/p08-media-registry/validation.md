# P8 Validation — media registry & identity

Status: **in progress.** Increments 1–3 are implemented and validated
below. The exit gate is recorded in its own section once run. Design: [design.md](design.md). Decisions:
[decisions.md](decisions.md). ADR: [ADR-024](../../adr/ADR-024-authoritative-outbox-readers.md).

Status values: **PASS** (the test ran against the stated services and
passed), **FAIL**, **BLOCKED** (could not run), **SKIPPED** (not run, with
reason), **NOT IMPLEMENTED**.

## How the tests ran

| Tier | Command | Services |
|---|---|---|
| unit + contract | `make check` | none |
| P8 host tier | `scripts/test-media.sh` | real Scylla 6.2 (container IP, throwaway keyspace `crawler2_it_p8_host`), real Redis (private namespaces / stream prefixes) |
| Scylla outage | `P8_SCYLLA_OUTAGE=1 scripts/test-media.sh` | as above; freezes the Scylla container with `docker pause` for ~25 s |
| two-host | `scripts/test-media-two-host.sh` | `app-host-1` + `app-host-2` containers (rebuilt image), shared Scylla/Redis, keyspace `crawler2_p8_mh` |
| coverage | `PYTHONPATH=<scratch> PYTEST="env/bin/python -m coverage run --branch --source=crawler2.media -m pytest" scripts/test-media.sh` | `coverage==7.16.2` in the session scratchpad only, not a project dependency (same method as P7 exit.md §9) |

Concurrency tests use an injected clock (the Redis-`TIME` seam) and named
pause hooks (`after_build`, `after_claim`, `after_record`, `before_mark`,
`before_advance`); no sleep decides an outcome. The only sleeps wait for
real minute buckets to close (I1-6b, I1-12) or for the frozen container
(I1-9). Every scenario ends with **the invariant**: one claim; M2 and M3
equal the claimed observation; every outbox row with the observation's
idempotency key carries the claim's `event_id` and the claim's exact bytes;
the checkpoint never above a bucket with an uncommitted claim.

## Increment 1 — resolver (2026-10-09)

| # | Requirement | Implementation | Test / command | Observed | Status |
|---|---|---|---|---|---|
| I1-1 | C-1: stale owner paused after its claim; new owner commits the stored claim and advances | `resolver.py` `_fold_reference`, `store.py` `claim_observation` | `test_c1_c2_new_owner_commits_the_stale_owners_claim` | B (epoch 3) committed A's envelope; `next_bucket` b+1 | PASS |
| I1-2 | C-2: stale owner resumes; identical rows; its advance/lease check fails | epoch CAS, per-row lease check | same test | A reported lease lost / conflict; epoch 3 kept; invariant holds | PASS |
| I1-3 | C-3: stale owner built an envelope before claiming; it is never written | claim loss → stored envelope | `test_c3_…` (spy store) | A's attempted `event_id` absent from the outbox; claim is B's | PASS |
| I1-4 | C-4: two readers race the claim with **different** envelopes | single-partition LWT | `test_c4_…` | one winner; M2 equals the winner's document; one `event_id` | PASS |
| I1-5 | C-5: crash after claim, before commit | re-fold commits stored claim | `test_c5_…` | 0 outbox rows before restart, 1 after; invariant | PASS |
| I1-6 | C-6: repeated crash after `record_observation`, before `mark_committed`, k ∈ {1, 3, 5} | conditional `committed_at`, stored-envelope replay | `test_c6_repeated_crash_after_record_before_mark[1,3,5]` | after every cycle: one `event_id`, byte-identical copies, M2/M3 = claim, `next_bucket` = b; finally committed and advanced once. Physical rows per cycle: k=1 `[1, 1]`, k=3 `[1, 1, 1, 1]`, k=5 `[1, 1, 1, 1, 1, 1]` (all re-commits fell in one minute bucket, which upserts the same row) | PASS |
| I1-6b | C-6 across minute buckets: copies are additional rows with identical bytes | outbox key (shard, bucket, event_id) | `test_c6_copies_in_later_buckets_are_byte_identical` | 3 physical rows in 3 buckets, one `event_id`, identical bytes. No upper bound is asserted | PASS |
| I1-7 | C-7: crash after all commits, before advance | committed claims are replayed, not rewritten | `test_c7_…` | restart: 0 claims, 0 commits, 3 replayed; outbox row counts unchanged; advanced once | PASS |
| I1-8 | C-8: `record_observation` raises `StorageUnavailableError` | no advance on error | `test_c8_…` | `next_bucket` stays b; retry commits; then advances | PASS |
| I1-9 | Scylla failure and recovery (real) | `run_forever` backoff | `test_scylla_outage_is_retried_and_nothing_is_skipped` (`P8_SCYLLA_OUTAGE=1`) | Scylla paused between claim and commit: `OperationTimedOut` → retry; after unpause the stored claims were committed and the bucket advanced; invariant for all 3 refs | PASS |
| I1-10 | C-9: stale owner's checkpoint CAS while the new owner is at b | `_ADVANCE` IF epoch/next_bucket | `test_c9_stale_owner_cannot_advance` | A's CAS failed, `next_bucket` unchanged; B advanced | PASS |
| I1-11 | C-10: replay of an advanced bucket emits nothing | claim pre-read | `test_c10_…` (checkpoint set back in `media_reader_state`) | 0 claims, 0 commits, 2 replayed; outbox unchanged | PASS |
| I1-12 | Stopped registry never holds back the crawl event path; it catches up later | no group on `media.discovered` (ADR-024) | `test_a_stopped_registry_never_holds_back_the_crawl_event_path` | relay published 40 `media.discovered` (> 64 KiB budget) and 5 `page.observed`, no backpressure; `media.discovered` has no group; extraction group read all 5; registry then resolved all 800 references | PASS |
| I1-13 | Retention gap: halt, metric/alarm, non-zero status, no silent skip, `accept-gap` | `_gap`, `accept_gap`, CLI `resolve status` | `test_gap_halts_alarms_and_needs_accept_gap` + unit `test_gap_halts_and_accept_gap_resumes` | `state=gap`, `next_bucket` unchanged, `media_reader_gap_total 1`, `media_reader_state{state="gap"} 1`, CLI status exit **2**, halted for a second reader; `accept-gap` recorded `accepted_by`/note first, moved the checkpoint, epoch +1; status exit 0 after | PASS |
| I1-14 | Duplicate/concurrent `media.discovered` (producer retry, same locator twice on a page) | claim key (page observation, media) | `test_duplicate_delivery_one_observation_and_provenance` | 4 commits, 3 replayed; one observation per key | PASS |
| I1-15 | Provenance preserved | `_build` | same test | page `UrlRef`, `page_version_id`, `observed_at` = event time, `causation_id` = discovered event, producer `media_registry` | PASS |
| I1-16 | One locator on many pages/domains; token URL → distinct `MediaId` (P8-D2) | `Media.of` (P1) | same test | 2 sightings for one `MediaId` across 2 domains; `?token=a`/`?token=b` → 2 `MediaId`s | PASS |
| I1-17 | Malformed rows do not halt the reader | `_decode` | `test_invalid_rows_are_skipped_and_counted`, unit `test_unsupported_major_is_invalid_not_fatal` | counted `invalid`, bucket still committed and advanced | PASS |
| I1-18 | C-11: two hosts, real lease timing, handover mid-bucket | `Resolver` + Redis `TIME` | `scripts/test-media-two-host.sh` (`resolve-setup`, `resolve`, `resolve-verify`) | host-1 died (`os._exit`, 137) after its 20th claim, before committing it; host-2 stood by 3 cycles, acquired epoch 3, committed host-1's stored claim and finished 3 buckets. 120 observations, 120 physical rows, one `event_id` each, deciders {host-1, host-2} | PASS |
| I1-19 | Probe wanted once per `MediaId`, not per sighting | `ProbeScheduler`, `RedisWork.want` (SET NX) | `test_probe_scheduler_wants_once_per_media`, `test_one_want_per_media_and_stage` | 5 sightings → 1 queued job | PASS |
| I1-20 | Redis `TIME`, leases, per-host spacing and slots | `work.py` | `tests/integration/storage/test_media_work.py` (4 tests) | pass on real Redis | PASS |
| I1-21 | Migration V005 additive; schema checks | `cql/V005__media_registry.cql` | `test_migrations.py` (unit), `test_schema.py` (real Scylla, fresh keyspace, TTL/compaction conventions) | 10 + 5 passed | PASS |
| I1-22 | CLI and settings | `cli.py`, `MediaSettings` | unit `test_cli_parser_and_settings`; CLI `resolve status` exit codes in I1-13 | pass | PASS |
| I1-23 | Coverage of new code ≥ 85 % (B.3), increment 1 | — | coverage command above, 52 tests incl. the outage test | `crawler2/media` **93 %** (781 statements, 44 missed, branch-aware): `cli.py` 73 %, `model.py` 100 %, `resolver.py` 96 %, `schedule.py` 88 %, `store.py` 87 %, `work.py` 98 % | PASS |

Regression on the real stack after adding V005: `test_schema.py`,
`test_media_projections_evidence.py`, `test_outbox_objects.py` — 22 tests;
the first run failed one schema-convention check (a TTL table must use
TimeWindow compaction), fixed in V005 for `encode_claims_by_day`; then all
passed. `make check`: all
passed.

Not claimed by increment 1: throughput per reference (one Paxos round,
one logged batch, one conditional update) is not measured.

## Increment 2 — probing and full-content hashing (2026-10-09)

Fixture tests (`tests/unit/media/test_ssrf.py`, `test_probe_hash.py`) run
the real P4 `HttpFetcher` and the hash reader against a loopback fixture
media web (`tests/fixtures/mediaweb.py`: GET and HEAD, Range, chunked
framing, gzip, redirects, mutable bytes, stalls). The guard sees
public-looking names through an injected resolver (no real DNS), and a
routing transport delivers the bytes from loopback. Worker tests
(`tests/integration/storage/test_media_probe_hash.py`) add real Scylla and
Redis.

| # | Requirement | Implementation | Test / command | Observed | Status |
|---|---|---|---|---|---|
| I2-1 | SSRF guard ported (fingerprinter suite) | `ssrf.py` | `test_ssrf.py`: loopback v4/v6, RFC 1918, fd00::/8, link-local + metadata IP, unspecified, multicast/reserved, 100.64/10, IPv4-mapped, mixed records, DNS failure ≠ SSRF, non-http schemes, allowlist opt-out | 28 passed; nothing reached the inner transport (`NeverCalled`) | PASS |
| I2-2 | Redirect to private / loopback / link-local refused on **both** paths, every hop | `GuardedTransport` in `HttpFetcher(transport=…)` and in the hash reader | `test_redirect_to_private_or_loopback_is_refused_on_both_paths` (4 targets) | probe `forbidden`/`unsafe_destination`; hash `forbidden`, no ContentId; only the public first hop was sent | PASS |
| I2-3 | Public redirects work and are provenance | manual redirects | `test_redirect_between_public_hosts…`, `test_probe_redirect_is_provenance_not_identity` | final URL + hop count stored; `MediaId` stays the locator's (P8-D4) | PASS |
| I2-4 | Range honoured and ignored; P4 64 KiB invariant untouched | P4 `HttpFetcher` unchanged | `test_probe_with_range_honoured…`, `…ignored…` | `Range: bytes=0-65535`; ≤ 64 KiB sent; total length from `Content-Range`, or `Content-Length` of a full response | PASS |
| I2-5 | 403/401/404, non-media, unreachable (probe) | `probe_status` | `test_probe_http_errors`, `test_probe_non_media_and_unreachable`, `test_probe_status_mapping` (9 cases) | forbidden / unreachable / not_media as mapped | PASS |
| I2-6 | Full SHA-256 of known byte sequences | `hashing.py` | `test_full_sha256_of_known_bytes…` | digest = `sha256(body)` with and without Range support; `Accept-Encoding: identity`, no Range, Referer sent; HEAD re-check | PASS |
| I2-7 | Transfer framing and content encoding | `aiter_raw`, `content_encoded` | `test_transfer_framing…`, `test_content_encoding_is_refused…` | chunked body → digest of the content only; gzip despite identity → `content_encoded`, no ContentId | PASS |
| I2-8 | Crafted collision (equal length, equal first/middle/last 64 KiB) → distinct ContentIds | `full-bytes/v1` | `test_crafted_collision_gets_a_distinct_content_id` | samples equal, bytes differ, ContentIds differ | PASS |
| I2-9 | Byte-identical on 3 URLs → one ContentId | — | `test_byte_identical_copies_share_one_content_id` | 1 ContentId | PASS |
| I2-10 | Too large (declared and streamed), timeout, forbidden, unreachable, http_error | bounds in `HashLimits` | `test_too_large…`, `test_timeout_forbidden_unreachable_http_error` | declared: 0 bytes read; streamed: stops at the bound; no ContentId in every failure | PASS |
| I2-11 | Content changed since the probe and **during** the read | validators before + HEAD after | `test_content_changed_since_the_probe_or_during_the_read` | both `changed_during_read`, no ContentId | PASS |
| I2-12 | Manifests probed for hints, never hashed; `unknown` hashed only when the probe shows media | `schedule.hashable` | `test_manifest_is_probed…`, `test_unknown_kind…`, `test_hashable_kinds` (9 cases) | as designed (P8-D6) | PASS |
| I2-13 | Facts with provenance, latest view, M5/M6 from hashes | `store.record_probe/record_hash` | `test_probe_then_hash_writes_facts_latest_and_m5_m6` (real Scylla/Redis) | probe and hash rows (worker, final URL, status); latest view; M5 row; M6 version | PASS |
| I2-14 | Same URL, changed bytes → new ContentId and M6 version; stale hash not attached | re-probe → validators differ → re-hash | `test_same_url_changed_bytes…`, `test_stale_hash_is_not_attached_to_new_observations` | 2 ContentIds, 2 M6 rows; between re-probe and re-hash `current_content()` is None and the resolver's new observation has hints but no content key | PASS |
| I2-15 | One probe and one hash per media under concurrency | per-media Redis leases + freshness re-check | `test_one_probe_and_one_hash_per_media_under_concurrency` (4 concurrent handlers, two worker identities) | 1 probe row, 1 hash row | PASS |
| I2-16 | Observation and hash paths commute; rebuild repairs lost derived writes | P2 statements and timestamp rules | `test_hash_and_observation_paths_commute…`, `test_rebuild_repairs_lost_derived_writes` | M6 first_seen = earliest of both, last_seen = latest; after losing the derived writes, `rebuild` restores the latest view, M5, M6; idempotent | PASS |
| I2-17 | Per-host hash slot: a busy host's job is put back; CLI `show`/`rebuild` | `HashWorker`, `cli.py` | `test_busy_host_requeues_and_cli_show_rebuild` | job requeued, hashed after the slot was free; CLI outputs | PASS |
| I2-18 | Probe/hash pool survives storage errors and bad jobs | `run_pool` | `test_pool_survives_storage_errors_and_bad_jobs` | failing job wanted again; others handled | PASS |
| I2-19 | Two hosts: probe and hash workers on both | `media_multihost.py` `serve-media`, `workers`, `workers-verify` | `scripts/test-media-two-host.sh` | 18 media (6 groups × 3 copies) served from app-host-1 through the guard (compose subnet allowlisted): exactly 1 probe and 1 `ok` hash per media; host-1 9 probes / 8 hashes, host-2 9 / 10; 6 ContentIds, one per group | PASS |
| I2-20 | Coverage of new code ≥ 85 %, increment 2 | — | coverage command, 115 tests | `crawler2/media` **92 %** (1,270 statements, 96 missed): `hashing.py` 92 %, `probe.py` 92 %, `ssrf.py` 91 %, `store.py` 93 %, `cli.py` 67 % | PASS |

Not claimed by increment 2: hash throughput, bandwidth and the 8 GiB /
3,600 s bounds on real media (approved assumptions, not measured); DNS
rebinding protection (P14).

## Increment 3 — encode requester and representation projection (2026-10-09)

Tests: `tests/integration/storage/test_media_encode.py` (real Scylla and
Redis), `tests/unit/media/test_encode_unit.py`, and the two-host stages.

| # | Requirement | Implementation | Test / command | Observed | Status |
|---|---|---|---|---|---|
| I3-1 | Claim race between two requesters → one logical request | `encode_claims` LWT, event id allocated first | `test_two_host_claim_race_has_one_logical_request` (both released together after the index write) | one `emitted`, one `claimed_elsewhere`; 1 outbox row, the claim's `event_id`; projection `requested` with that `event_id` | PASS |
| I3-2 | Byte-identical content → one claim, later requests suppressed | step 1 + claim | `test_byte_identical_media_one_claim_then_suppressed` | `emitted`, `suppressed`, `suppressed`; 1 claim, 1 event | PASS |
| I3-3 | Crash after claim, before the outbox write; sweeper re-emits the **same** `event_id` | `sweep`, stored envelope | `test_crash_after_claim_before_outbox_then_sweeper_reemits_same_event` | before grace: pending, 0 rows; another media: `claimed_elsewhere`; after grace: 1 re-emission with the stored `event_id`, referer kept, `required_spec` absent; `emitted_at` set; next sweep 0 | PASS |
| I3-4 | Crash after the outbox write, before `emitted_at` (twice) | conditional `emitted_at` | `test_crash_after_outbox_before_emitted_redelivers_one_event` | redeliveries carry one `event_id` and identical bytes; finally emitted | PASS |
| I3-5 | `ready` suppresses; `encode.failed` recorded, no alternative-source re-request (P8-D7) | `_suppressed`, projection | `test_ready_suppresses_and_failed_never_re_requests` | ready → no claim; failed → state `failed`; a second source → `claimed_elsewhere`, still 1 event | PASS |
| I3-6 | Crash between an ok hash and its claim is recovered | `ensure` via the resolver sink | `test_ensure_recovers_a_crash_between_hash_and_claim` | request created once; a second `ensure` emits nothing | PASS |
| I3-7 | `representation.ready` / `encode.failed` through the streams, repeated delivery applied once | `RepresentationLoop` + `IdempotentConsumer` | `test_representation_events_through_streams_are_applied_once` (each event delivered twice, plus one malformed entry) | projection `ready` (supersedes the earlier failure); 2 applied, 2 duplicates; malformed acknowledged; nothing pending | PASS |
| I3-8 | Loop/sweeper failure branches; CLI `encode sweep`, `representations run` | `encode.py`, `cli.py` | `test_loop_acks_invalid…`, `test_sweep_forever…`, `test_cli_encode_sweep_and_representations_run` (private stream prefix and group) | invalid acked, storage failure left pending; sweep retries; CLI re-emitted 1 and applied ready | PASS |
| I3-9 | Two hosts: concurrent encode claims | `media_multihost.py encode-race/encode-verify` | `scripts/test-media-two-host.sh` | 30 contents requested by both hosts at once (each with its own source media): 30 claims (host-1 14, host-2 16), host-1 14 `claimed_elsewhere` + 2 `suppressed`, host-2 11 + 3; exactly one `event_id` per content in the outbox | PASS |
| I3-10 | Two hosts: hash workers on both hosts issue encode requests for real hashes | `workers` stage with requester | same script, `workers-verify` | 6 ContentIds → 6 claims, claims decided by both hosts, one `event_id` each | PASS |
| I3-11 | Coverage ≥ 85 %, increment 3 | — | coverage command with `P8_SCYLLA_OUTAGE=1`, 126 tests | `crawler2/media` **92 %** (1,516 statements, 104 missed): `encode.py` 93 %, `cli.py` 73 % | PASS |
