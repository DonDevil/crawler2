# ADR-020 — Page revisions: meaningful change as an additive identity beside page versions

Status: Accepted (P5 design review, 2026-09-29). Additive to ADR-008 and
ADR-009; supersedes nothing.

## Context

P1 derives `PageVersionId` from `(final url_id, body_digest)`: one exact-bytes
state of a page. P4 computes it when it records an observation, and P2
keeps first/last seen per raw version (W8). Links (W9) and media (M4) are
keyed by it. The plan's P5 rule is different: a page *version* exists only
when the **normalized** content changes, so ad rotation, tracking markup
and timestamps do not create versions. Redefining `PageVersionId` would
change a frozen ID derivation and a field's meaning (plan B.5 #5, ADR-008),
and it would force P4 to parse pages before recording them.

## Decision

1. `PageVersionId` keeps its P1 meaning (exact bytes). Nothing that uses
   it changes.
2. Contract 1.1 adds **`PageRevisionId`** (`pgr_`, derived from final
   `url_id`, normalization scheme and normalized digest). A revision is
   the P5 "page version": one meaningful content state of a URL.
3. A revision is created on the first sighting of its normalized state.
   Later sightings move only `last_seen`/`last_observation_id`
   (latest-wins write timestamps). Concurrent writers derive the same ID
   and converge on one row; there is no check-then-insert on the row.
4. Contract 1.1 adds the event **`page.changed`** (producer `extraction`,
   consumer `crawl_intelligence`, idempotency key `revision_id`) and the
   value model `PageHashes`. The event is emitted only for the first
   sighting of a revision. A concurrent first sighting may emit it twice,
   and consumers deduplicate on the key. `page.observed` and
   `fetch.completed` are unchanged, and there is no `page.fetched`.
5. The normalization scheme is part of the identity (`html-normalized/v1`).
   Changing the normalizer means a new scheme, so revisions under two
   schemes never compare equal by accident.

## Consequences

- A revert (A → B → A) re-sights A and creates no new revision. Counting
  transitions needs ordered allocation (LWT or a lock), which P2 uses
  only for evidence. Intelligence sees the revert through `last_seen`.
- Two identity levels exist for a page's content: the raw version (evidence,
  exact bytes, snapshot) and the revision (meaningful change). Docs call the
  first a "page version" and the second a "revision".
- A normalizer upgrade creates new revision IDs for every page on its next
  sighting (one `page.changed` each). This is expected and documented, not
  a real content change.
