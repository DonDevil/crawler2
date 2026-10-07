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

**Not wired:** nothing calls it at runtime yet, and it has no storage and
no consumer. The crawler runs exactly as in P6 (plan B.5 #1).

**Planned, in review order:**

1. a P7-owned `source_profiles` table, an `intel-*` best-effort
   `fetch.completed` consumer (an ADR is needed for the ADR-022
   interaction), and advice at P6 admission;
2. WHAT: page kinds, URL templates, spam as negative knowledge;
3. WHEN: hub-novelty recrawl, gated on a validated signal (G5 failed
   for `page.changed`);
4. one priority function.

Target relevance waits for P9; Tor profiles wait for a Tor deployment.
