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

**Planned, in review order:**

1. WHAT: page kinds, URL templates, spam as negative knowledge (its own
   labelled sample, not the G5 labels);
2. WHEN: hub-novelty recrawl, gated on a validated signal (G5 failed
   for `page.changed`);
3. one priority function.

Target relevance waits for P9; Tor profiles wait for a Tor deployment.
