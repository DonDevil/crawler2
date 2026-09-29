# ADR-017 — Fetch runtime boundary: one attempt per fetcher, outcomes as facts, frontier as the only retry

Status: Accepted (P4, 2026-09-29).

## Context

V1's seven engines each returned `(html, error)` (D1), each carried a
copy of the worker loop (D3), and retries were nested inside engines,
inside the hybrid escalation chain and inside the frontier (D4). The
decision to try a browser was made inside the fetch path from string
markers. V2 needs rich attempt facts for P5/P7/P12, independent pools per
capability, and one retry authority (ADR-016).

## Decision

1. A `Fetcher` performs **exactly one attempt** and returns a
   `FetchResult`; it never raises for target behaviour, never retries,
   and has no access to the frontier, storage, P6 or P7.
2. Outcomes are **facts** (`ok`, `not_modified`, `http_error`,
   `needs_js`, `blocked`, `captcha`, `media`, `timeout`, `dns_error`,
   `network_error`, `tls_error`, `redirect_error`, `invalid_response`,
   `too_large`, `proxy_unavailable`, `fetcher_unavailable`,
   `fetcher_crash`, `cancelled`), not strategy.
3. **One runtime** owns claim, heartbeat, the hard attempt deadline,
   health gating, recording and reporting for every pool. The mapping
   outcome → `complete` / `fail(next_queue)` / `defer` is one pure
   function (`decide`, P4 design §22), the static default profile until
   P7 replaces its escalation column.
4. A capability change is `fail(next_queue=…)`: it consumes the same
   attempt budget and backoff as any retry. Only `needs_js` from the
   http pool escalates by default; `blocked`/`captcha` are recorded and
   completed (no bypass).
5. Only infrastructure outcomes that did not touch the target are
   deferred (attempt refunded): proxy/fetcher unavailable, shutdown,
   storage unavailable, and ambiguous network failures while the
   per-process network health is probe-confirmed `OFFLINE` (V1 N2).
6. P1 is unchanged. The recorder projects onto `FetchOutcome`
   (`needs_js`/`not_modified`/`media`/`http_error` → `response`,
   `captcha` → `blocked`, `redirect_error`/`invalid_response` →
   `connection_failure`, `fetcher_crash` → `cancelled`) and carries the
   fine-grained code in `FetchAttempt.detail` as `outcome=<code>`.
   Deferred attempts are not recorded.

## Consequences

- Fetchers are interchangeable behind one contract suite; P7 learns from
  recorded outcomes without fetchers changing.
- A task gets at most `max_attempts` fetch executions across all
  capabilities (e.g. http → browser → browser with 3).
- Consumers needing the fine outcome parse `detail`; promoting it to a
  typed P1 field later is an additive minor change (ADR-009).
- Media probe metadata stays in `FetchResult` until P8 defines its
  persistence.
