# Crawl intelligence (current state)

P7 decides what, when and how to crawl **outside** the crawler. Full
design, evidence and gates: [P7 documents](../phases/p07-crawl-intelligence/).

**Implemented (increment 1):** `crawler2/intelligence/sources.py`. This is
a pure library for source health and fetch profiles:

- a bounded per-host profile folded from `fetch.completed` facts
  (order-independent decayed weights);
- lifecycle states: unknown, active, degraded, moved, blocked,
  unreachable, error;
- probe-only advice with a backoff of 6 h to 7 d, and reactivation on one
  success;
- the starting capability: http, or browser on repeated `needs_js`
  evidence, with a weekly http re-test.

**Implemented (increment 2, [ADR-023](../adr/ADR-023-projection-readers-outside-stream-retention.md)):**

- `crawler2-intel sources run`: one reader folds every `fetch.completed`
  from the **outbox** (read-only, its own checkpoint, no Redis consumer
  group) into `intel_source_profiles`. A stopped reader cannot hold back
  the relay (ADR-022 unchanged). After a gap longer than the outbox's
  14 days, or a policy change, or an operator `rebuild`, it starts a new
  generation from the retained facts.
- P6 admission asks P7 for source advice **after** its own filter, scope
  and revisit gate. P7 can defer a URL of a host in a negative state (one
  probe per interval) and choose the browser starting queue. It cannot
  admit what P6 rejected, and priorities stay P6's.
- Advice reads an in-memory snapshot refreshed in the background, so there
  is no I/O per URL. **Off by default** (`intelligence.advice_enabled`).
  When off, empty, stale, rebuilding or unreachable, admission is today's
  P6 behaviour (plan B.5 #1).

**Implemented (increment 3, offline only, [what.md](../phases/p07-crawl-intelligence/what.md)):**
`crawler2/intelligence/what.py` is a pure library over P5 page facts:

- per-host URL templates and their role (content, mixed, spam, listing);
- page kind (hub/detail/other);
- page relevance (relevant/spam/unknown), with reason codes and a
  versioned operator vocabulary.

It has no table, consumer, setting or runtime caller. The crawler's
behaviour is unchanged.

**Planned, in review order:**

1. WHEN: hub-novelty recrawl over WHAT's hub and content-link features,
   gated on a signal validated on a new labelled hub-revisit sample (G5
   failed for `page.changed`). Readiness audit ([when.md](../phases/p07-crawl-intelligence/when.md)):
   M1 has 6 candidate hub revisits against the 50 required, so a hub-only
   collection is proposed (not run). There is no WHEN code;
2. one priority function.

Frozen P7 boundaries: [decisions.md §4](../phases/p07-crawl-intelligence/decisions.md).

Target relevance waits for P9; Tor profiles wait for a Tor deployment.
