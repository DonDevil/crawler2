# P11 audit — what the feedback loop could build on

Date: 2026-10-22. Scope: only the components the loop touches. Repositories:
crawler2 `main` at `08ac92c` (clean), fingerprinter `p10-impl` at `6be5fad`
(user changes in `.vscode/settings.json` and one untracked bench result; not
touched — P11 needed no fingerprinter change).

## 1. Existing components and what was reused

| Need | Found | Runtime-wired before P11? | P11 use |
|---|---|---|---|
| `match.found` | `MatchFound{match: MatchResult}` (contracts, schema 1.0): `match_id`, `content_id`, `target` (id + version), technique, verdict (`confirmed`/`probable`), confidence, `decided_at`. **No URL, page or host** — by design "crawler2 maps it back … through its own registry". Catalog consumers: `FEEDBACK`, `EVIDENCE_COLLECTOR`; idempotency key `match.match_id` | Produced by the fingerprinter's P10 matcher on `events:match.found.v1` (same prefix and `envelope` field as crawler2's `stream_name`). **No crawler2 consumer existed.** | Consumed as is; no contract change |
| Content → pages | M5 `media_by_content` (content → media), M3 `media_observations_by_media` (media → page observation, page URL, content seen there), W4 `page_observations` (→ `capability`) | Written by P8 | Attribution path (read only) |
| Match projections | `ProjectionRepository.record_match` → P3 `matches_by_content`, P4 `matches_by_target`, P5 `matches_by_domain`; P2 `targets` via `apply_target_registered/retired` | Implemented and tested (P2), **never called at runtime** | P11 consumer is the first runtime writer; P3/P4 are also the rebuild source |
| Consumer plumbing | `RedisStreamReader` (consumer groups, `XAUTOCLAIM`), `IdempotentConsumer` + Scylla `processed_events` (X3, marker after success) | Used by P5, P6, P8 consumers | Reused unchanged |
| Source profile / fetch profile | P7 `SourceProfile` (health per host from `fetch.completed`), `capability()` (http vs browser from `needs_js` evidence), `SnapshotAdvisor` (in-memory snapshot, never raises, fallback) | Advice behind `intelligence.advice_enabled` (off) | Not modified. P11 mirrors the snapshot/fallback pattern; P7's informed capability advice wins over P11's |
| URL patterns / page types | P7 WHAT `tokenize_url` → `UrlShape.template` and `structural` (listing-like) | Pure library, nothing in runtime uses it | Reused for the template and a detail/listing page kind |
| Priority semantics | P1 `Priority` 0..100, **higher = more urgent**, default 50. P6 static priorities: seed 70, search 60, link 50, leaf 40. P3 Lua rank `(100 - priority) * BAND + seq` per domain queue; the ready index orders domains by head rank; per-domain politeness gate and in-flight limit (ADR-019) | P6 `Admitter` → `RedisFrontier.admit_many` | P11 boosts the priority inside `Admitter` (the real path) |
| Target relevance | P2 `targets` projection (`TargetState.active`) | Not populated at runtime | P11 consumer populates it from `target.registered/retired` |

## 2. What was missing

1. A `match.found` consumer and any source attribution of a match.
2. Any persisted link from a match to the page template, page kind or the
   capability that reached the media.
3. A priority input: P6 priorities were static and P7's WHEN (the planned
   priority/scheduler path) is **deferred, not built** (P7 D-22). There was no
   scheduler hook to extend, so P11 adds the narrowest one: an optional
   `prioritizer` on the P6 `Admitter`.
4. Runtime population of the P2–P5 projections.

## 3. Boundaries

- crawler2 only. The fingerprinter (owner of `match.found`, targets and
  representations) is unchanged; P10 behaviour, P9 identity, P8 contracts and
  the shared contract package are untouched.
- No cross-keyspace reads: attribution uses crawler2's own registry only.
- No P7 gate is reopened; WHEN stays deferred.
- New state is P11-owned and derived (migration V006, two tables).

## 4. Risks and assumptions

| Risk | Handling |
|---|---|
| A match arrives before its media is hashed (M5 empty) | Counted `unattributed`, no feedback; the match is still projected and `feedback rebuild` re-attributes later |
| Same bytes on many pages/targets inflate value | Value is the max per target, summed over distinct targets, then saturated (design §5) |
| One host monopolises the frontier | Boost ≤ 20; P3's per-domain politeness gate and in-flight limit still bound claims per host |
| Browser lock-in after one success | Needs ≥ 1.0 decayed browser evidence, no comparable http evidence, and 20 % of URLs stay on http |
| Feedback storage down | Snapshot keeps the last good copy and ages into fallback (static priority); consumer leaves events pending |
| P10 M3 is **not passed** | P11 does not depend on calibration: it consumes any contract-valid `match.found` and weights `probable` at 0.5 |

## 5. Upstream limitations affecting M4

- P10 M3 NOT PASSED (fingerprinter `docs/phases/p10-vector-index-matcher/README.md`,
  2026-10-21): equivalence recall fails; E-R1 index latency blocked on this
  host. The matcher is therefore simulated in the M4 replay (validation §2).
- P4 live fetching of external sites is not a test prerequisite (and must not
  be); fetches are a fixture oracle in the replay.
- P7 WHEN deferred: there is no time-of-day/revisit scheduling to feed.
