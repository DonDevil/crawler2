# P11 design — match feedback (as implemented)

Status: implemented 2026-10-22. Code: `crawler2/intelligence/feedback.py`
(pure model), `feedback_store.py` (V006 tables), `feedback_service.py`
(consumer, snapshot, prioritizer, rebuild), `crawler2/discovery/admission.py`
(the hook), `crawler2-intel feedback …` (CLI). Operator guide:
[../../architecture/match-feedback.md](../../architecture/match-feedback.md).

## 1. Event flow and ownership

```
fingerprinter (P10 matcher) ──match.found.v1──┐
fingerprinter (target manager) ─target.*.v1───┤  Redis Streams, group "crawl-feedback"
                                              ▼
crawler2-intel feedback run  (IdempotentConsumer + Scylla processed_events)
   ├─ target.registered/retired → P2 targets projection (existing)
   └─ match.found → attribution (M5 → M3 → W4, crawler2-owned)
                    ├─ P3/P4/P5 match projections (existing record_match)
                    └─ B1 feedback_evidence + B2 feedback_sources (P11, V006)
                                              ▼
crawler2-discover (Admitter) ── FeedbackPrioritizer (in-memory snapshot, refreshed
   in a daemon thread) ── effective priority / queue ──► RedisFrontier.admit_many
                                              ▼
P3 claim order: higher priority heads first, per-domain gates unchanged
```

The fingerprinter owns matching and targets; crawler2 owns the registry,
the evidence and the priorities. No synchronous call crosses the boundary
and the fetch path never touches feedback (only admission does, in memory).

## 2. Authoritative vs derived

| State | Authority |
|---|---|
| `match.found`, `target.*` events | fingerprinter (authoritative facts) |
| M3/M5, W4 | crawler2, authoritative (P8/P2) |
| P2–P5 projections | derived from fingerprinter events (existing) |
| B1/B2 evidence | **derived**, rebuildable from P3/P4 + M3/M5 + W4 (§8); TTL 90 d |
| Snapshot, assessments | process memory, recomputed |

## 3. Attribution (`MatchFeedback.attribute`)

For a `MatchResult`: `media.by_content(content_id)` (≤ 50 media) →
`sightings(media, decided_at − 90 d … decided_at + 1 d, limit 50)` →
for each sighting whose observed content equals the matched content (a
locator that served other bytes on that page is skipped), the **page** URL's
host is the source (never the media/CDN host), `tokenize_url(page)` gives the
template and `structural` gives page kind `listing`/`detail`, and
`pages.get(observation)` gives the fetch `capability`. One evidence row per
(host, match, page observation). No row ⇒ result `unattributed` (no
feedback); the match is still recorded in P3–P5.

## 4. Storage

V006 (`crawler2/storage/scylla/cql/V006__match_feedback.cql`):
- B1 `feedback_evidence ((domain_id), match_id, page_observation_id)` — the
  row's values are a pure function of the match and the registry, so replays
  and concurrent consumers upsert identical cells (no LWT needed).
- B2 `feedback_sources ((bucket), domain_id)` — single partition listing hosts
  with evidence (bounded by productive hosts), read by the snapshot.
- Snapshot read: B2 once + one bounded partition read per host (`LIMIT 500`).
All writes `DERIVED_WRITE`, reads `EVENTUAL_READ` (P1 conventions).

## 5. Scoring (`feedback.py`, policy `feedback/v1`)

Strength of one row at time `now`:
`s = clamp(confidence, 0, 1) × (1 if confirmed else 0.5) × exp(−age/τ)`,
τ = 14 days; 0 beyond 90 days; NaN → 0; future timestamps count as fresh.

Host value: `V = Σ_targets relevance(target) × max_rows_of_target(s)` with
relevance active 1.0, unknown (no `target.registered` yet) 0.5, retired 0.1.

Boost: `round(15 × (1 − exp(−V/2)))`, 0 when `V < 0.05` (neutral). One fresh
confirmed target → 6, two → 9, three → 12, saturating at 15.

