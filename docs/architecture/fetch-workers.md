# Fetch layer & worker pools (current state)

Workers turn frontier claims into **attempt facts**. One runtime runs
every pool; a pool is an execution queue plus a pluggable `Fetcher`.
Full design, measurements and exit gates:
[P4 phase document](../phases/p04-fetch-workers/p4-fetch-layer-worker-pools.md).
Decisions: [ADR-017](../adr/ADR-017-fetch-runtime-boundary.md) (boundary,
outcomes, frontier mapping), [ADR-018](../adr/ADR-018-fetch-engine-retention.md)
(httpx + Playwright; Scrapling and Selenium dropped).

```
 P3 frontier ── claim(queue) ──► WorkerRuntime (one per pool process)
      ▲                              │ health gate · run_with_heartbeat · hard deadline
      │                              ▼
      │                    Fetcher.fetch(FetchRequest) ── one attempt, no retries
      │                      http  : HttpFetcher  (httpx, manual redirects, bounded decode, media probe)
      │                      tor   : TorFetcher   (same, over socks5h)
      │                      browser: BrowserFetcher (Playwright pool, interception hook → P6)
      │                              ▼
      │                        FetchResult (outcome = fact)
      │                              │ decide(): static default profile (P7 replaces escalation)
      │                              ▼
      │                    Recorder: MinIO snapshot → W4 PageObservation + page.observed
      │                              W1 FetchAttempt + fetch.completed (outbox); W5 validators in
      └── complete | fail(reason, next_queue) | defer ◄──┘
```

## Pools

| Pool | Queue | Fetcher | Capability | Default slots/process |
|---|---|---|---|---|
| `http` | `http` | `HttpFetcher` | `http` | 32 |
| `browser` | `browser` | `BrowserFetcher` | `browser` | 2 pages (2 contexts × 1) |
| `tor` | `tor` | `TorFetcher` | `tor_http` | 8 |

`crawler2-worker --pool <pool>` runs one pool; `workers.<pool>.processes`
starts several. The `selenium` queue exists (ADR-015) but has no pool.
Local capacity never bypasses P3 domain politeness.

## Guarantees

- One frontier attempt = one fetch execution; capability changes are
  `fail(next_queue)` and consume the same budget. No fetcher retries.
- Media is probed, never downloaded: at most `media_probe_bytes` (64 KiB)
  of a media body are read (`Range` when the URL looks like media).
- No page body above `max_body_bytes` (5 MiB decoded) is kept;
  decompression is bounded (bomb-safe); `total_timeout_s` bounds
  slowloris servers.
- Only a probe-confirmed local outage, an unavailable fetcher/proxy,
  storage failure or shutdown defers (attempt refunded); everything else
  is charged to the task.
- A dead browser is replaced without stopping the worker; contexts and
  browsers are recycled by page count and RSS.
- Fetchers never touch Redis, Scylla, P6 or P7; nothing requires an
  intelligence service to run (B.5 #1).

## Operating

- Metrics (bounded labels: pool, outcome, op): `fetch_attempts_total`,
  `fetch_frontier_ops_total`, `fetch_escalations_total`,
  `fetch_bytes_total`, `fetch_media_probe_bytes_total`,
  `fetch_redirects_total`, `fetch_duration_seconds`,
  `fetch_lost_claims_total`, `fetch_record_errors_total`,
  `worker_active_attempts`, `worker_state`.
- Worker states: `ready`, `degraded` (network suspect / browser
  relaunching), `unavailable` (fetcher cannot start — no claims),
  `offline` (confirmed local outage — no claims), `overloaded` (process
  RSS above `limits.max_memory_mb` — no claims), `draining`.
- Network health probes (`workers.network_health.probe_endpoints`) must be
  ≥ 2 independent endpoints; disable only on air-gapped hosts.
- Browser pools need Chromium on the host (`playwright install chromium`);
  the app container image carries no browser.
