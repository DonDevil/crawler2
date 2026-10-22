# Match feedback (P11) — operator guide

What it changes: when enabled, P6 admission raises the static priority of
URLs on hosts whose pages carried media the fingerprinter matched to a
target (by at most 15), and of URLs following a productive template on that
host (by at most 5 more; total ≤ 20), and may start such a host on the
browser queue when only browser fetches reached its media. It never lowers a
priority, never admits or rejects a URL and never touches fetch workers.
Design: [P11 design](../phases/p11-feedback-loop/design.md).

## Enable / disable

1. `crawler2-storage migrate` (adds V006: `feedback_evidence`, `feedback_sources`).
   Processes built from this code refuse a keyspace without V006
   (`require_current`), so migrate when the new image is deployed.
2. Start the consumer (any number of instances; consumer group
   `crawl-feedback`): `crawler2-intel feedback run`. It also fills the P2
   target and P3–P5 match projections. It can run with admission disabled.
3. Admission: `CRAWLER2_FEEDBACK__ENABLED=true` for `crawler2-discover`.
   Disable by unsetting it (restart): admission is then exactly P6/P7.

Bounds: `CRAWLER2_FEEDBACK__MAX_SOURCE_BOOST`, `__MAX_PATTERN_BOOST`,
`__MAX_TOTAL_BOOST` (≤ 50), `__TAU_DAYS`, `__EXPLORE_PERCENT`,
`__SNAPSHOT_REFRESH_S`, `__MAX_SNAPSHOT_AGE_S`.

## Inspect

- `crawler2-intel feedback show` — every host with evidence: value, boost,
  pattern boosts, capability weights, targets, matches, last match.
- `crawler2-intel feedback show --url URL [--base 50]` — that host's
  assessment and the URL's effective priority with the reason.

## Reset / rebuild

`crawler2-intel feedback rebuild [--months 3]` drops all P11 evidence and
re-derives it from the match projections and the media registry. Evidence is
derived; rows expire after 90 days.

## Diagnose

| Symptom | Check |
|---|---|
| No boosts | `feedback_priority_total{result}`: `unavailable`/`snapshot_stale` = snapshot not loading (`feedback_snapshot_failures_total`, logs `feedback_snapshot_failed`); `neutral` = no evidence for those hosts |
| Consumer stalled | `XINFO GROUPS events:match.found.v1` (pending, lag for `crawl-feedback`); logs `feedback_storage_unavailable` (entries stay pending and are reclaimed after `claim_idle_ms`) |
| Matches but no evidence | `feedback_matches_total{result="unattributed"}`: the content is not (yet) in the media registry (M5); run `feedback rebuild` later |
| Bad events | `feedback_matches_total{result="invalid"}`, logs `feedback_event_invalid` (acknowledged, not retried) |
| Duplicates | `consumer_events_total{consumer="crawl-feedback",outcome="duplicate"}` |

## Known limitations

Matcher quality is P10's (M3 not passed); `probable` matches weigh half.
No effort-normalised productivity. P7 informed capability advice overrides
P11's browser preference. M4 status: [exit](../phases/p11-feedback-loop/exit.md).
