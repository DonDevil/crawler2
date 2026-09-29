# ADR-021 — Filter rules as durable, versioned data; filtering at admission

Status: Accepted (P6 design review, 2026-09-29). Additive; supersedes nothing.

## Context

V1 kept its blacklist in a flat file that crawler processes appended to at
runtime, with provenance only in log lines (plan D6). It rejected fetches
on "suspicious" redirects (D7). P5 left two hooks for P6:
`LinkPolicy` inside extraction and the P4 browser `RequestInterceptor`.
Crawl policy (what to admit, when to revisit) belongs to P6/P7; the
frontier forgets a URL when its task ends.

## Decision

1. **Rules are data in Scylla**, written only by the operator tool
   (`crawler2-filter`): immutable source revisions (content-addressed,
   with origin, sha256, importer and licence), immutable rulesets
   (source revisions + decision policy; id = content digest including
   engine semantics and PSL version), and one active pointer per ruleset
   name, changed only by compare-and-set (LWT). Processes hold a compiled
   engine as derived state and swap it only for a fully loaded, verified
   ruleset; otherwise they keep the last good one. No ruleset = allow all
   (B.5 #1).
2. **Decisions are deterministic and explainable**: a total precedence
   order ending in the rule id, and every decision carries classification,
   action, rule id, `source@revision`, matched field/pattern, override and
   ruleset id.
3. **Discovered links are filtered at admission**, not in extraction:
   P5 `LinkPolicy` stays allow-all so extracted facts never depend on the
   ruleset. A P6 consumer of `urls.discovered` applies the filter, the
   static M1 scope and revisit rules, and admits through the P3 API.
4. **Redirects are classified, never rejected at fetch time**: a blocked
   redirect only stops admission of the page's links; the chain stays a
   P4 fact.
5. **Third-party lists are imported, not vendored**, with their licence
   header stored per revision.

## Consequences

- Multi-host safe rule changes within one poll interval; rollback is
  re-pointing to the previous immutable ruleset.
- A rule change never rewrites stored facts; decisions record which
  ruleset produced them.
- P6 owns 11 tables (V003) and emits no events: no P1 contract change.
- P7 replaces the static scope/revisit/priority rules by producing
  admissions; the filter stays a separate, explicit policy layer.
