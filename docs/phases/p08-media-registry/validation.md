# P8 Validation — media registry & identity

Status: **in progress.** Increment 1 (resolver) is implemented and
validated below. Increments 2–3 and the exit gate are not yet run; their
rows are added as they are. Design: [design.md](design.md). Decisions:
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