Pattern boost: for each template with ≥ 2 distinct carrying pages,
`round(5 × (1 − exp(−V_template/2)))`; applied only to a URL with that
template. Total boost ≤ 20; effective priority = `clamp(base + boost, 0, 100)`.

Properties (tested): never lowers a priority (cold hosts keep the base, which
is the exploration guarantee); repeated matches or observations cannot grow
the boost past 15/5/20; every term decays by the same factor, so without new
evidence the order of hosts by value never flips, boosts only shrink, and an
expired host stays neutral; evaluation is a pure function of
(evidence, targets, minute).

## 6. Capability preference and fallback

Per host, the decayed strength per distinct carrying page is summed per
capability. `browser` is preferred only if browser weight ≥ 1.0 **and** http
weight < 0.25 × browser weight (an http page that carried matching media
proves the cheap path works). Even then 20 % of URLs (blake2b of `url_id`,
stable) stay on http so a site that stops needing the browser is noticed;
decay retires the preference (two browser pages fall below 1.0 after 14 days).
An HTTP 200 without matching media produces no evidence at all — only a match
is evidence of "reached real media". Admission applies the preference only
when P7 gave no informed advice for the host (P7's capability logic, with its
own http re-test, wins).

## 7. Runtime wiring and failure semantics

- `Admitter(prioritizer=…)`: after filter, scope, revisit and P7 advice,
  `priority = prioritizer.prioritize(url, static_priority, now).priority`.
  Any exception or an out-of-range result → static priority (logged).
- `FeedbackPrioritizer`: snapshot loaded in a daemon thread every 60 s;
  missing → `fallback=unavailable`; older than 15 min → `snapshot_stale`;
  refresh failure keeps the old snapshot (it ages into fallback). Assessments
  cached per (snapshot, minute).
- `crawler2-discover` builds it only when `CRAWLER2_FEEDBACK__ENABLED=true`
  (**default false**: admission is byte-for-byte the P6/P7 behaviour).
- `FeedbackLoop`: malformed/invalid events are logged, counted
  (`feedback_matches_total{result="invalid"}`) and acknowledged (they can never
  become valid); a storage error stops the batch with the entry unacknowledged
  (redelivered via `XAUTOCLAIM` after `claim_idle_ms`), the processed marker is
  written only after success. A crash between write and marker re-applies
  identical rows.

## 8. Rebuild

`crawler2-intel feedback rebuild [--months 3]`: drop B1/B2, then for every
target in P2, every P4 match summary of the last N months, re-read the full
`MatchResult` from P3 and re-run attribution. Tested to reproduce identical
evidence (unit and real stack). Limits: matches older than the window, or of a
target missing from P2, are not replayed (they would be ≈ 0 after decay).

## 9. Metrics

`feedback_matches_total{result=applied|unattributed|invalid|storage_error}`,
`consumer_events_total{consumer="crawl-feedback",outcome=applied|duplicate}`
(existing), `feedback_evidence_rows_total`,
`feedback_priority_total{result=boosted|neutral|unavailable|snapshot_stale|error}`,
`feedback_snapshot_failures_total`.

## 10. Configuration

`FeedbackSettings` (`CRAWLER2_FEEDBACK__*`): `enabled` (false),
`consumer_group`, `batch_size`, `block_ms`, `claim_idle_ms`,
`snapshot_refresh_s` (60), `max_snapshot_age_s` (900), `tau_days` (14),
`max_source_boost` (15), `max_pattern_boost` (5), `max_total_boost` (20),
`explore_percent` (20). Non-default bounds change the policy version to
`feedback/v1+custom`; inconsistent bounds fail at start.

## 11. Trade-offs

- Additive boost, never a penalty: failure evidence stays P7's job (whose
  states always re-probe); P11 cannot condemn a host.
- Target relevance weights evidence by the target's state; there is no
  per-URL "which target is this page about" ranking (WHAT relevance is not
  persisted and P11 does not build a target-ranking service).
- The effort denominator (fetches per match) is not used: P7 profiles are
  decayed weights without per-host attempt counts for the same window, and a
  ratio over tiny counts would be noisy. Recorded as a follow-up.
- Pattern support counts distinct pages, not distinct targets: two pages of
  one target on one template already show the template carries media.
