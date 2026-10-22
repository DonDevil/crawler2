# P11 validation

Date: 2026-10-22, dev host (compose stack `crawler2-*` up: Scylla, Redis,
MinIO). Every result below was produced by the command shown in this session.

## 1. Commands and outcomes

| Tier | Command | Result |
|---|---|---|
| Lint / format | `env/bin/ruff check .` · `env/bin/ruff format --check .` | pass |
| Types | `env/bin/mypy` (strict, 254 files) | pass |
| Unit + contract (whole repo) | `env/bin/pytest` | **796 passed**, 159 skipped (integration, no stack flag), 45.1 s (final run) |
| P11 tier (unit + real stack) | `scripts/test-feedback.sh -s` | **24 passed** (22 unit, 2 integration), 24–34 s |
| Regression, real stack | `scripts/test-feedback.sh tests/integration/storage/test_schema.py tests/integration/storage/test_media_projections_evidence.py tests/integration/storage/test_filter_discovery.py tests/integration/storage/test_source_projection.py tests/integration/fingerprinter` | **42 passed**, 445 s (includes the P11 tier; P7 admission/failure injection, P9 cross-service, schema with V006) |
| CLI smoke, real stack | `python -m crawler2.intelligence.cli feedback show` / `feedback rebuild` / `feedback show --url …` on keyspace `crawler2_it_p11_host` after the replay | rebuild `{"applied": 6, "dropped_sources": 2, "unattributed": 0}`; assessments identical before and after |

Not run: the full `make validate-stack` (both compose profiles, all
integration areas; P11 does not touch fetch workers, extraction, media
resolver or object storage), the fingerprinter's own test suite (no
fingerprinter file changed), a real-media or live-internet crawl.

## 2. Real vs simulated boundaries in the integration tests

`tests/integration/storage/test_feedback_loop.py`:

| Real | Simulated (labelled) |
|---|---|
| Redis Streams publish/consume (`RedisStreamPublisher`, consumer groups) | **Fetch**: a fixture site oracle decides what a page carries per capability (`alpha` renders its player only in a browser; `beta` serves media on http; six `dud` hosts carry unrelated media) |
| `IdempotentConsumer` with Scylla processed-event markers | **Fingerprinter encode + match**: an oracle publishes a contract-valid `match.found` (`MatchResult` with derived ids) for matching content |
| Media registry M1–M6 and page observations W4 writes/reads | Workers: capability-flexible claim loop (2 slots/tick) |
| P2–P5 projections, P11 V006 tables, snapshot loader, rebuild | |
| P6 `Admitter` (filter, revisit, priority) and P3 `RedisFrontier` admit/claim order (controllable clock) | |

## 3. M4 replay (deterministic: fixed run token, seed 1101, fixed timestamps)

Population: 8 hosts (2 productive, 6 comparable unproductive), 2 targets
(A on `alpha`, B on `beta`). Cycle 1 (T1 = 2026-10-20 09:00Z) crawls 4 pages
per host (alpha: 2 http + 2 browser) → 6 matches (alpha's 2 http fetches
answered 200 with no media: **no evidence**). Cycle 2 (T1 + 6 h) offers 8 new
pages per host (64) through the real `Admitter`; budget 2 claims/tick × 8
ticks = 16 claims in both arms.

Output line `P11-M4-REPLAY` (identical across two consecutive runs):

| Measure | Feedback off | Feedback on |
|---|---|---|
| Effective priority alpha / beta / dud (base 50) | 50 / 50 / 50 | **58 / 58 / 50** (source 6 + pattern 2) |
| Claims (budget) | 16 | 16 |
| Claims per host | alpha 1, beta 2, dud1 2, dud2 1, dud3 2, dud4 2, dud5 4, dud6 2 | alpha 8, beta 8 |
| Queues used | http | browser (alpha), http (beta + alpha's re-test share) |
| **Matching media reached by cycle 2** | **2** | **15** |

The one alpha claim without media is a URL in the 20 % http re-test share
(exploration working as designed, not a false positive).

Evidence capability: alpha `{browser}`, beta `{http}`; no evidence for any
dud host. Rebuild inside the test: `{"dropped_sources": 2, "applied": 6,
"unattributed": 0}` and identical evidence sets.

Decay (same evidence, no new matches), alpha effective priority by age:
0 d 58 · 14 d 53 · 30 d 51 · 60 d 50 · 91 d 50. At 91 d the cycle-2 claim
sequence is **identical** to feedback off (2 matching media).

No-match control (`P11-M4-CONTROL`): same population, no `match.found`
published → empty evidence, cycle-2 claim sequence identical with feedback
on and off (16 claims each, 0 matching media in this run's sample).

## 4. Unit coverage map (`tests/unit/intelligence/test_feedback.py`, 22 tests)

| Requirement | Test |
|---|---|
| Consumption updates the right source; attribution via registry, page host not CDN; template, kind, capability | `test_match_is_attributed_to_the_pages_that_carried_the_media` |
| Idempotency (redelivery, producer retry with new event id, crash before marker) | `test_redelivered_match_is_counted_once_and_changes_nothing` |
| No provenance → no feedback | `test_unattributable_match_gives_no_feedback` |
| Pattern learning with support threshold | `test_pattern_needs_support_and_applies_to_its_template_only` |
| Capability: browser only when only browser reached media; http evidence wins; exploration share ~20 %; decay retires it | `test_browser_preferred_only_when_only_browser_reached_media`, `test_browser_preference_fades_with_decay` |
| Target relevance bounded | `test_target_relevance_weights_within_bounds`, `test_target_events_drive_relevance_in_the_snapshot` |
| Bounds under 2,000 targets; NaN/out-of-range confidence | `test_boost_is_bounded_whatever_the_evidence`, `test_nan_and_out_of_range_confidence_add_nothing_beyond_bounds`, `test_one_target_many_observations_counts_once` |
| Decay to neutral | `test_decay_moves_an_old_productive_source_to_neutral` |
| Stability: 480 evaluations over 120 days, no order flip among live hosts, nothing grows, expired stays neutral, pure re-evaluation | `test_rankings_do_not_change_without_new_evidence` |
| Cold source eligible, never lowered | `test_cold_sources_keep_base_and_feedback_never_lowers` |
| Failure: storage down never marks processed; loop acks malformed, leaves outage pending | `test_storage_outage_is_never_a_successful_update`, `test_loop_acks_malformed_events_and_leaves_storage_failures_pending` |
| Admission consumes effective priority/queue; disabled, never loaded, stale, raising, invalid → static | `test_admission_uses_the_effective_priority_and_capability`, `test_admission_falls_back_to_static_priorities` |
| Rebuild reproduces evidence | `test_rebuild_reproduces_evidence_from_match_projections` |
| Settings bounds, default off | `test_operator_bounds_come_from_settings`, `test_feedback_is_off_by_default` |

Concurrency: two consumers writing the same match produce identical upserts
(keyed rows, deterministic values); exercised as sequential double-apply and a
second consumer pass in the replay, not as truly parallel processes.

## 5. Performance

Not benchmarked beyond the tests: the admission hot path is an in-memory dict
lookup plus one `tokenize_url` per URL and an assessment cached per host and
minute; the snapshot reads one partition per productive host
(`LIMIT 500`). The 64-URL admission in the replay is not a performance
measurement.

## 6. Limitations

- The matcher and the network fetch are simulated (§2); P10 M3 is NOT PASSED.
- No measurement on real crawl data (P7's M1 dataset holds no matches).
- No multi-process concurrency test; no multi-host claim.
- Effort denominator not implemented (decisions P11-D14).
