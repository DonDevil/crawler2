# Filter engine & discovery (current state)

P6 classifies requests and links with explicit, provenance-carrying rules
and turns seeds, search results and discovered links into frontier
admissions. It classifies and applies stated policy; it does not score,
learn or prioritise (P7). Full design and measurements:
[P6 phase documents](../phases/p06-filter-discovery/). Decision:
[ADR-021](../adr/ADR-021-filter-rules-and-admission.md).

## The M1 loop

```
 seed file ─ crawler2-discover seeds ──┐      operator queries ─ crawler2-discover search ─┐
                                       ▼                                                    ▼
                       filter (LINK) → scope → revisit gate → Frontier.admit_many (P3) ◄───┘
                                                              │ claim
                                                              ▼
            crawler2-worker (P4) — browser sub-requests → FilterInterceptor (REQUEST context)
                                                              │ page.observed (outbox → relay)
                                                              ▼
                                   crawler2-extract (P5) — links as facts → urls.discovered
                                                              │
                                                              ▼
       crawler2-discover admit: redirect chain (REDIRECT context) → per-link filter → scope
       (rooted site: follow; external: fetch once as a leaf) → revisit gate → admit_many
                                                              │
                                                              └──► frontier … next crawl
```

## Rules and decisions

| Concept | Where |
|---|---|
| classes | CONTENT, MEDIA, PLAYER, NAVIGATION, AD, TRACKER, UNKNOWN |
| actions | ALLOW, CLASSIFY (labelled, proceeds), BLOCK |
| rule kinds | host, host label, path, ABP URL pattern, redirect, selector (normalization, operator only) |
| sources | built_in, v1_blacklist (reviewed manifest), easylist, easyprivacy, ublock, operator |
| precedence | operator explicit action → ABP exception / `$important` → kind specificity → longer match → protective class → source rank → rule id |
| storage | Scylla F1–F4: source revisions, rules (16 buckets), rulesets, active pointer (LWT) |
| reload | processes poll the pointer every 15 s; swap only a verified, compiled ruleset; keep the last good one on failure |
| decision data | F5 latest per URL, F6 history (on admission or change, TTL 90 d), F7 redirects, F8 browser aggregates per observation |

## Guarantees

- Pure, deterministic engine: the same input and ruleset id give the same
  decision; no URL-level Prometheus labels.
- Fetching never depends on P6: without an active ruleset every request
  and link is allowed (B.5 #1).
- P5 facts are complete: links are filtered at admission, not extraction.
- A main-frame navigation is never blocked; redirects are classified,
  never rewritten or failed.
- No component writes frontier internals; every admission goes through P3.
- Nothing solves or evades CAPTCHAs or bot protection.

## Running it

```bash
crawler2-discover admit                     # urls.discovered consumer (any number)
crawler2-discover seeds FILE --source NAME --every 21600
crawler2-discover search --queries FILE --every 86400
crawler2-filter status | explain URL | publish … | activate ID | rollback
```

Configuration: `filter.*`, `discovery.*`, `search.*` (implementation §2–§4).
