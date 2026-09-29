# P4 design — fetch layer & worker pools

Status: **DESIGN — awaiting review** (plan B.1: no implementation before
approval). Audit: [audit.md](audit.md). After implementation this becomes
the P4 phase document (`p4-fetch-layer-worker-pools.md`) with validation
results; ADR-017 (fetch runtime boundary and outcome mapping) and ADR-018
(engine retention, D14) are written with the first implementation commit.

## 1. Goal

One worker runtime that claims from the P3 frontier, runs exactly one
fetch attempt with a pluggable `Fetcher`, and reports one frontier
outcome — with a rich `FetchResult` (D1), probe-only media handling (D2),
no duplicated worker loops (D3), no hidden retries (D4) and pooled,
recycled browsers (D14).

## 2. Scope

In: `Fetcher` protocol and `FetchRequest`/`FetchResult`; outcome
taxonomy; HTTP fetcher (httpx) with conditional requests, bounded
decoding and media probing; Playwright browser pool with recycling, crash
recovery, render metrics and an interception hook; Tor fetcher (HTTP over
SOCKS5h); failure classifier + per-process network health (V1 N1–N7
port); worker runtime (claim → heartbeat → one attempt → record →
complete/fail/defer), capacity and health; recording of `FetchAttempt`
(W1, `fetch.completed`) and `PageObservation` + raw snapshot (W4,
`page.observed`) through the existing P2 repositories and outbox;
conditional-request validators from W5; `crawler2-worker` CLI; P4
metrics; fixture web extensions; contract, integration, leak, chaos and
end-to-end tests; the seed-set evaluation.

Out: see §3.

## 3. Boundaries

| Phase | Owns | P4 provides |
|---|---|---|
| P4 | *how* to execute one attempt; facts about it | `FetchResult`, `fetch.completed`, `page.observed` with snapshot |
| P5 extraction | parsing, links, media URLs, page versions/change detection, archival policy | the raw body snapshot (`BlobRef`) in `page.observed` |
| P6 filtering | ad/tracker classification, blocking rules | `RequestInterceptor` hook (no-op default); P4 never blocks a host by name |
| P7 intelligence | which capability to use first, escalation policy, retry policy, recrawl | outcomes recorded per attempt; a static default profile table (§22) that P7 later replaces |
| P8 media registry | media identity, persisted probe metadata | `MediaProbe` inside `FetchResult` (§18) |

Fetchers never call Redis, Scylla, the frontier, P6 or P7.

## 4–6. V1 audit, engine comparison, KEEP/CHANGE/DROP

In [audit.md](audit.md) §2–§4. The measured per-engine comparison on the
P0 seed set is §28 below.

## 7. Fetcher interface

```python
class Fetcher(Protocol):
    capability: FetchCapability  # P1 enum: http, browser, tor_http

    async def start(self) -> None: ...  # launch pools/clients; raises FetcherUnavailableError
    async def fetch(self, request: FetchRequest) -> FetchResult: ...
    def health(self) -> FetcherHealth: ...  # ready / degraded / unavailable + reason
    async def close(self) -> None: ...


@dataclass(frozen=True)
class FetchRequest:
    url: str  # canonical URL from the claim
    url_id: UrlId
    attempt: int
    deadline_s: float  # total budget for this attempt
    validators: HttpValidators | None  # from W5, for If-None-Match / If-Modified-Since
    expect: Expectation  # PAGE (default) | MEDIA_PROBE; a hint, not a rule
```

`fetch()` never raises for target behaviour: every target/network result
is a `FetchResult`. It raises only `FetcherUnavailableError` (cannot run
an attempt at all, e.g. browser cannot launch) and `CancelledError`.

## 8. FetchResult

Runtime-internal (not a P1 contract); projected onto P1 by the recorder.

| Field | Notes |
|---|---|
| `outcome: Outcome` | §9 |
| `capability` | from the fetcher |
| `requested_url`, `final_url` | final after redirects |
| `redirects: tuple[Hop, ...]` | status + location per hop (→ P1 `RedirectHop`) |
| `status: int \| None` | last response status |
| `headers: Mapping[str, str]` | response headers, `Set-Cookie`/`Authorization`-like values dropped |
| `content_type`, `content_length` | as declared |
| `body: bytes \| None` | decoded (decompressed) body, only for page responses, ≤ `max_body_bytes` |
| `bytes_read` | bytes read from the network for the whole attempt (all hops, compressed) |
| `body_bytes` | decoded body size |
| `validators: HttpValidators \| None` | `ETag`/`Last-Modified` sent by the server (never fabricated) |
| `conditional: NONE \| SENT \| NOT_MODIFIED` | 304 path |
| `media: MediaProbe \| None` | §18 |
| `timings` | `queue_wait`, `connect`, `ttfb`, `total`; browser: `navigation`, `dom_ready`, `page_acquire` |
| `render: RenderMetrics \| None` | browser: requests, requests blocked by hook, transferred bytes, context/page age, restarts |
| `error: ErrorInfo \| None` | category (§16), exception type name, short message (no URLs with credentials, no headers) |
| `signals: tuple[str, ...]` | which markers produced `needs_js`/`captcha`/`blocked` (for P7 and debugging) |

## 9. Outcome taxonomy

Facts, never strategy. P1 projection in the last column (no P1 change; the
fine-grained code goes into `FetchAttempt.detail` as `outcome=<code>`):

| Outcome | Meaning | P1 `FetchOutcome` |
|---|---|---|
| `ok` | response with content (any 2xx) | `response` |
| `not_modified` | 304 to a conditional request | `response` |
| `http_error` | 4xx/5xx that is not a block (404, 410, 500…) | `response` |
| `needs_js` | 2xx HTML whose content depends on script (V1 `JS_REQUIRED_MARKERS` + script/anchor heuristic) | `response` |
| `blocked` | bot wall / challenge / 429 / 403 with challenge markers | `blocked` |
| `captcha` | a captcha interstitial specifically | `blocked` (detail `outcome=captcha`) |
| `media` | media response probed, body not read | `response` |
| `timeout` | connect/read/total/navigation deadline | `timeout` |
| `dns_error` | target name did not resolve | `dns_failure` |
| `network_error` | refused/reset/unreachable | `connection_failure` |
| `tls_error` | handshake/certificate | `tls_failure` |
| `redirect_error` | loop, > `max_redirects`, invalid/unsupported `Location` | `connection_failure` (detail) |
| `invalid_response` | malformed status line/headers, truncated body, bad encoding | `connection_failure` (detail) |
| `too_large` | page body over `max_body_bytes` | `too_large` |
| `proxy_unavailable` | Tor/SOCKS proxy unreachable (local infrastructure, not target) | not recorded as an attempt (deferred) |
| `fetcher_crash` | browser/page/renderer died during the attempt | `cancelled` (detail) |
| `cancelled` | shutdown or lost claim | not recorded (deferred / abandoned) |

## 10. Worker runtime

One implementation (`crawler2/crawlers/runtime.py`) for every pool; a pool
is `(ExecutionQueue, Fetcher, capacity)`.

```
loop:
  wait until: slot free AND health allows claiming (§20) AND not draining
  claim = to_thread(frontier.claim, queue)        # None → idle backoff 0.2 s … 2 s
  spawn attempt(claim)                             # up to `concurrency` in flight
attempt(claim):
  validators = recorder.latest_validators(url_id)  # W5, bounded, optional
  result, claim = run_with_heartbeat(frontier, claim,
                      asyncio.timeout(attempt_deadline)(fetcher.fetch(request)))
  health.observe(result)
  op = decide(result, health, queue)               # §22 table, pure function
  recorder.record(claim, result, op)               # W1 (+W4/snapshot) via outbox
  report op to frontier (complete / fail(next_queue) / defer), retrying on
      FrontierUnavailableError until the lease would lapse
periodically: frontier.recover()  (every recovery_interval_s, any process; safe concurrently)
```

- Frontier calls are synchronous (P3) and run in `asyncio.to_thread`.
- `ClaimLostError` → abandon silently (P3 §12). `FrontierUnavailableError`
  on claim → pool backs off; nothing is owned.
- Record before report: a crash between them leaves a recorded attempt and
  an expired lease → recovery retries it (at-least-once, idempotent IDs).
- Graceful shutdown: stop claiming, wait `shutdown_grace_s` for in-flight
  attempts, then cancel them and `defer` each (not the target's fault).
- One process per pool (`crawler2-worker --pool http|browser|tor`), many
  async slots per process; hosts choose pools via `roles` (B.4 #6).

## 11. HTTP fetcher

**Library: httpx** (not aiohttp). Reasons: its `Timeout(connect, read,
write, pool)` maps directly onto §15; one library covers HTTP and Tor
(SOCKS5h is V1's only live-verified Tor path, `httpx` + `socksio`);
`MockTransport` for unit tests; HTTP/2 optional. aiohttp would only be
added if the V2 seed evaluation showed a success gap attributable to the
client (V1's aiohttp engine is measured in §28 for that purpose).

- One `AsyncClient` per process with bounded pool limits; cookies are
  **not** kept across attempts (a fresh jar per attempt, used across that
  attempt's redirects only).
- Redirects followed manually, hop by hop: ≤ `max_redirects` (10),
  loop detection on (method, URL), only `http`/`https` targets, each hop
  recorded; `Location` resolution per RFC 3986.
- Streaming body read with a hard cap (`max_body_bytes`, default 5 MiB
  decoded, `max_wire_bytes` 5 MiB compressed); exceeding it stops the read
  and yields `too_large` (no truncated body is stored).
- Decompression: gzip/deflate built in, brotli via `brotli` (adds a
  dependency; the fixture covers it); `Accept-Encoding: gzip, deflate, br`.
  Decompression is bounded by `max_body_bytes` (zip-bomb safe).
- Headers: honest configurable `User-Agent` (default
  `crawler2/<version> (+contact)`), plain `Accept`, `Accept-Language`,
  `Accept-Encoding`; no browser impersonation headers.
- Charset: bytes are stored as received; decoding for marker detection
  uses header charset → meta charset → utf-8 with replacement, on at most
  the first 256 KiB.
- `needs_js`/`blocked`/`captcha` detection runs on the first 256 KiB of
  HTML (V1 marker sets, audit §4).
- Media: §18.

## 12. Browser pool (Playwright, Chromium)

- One Chromium **process** per browser-pool worker process, `contexts`
  contexts (default 2 = `limits.browser_contexts`), `pages_per_context`
  concurrent pages (default 1). A context is reused for many pages; each
  attempt gets a fresh page in a leased context.
- Recycling: a context is closed and replaced after `recycle_pages` pages
  (default 50) and the browser is relaunched after
  `browser_recycle_pages` (default 500) or when the browser's process-tree
  RSS exceeds `browser_rss_limit_mb` (default 1 200). Recycling waits for
  the context's in-flight pages (drain), bounded by
  `browser_operation_timeout_s`.
- Crash detection: `browser.on("disconnected")`, `page.on("crash")`,
  Playwright `TargetClosedError`, and a liveness check before lease. A
  dead browser is relaunched (bounded: `relaunch_attempts` with backoff);
  repeated launch failure → fetcher `unavailable` (§20). The worker
  process never exits because a browser died.
- Navigation: `goto(wait_until="domcontentloaded")` then a bounded settle
  (`networkidle` or `settle_s`, whichever first) — **both inside one
  navigation deadline** (fixes the V1 double timeout).
- Resource policy (cost control, not filtering): abort `image`, `font`,
  `media` requests; a media request is recorded as a probe *candidate* in
  `render` metrics, never downloaded. Manifests (`.m3u8`, `.mpd`) are
  allowed and bounded.
- Result: status of the main response, redirect chain of the main
  navigation, `page.content()` as body (bounded), render metrics.
- No stealth plugins, no fingerprint spoofing, no captcha handling.

## 13. Tor fetcher

The HTTP fetcher over `socks5h://` (DNS through Tor), `capability =
tor_http`, pool `tor`. Proxy resolved as V1 (`TOR_SOCKS_PROXY`, else
`TOR_SOCKS_PORT`, else probe 9050/9150) but **no silent default**: no
reachable proxy → `unavailable`. Health probe = TCP + SOCKS5 greeting to
the proxy every `tor_health_interval_s`. Proxy connection failures are
`proxy_unavailable` (deferred, attempt refunded), distinct from target
failures behind the proxy. `tor_browser` is not implemented in P4 (no V1
equivalent; `needs_js` on the tor queue is recorded, not escalated).
Contract tests use a local SOCKS5 fixture; a live Tor check needs a Tor
daemon (not installed on the dev host — decision point §33).

## 14. Selenium and Scrapling (D14)

Decided from the §28 measurement plus these constraints:

- **Scrapling — not retained.** Its fetcher is built for anti-detection
  (`StealthyFetcher`; options `solve_cloudflare`, `hide_canvas`,
  `block_webrtc`), i.e. defeating access controls, which P4 must not do;
  it also hides its own retries and a per-call browser. Its V1 numbers are
  recorded for the record only.
- **Selenium — not retained unless §28 shows pages it fetches that
  Playwright cannot.** V1's audit found no capability Selenium has that
  Playwright lacks; its cost is a browser process per page. On S51 its
  apparent lead (34 vs 29) reduces to 32 vs 29 after removing 404/403
  pages it cannot distinguish, the rest being live-web flakiness (§28). If §28 finds
  such pages, Selenium comes back as a pooled fallback fetcher (one
  long-lived driver per slot, liveness check, explicit `quit` with logged
  failures, killed process tree on timeout) behind the same `Fetcher`
  protocol, and `WorkerRole` gains `selenium`.
- If not retained, the P3 `selenium` queue stays in the enum (removing it
  is a P3 change with no benefit) but has no pool, no capability maps to
  it, and nothing routes to it; ADR-018 records this.

## 15. Timeout model

| Layer | Setting | Default | Enforced by |
|---|---|---|---|
| claim poll | `idle_poll_min_s` / `idle_poll_max_s` | 0.2 / 2 s | runtime |
| queue wait (browser page lease) | `browser_acquire_timeout_s` | 30 s | pool |
| connect (TCP **and** TLS) | `connect_timeout_s` | 10 s | httpx (covers TLS; classified separately) |
| read inactivity | `read_timeout_s` | 15 s | httpx, per chunk |
| total attempt | `total_timeout_s` | 30 s | `asyncio.timeout` around the whole attempt incl. redirects — **defeats slowloris** (trickling bytes resets read inactivity, not the total) |
| browser navigation | `navigation_timeout_s` | 30 s | one deadline for goto + settle |
| browser operation | `browser_operation_timeout_s` | 10 s | `content()`, context/page close |
| browser launch | `browser_launch_timeout_s` | 30 s | pool |
| runtime hard cap | `total + grace` | +5 s | runtime; on expiry the page/context is killed |
| shutdown | `shutdown_grace_s` | 20 s | runtime |

All are per-pool settings; one attempt can never exceed its hard cap, and
the lease is kept alive by heartbeat independently.

## 16. Network failure handling

Port of V1 N1–N7 (audit §3), classifying from **exception types** (httpx,
socket, ssl, Playwright error codes `net::ERR_*`), never from `str(exc)`
alone:

| Class | Examples | Retry budget | Feeds health counter |
|---|---|---|---|
| target response | any status | per §22 | no |
| target connection / DNS / timeout | refused, NXDOMAIN, read timeout | consumed — **unless** health is `OFFLINE` at completion → defer | yes (ambiguous) |
| TLS | cert, handshake | consumed | no |
| local infrastructure | `OFFLINE` confirmed; Tor proxy unreachable | **not consumed** (defer) | — |
| worker failure | browser crash during attempt | consumed (bounded, poison-page safe) | no |
| worker unavailable | cannot start an attempt | not consumed (defer) and pool stops claiming | — |
| cancellation | shutdown | not consumed (defer) | no |
| frontier failure | `FrontierUnavailableError` | never a fetch outcome (P3 §19) | no |

`HealthController` (HEALTHY → SUSPECT → OFFLINE, probe = HTTPS HEAD
without redirects to ≥ 2 configured endpoints) is per process, in memory,
never shared (host A offline must not pause host B). Endpoints are
configuration; tests use fixture endpoints.

## 17. Conditional requests

Before an attempt the runtime reads W5 (`PageObservationRepository.latest`)
for the URL and, if it has validators, the fetcher sends `If-None-Match`
/ `If-Modified-Since` verbatim. A 304 → `not_modified`: recorded as a
`FetchAttempt` (`response`, 304, detail `outcome=not_modified`), **no new
`PageObservation`** (nothing new was observed; P5/P7 learn "unchanged"
from the attempt). Validators are never fabricated; a W5 read failure
(storage unavailable) means an unconditional request, not a failed
attempt. Browsers do not send conditional requests.

## 18. Media probe (D2) — hard invariant

- A response is *media* when its content type is `video/*`, `audio/*`,
  `application/octet-stream` with a media extension, or the URL has a
  media extension. Detection happens **on response headers, before any
  body read**.
- URLs with a media extension are requested with `Range: bytes=0-N`
  (`media_probe_bytes`, default 64 KiB) instead of a plain GET; a server
  that ignores Range is cut off after `N` bytes anyway.
- The fetcher reads at most `N` body bytes of a media response and closes
  the connection. `MediaProbe` = content type, declared length, `Accept-
  Ranges`, `Content-Range` total, ETag/Last-Modified, bytes read, the
  first `probe_sniff_bytes` (≤ 4 KiB, magic-number sniff: mp4 `ftyp`,
  webm, ts sync byte).
- Manifests (`application/vnd.apple.mpegurl`, `application/dash+xml`,
  `.m3u8`, `.mpd`) are pages, bounded by `max_manifest_bytes` (1 MiB);
  the body goes to the snapshot and P5 parses it (P5 owns the parser).
  P4 records only kind + size.
- No fingerprinting, no segment download, no media persistence (P8).
- Test (§24): a 1 GiB streaming fixture counts the bytes it actually
  sent; the assertion is `bytes_sent ≤ N + one socket buffer` and
  `bytes_read ≤ N`, and the Range header received is checked.

## 19. Browser interception hook

```python
class RequestInterceptor(Protocol):
    def decide(self, request: InterceptedRequest) -> InterceptDecision: ...


# InterceptedRequest: url, resource_type, is_navigation, frame_url, method
# InterceptDecision: ALLOW | BLOCK (+ rule_id, reason) — observe-only fields recorded
```

The pool calls the interceptor for every request after the resource
policy (§12). Default: `AllowAll` (no-op). The decision count per kind is
in `render` metrics. P6 plugs its engine in by configuration; P4 ships no
rules, no blocklist and no host heuristics.

## 20. Worker health

Per process: `STARTING → READY ⇄ DEGRADED → UNAVAILABLE`, `DRAINING`.

| Signal | Effect |
|---|---|
| fetcher `start()` fails | `UNAVAILABLE`; retry start with backoff; no claims |
| browser dead and relaunch failing | `UNAVAILABLE`; no claims |
| Tor proxy unreachable | `UNAVAILABLE`; in-flight → defer |
| network `OFFLINE` | no claims; in-flight → defer |
| network `SUSPECT` | `DEGRADED`; claims continue |
| `timeout_streak` consecutive timeouts on this pool | feeds the network health counter (V1 rule) |
| process RSS > `limits.max_memory_mb` (non-browser) or browser tree > limit | drain + recycle; claims paused until back under limit |

Exposed as a gauge and in a `/healthz`-style JSON line on the metrics
port; no scheduler is built on it.

## 21. Capacity configuration

New `WorkerSettings` (one per pool) under `Settings.workers`:
`http`, `browser`, `tor`, each with `enabled`, `processes`,
`concurrency`, timeouts (§15), body limits (§11, §18), and pool-specific
fields (§12, §13). Defaults (dev host): http 1 process × 32, browser
1 × 2 pages, tor 1 × 8. Local capacity is independent from P3 domain
politeness: a free slot does not bypass a closed domain gate. Host roles
(`roles`) select which pools a host runs.

## 22. Frontier integration — outcome → operation

Static default profile (no P6/P7 needed, B.5 #1). `decide()` is a pure
function; P7 later replaces the escalation column with learned profiles.

| Outcome | Pool `http` | Pool `browser` | Pool `tor` |
|---|---|---|---|
| `ok`, `not_modified`, `media` | complete | complete | complete |
| `http_error` 4xx (≠ 408, 429) | complete | complete | complete |
| `http_error` 5xx, 408 | fail | fail | fail |
| `needs_js` | **fail → `browser`** (escalation, consumes one attempt) | complete (rendered) | complete (no tor browser) |
| `blocked` 429 | fail (backoff) | fail | fail |
| `blocked`, `captcha` | complete (recorded; no escalation, no bypass) | complete | complete |
| `too_large`, `redirect_error` | complete | complete | complete |
| `invalid_response` | fail | fail | fail |
| `timeout`, `dns_error`, `network_error` | fail, or **defer** if health `OFFLINE` | same | same |
| `tls_error` | fail | fail | fail |
| `fetcher_crash` | — | fail | — |
| `proxy_unavailable`, fetcher unavailable, `cancelled` | defer | defer | defer |
| claim lost | nothing | nothing | nothing |

`fail` reason = the outcome code (≤ 64 chars). Exhausted `fail`
(`exhausted`) is already recorded as an attempt. A capability change is
`fail(next_queue=…)`, so it consumes the same attempt budget and backoff
(one attempt = one fetch execution). With `max_attempts = 3` a page gets
http → browser → browser at most.

## 23. Fixture web

`tests/fixtures/web.py` keeps its API (`serve(routes)`) and gains
dynamic routes: redirect/redirect chain/loop; ETag/Last-Modified with 304;
gzip, brotli, zip-bomb-ish compressed body; JS-only page (content injected
by script, V1 markers); captcha page; 403 challenge and 429 pages; a
streaming large media route (e.g. 1 GiB generated, never materialised)
that counts bytes sent and records the `Range` header; small HLS/DASH
manifests; slowloris (headers then 1 byte / s); stalled (no response);
malformed status line/headers and truncated body; connection reset. Plus a
minimal SOCKS5 fixture proxy (counts connections) for the Tor fetcher and
fixture connectivity-probe endpoints. All deterministic, loopback only.

## 24. Test architecture

| Suite | Where | Needs |
|---|---|---|
| unit: FetchResult/outcome mapping, `decide()`, classifier, health state machine, settings | `tests/unit/crawlers` | nothing |
| **fetcher contract suite**: one parametrised suite; common cases for every fetcher, capability-specific cases marked (HTTP-only: 304, gzip/brotli bytes, media Range; browser-only: JS render, crash) — the 15 cases of the brief | `tests/contract/crawlers` | fixture web; browser cases need Chromium (`RUN_BROWSER_TESTS=1`) |
| media body test | contract suite | fixture byte counter |
| runtime ↔ frontier integration (real Redis frontier, fake recorder) | `tests/integration/crawlers` | compose Redis |
| recorder (Scylla W1/W4/W5, MinIO snapshot, outbox) | `tests/integration/crawlers` | compose stack (host reaches Scylla at its container IP, audit §6) |
| browser leak (1 000 pages), browser chaos (SIGKILL mid-page) | `tests/integration/crawlers`, marked `browser` | Chromium, Redis |
| B.5 #1 end-to-end | `tests/integration/crawlers/test_e2e.py` | fixture web + Redis + Scylla + MinIO + real http and browser worker processes; no P6/P7 |

## 25. Browser leak test

1 000 fixture pages through one browser pool (`recycle_pages` 50,
`browser_recycle_pages` 500): record browser-tree RSS every 50 pages,
contexts/pages created and closed, relaunches. Pass: every page
succeeded; open pages/contexts return to 0 between batches; ≥ 1 browser
relaunch and 20 context recycles happened; RSS after the last recycle
≤ 1.25 × RSS after the first recycle and never above
`browser_rss_limit_mb` (platform-independent relative bound, absolute
values reported).

## 26. Browser chaos test

Real frontier (Redis), real browser worker; a fixture page that stalls;
SIGKILL the Chromium process while the attempt is in flight. Pass: the
attempt ends `fetcher_crash` → `fail` (attempt counted, task rescheduled,
not lost; frontier `audit()` clean); the pool relaunches the browser; the
worker completes the next claims; browser restart metric = 1. Also:
SIGKILL the whole worker process → lease expires → another worker
completes the task (P3 recovery).

## 27. B.5 #1 end-to-end test

Stack: fixture web, compose Redis/Scylla/MinIO, `crawler2-worker --pool
http` and `--pool browser` processes. No P6/P7 service runs. Admit a
fixture workload (plain, redirect chain, 304 revisit, gzip/brotli,
JS-only, captcha, blocked, 404, large media, manifest, slowloris) with
default priority and no capability. Assert: all tasks leave the frontier
(completed or exhausted, none lost); the JS-only page was escalated
http → browser and rendered; redirects recorded; the media route sent
≤ N bytes; `fetch.completed`/`page.observed` events in the outbox with
snapshots in MinIO; a worker killed mid-run is recovered from.

## 28. Seed-set evaluation (exit gates)

**Workloads (fixed, committed as URL lists):**

- `S51` — the 51 P0 seeds (`benchmarks/v1-baseline/seeds.txt`): per-engine
  comparison of V1's engines (done in the audit, results below) and of
  V2's fetchers.
- `W691` — the 691 unique URLs V1 processed in the P0 baseline run
  (from its `crawl.log.gz`, seeds + links V1 discovered). Needed because
  P4 has no link extraction, so V2 cannot re-crawl from seeds; using V1's
  own processed set avoids picking URLs favourable to V2.

**Gate run:** same session, back to back, same host, same politeness
(0.3 s per domain as the P0 config), same UA, both through the counting
proxy: (a) V1 `HybridCrawler`'s own escalation chain per URL (V1 code,
read-only snapshot); (b) V2 admits `W691` to its frontier and runs one
http and one browser worker with the default profile.

**Definitions (fixed before measuring):**

- *success* = the URL ended with page content: V1 status `visited`;
  V2 last attempt outcome `ok`/`not_modified`/`media`. Rate = successes /
  URLs in the workload.
- *bytes per page* = upstream bytes through the counting proxy (on the
  wire, both systems, all engines incl. browser subresources) / successes.
- *browser share* = successes whose final attempt ran on a browser engine
  (V1: playwright, selenium, scrapling; V2: browser pool) / successes —
  the P0 definition. A *browser escalation* is any attempt on a browser
  engine. Browser attempt share is reported alongside.
- *media body downloads* = media responses from which more than
  `media_probe_bytes` were read (fixture-proven in tests; counted from
  `FetchResult.media` in the run).

Gates: success ≥ V1, bytes/page ≤ V1, browser share < V1, media body
downloads = 0. Reported exactly, also if failed.

### V1 per-engine results on S51 (audit measurement, D14 evidence)

`benchmarks/p4-fetch/run.sh v1-engines` on 2026-09-29 07:30 UTC, V1
`2dfb542` read-only, results in
`benchmarks/p4-fetch/results/20260929T073053Z/`. Each engine fetched each
of the 52 seed entries once (`max_retries=1`), V1 timeout 15 s, V1 UA,
V1's per-engine concurrency caps, all through the counting proxy.
"Success" is V1's own (`fetch()` returned HTML).

| Engine | Success | Median / p95 latency | Bytes per success | Peak RSS (process tree) | Startup | Failures |
|---|---|---|---|---|---|---|
| async (aiohttp) | 22/52 (42 %) | 1.5 / 3.0 s | 30.5 KB | 108 MB | — | connection 14, unknown 14, http 2 |
| http (httpx) | 26/52 (50 %) | 1.1 / 2.2 s | 28.9 KB | 111 MB | — | unknown 24, http 2 |
| playwright | 29/52 (56 %) | 2.4 / 7.9 s | 276 KB | 1 037 MB (9 children) | 13.1 s cold (USB HDD) | unknown 12, connection 6, timeout 3, http 2 |
| selenium | 34/52 (65 %) | 2.3 / 6.1 s | 467 KB | 1 558 MB (14 children) | 0.27 s/driver, **1 page per process** | engine 13, timeout 5 |
| scrapling | 26/52 (50 %) | 4.0 / 29.7 s | 797 KB | 3 840 MB (32 children) | per call | unknown 12, http 7, timeout 6 |

Union of all engines: 36/52. Direct `curl` the same hour: 24 × 200,
5 × 3xx, 21 × no response — the seed set has decayed since P0 (97.5 %
two days earlier), which is why gate comparisons are same-session only.

Cross-engine analysis:

- **Selenium's lead is mostly an artefact.** Of the 4 seeds only
  Selenium "fetched", 2 are HTTP 404/403 pages that every other engine
  reported as such — V1 Selenium cannot see status codes (D1) and returns
  the error page as success. The other 2 are flaky sites whose success
  alternates between tries both direct and through the proxy (3 × 3
  curl tries: `000/301/301`, `000/000/301`), i.e. live-web noise.
  Status-corrected Selenium = 32/52 vs Playwright 29/52 — within noise —
  at 1.7× Playwright's bytes and a full browser process per page. No
  capability Playwright lacks was found (as in V1's own audit).
- **Scrapling** adds no page that another engine did not also fetch (0
  exclusive successes), at 2.9× Playwright's bytes, 3.7× its memory and a
  30 s p95; plus the stealth objection (§14). Not retained.
- **Browser vs HTTP**: Playwright reached 4 pages the HTTP engines did
  not and missed 3 they reached; browsers cost ~10× the bytes per page.
  Cheap-first remains correct.
- The proxy logged 372 upstream connect errors over the run, almost all
  from browser subresource hosts; in a direct-vs-proxy control (3 sites ×
  3 tries each way) the proxy added no failures.

Consequence for §14: Scrapling dropped; Selenium not retained on this
evidence, with one re-check in the `W691` gate run (status-aware: a page
counts for Selenium only if Playwright failed it *and* its status was
2xx), since V1's hybrid chain exercises Selenium there.

## 29–31. Methodology, results, exit-gate status

Validation stage.

## 32. Known risks

- **Success gate risk:** V1 rescued 27 of 691 pages with Scrapling (18)
  and Selenium (9). Dropping Scrapling (not negotiable: stealth) and not
  escalating `blocked`/`captcha` may lower V2's success rate below V1's
  97.5 %. It will be measured and reported, not redefined.
- Live-web results drift day to day; the gate compares same-session runs.
- httpx + brotli add dependencies; Playwright adds a large browser
  download (already cached on the dev host).

## 33. Decisions requested at review

1. Seed evaluation workload `W691` and the gate definitions in §28.
2. Scrapling not retained (stealth); Selenium retained only if §28 shows
   Playwright-unreachable pages.
3. `blocked`/`captcha` are recorded, not escalated to a browser.
4. httpx as the single HTTP stack.
5. No P1 change: fine-grained outcome in `FetchAttempt.detail`; media
   probe metadata stays in `FetchResult` until P8 defines its persistence.
6. P4 records `FetchAttempt` + `PageObservation` + raw snapshot through the
   P2 repositories (plan B.5 #1 "persist"; P5 needs the body).
7. Live Tor: install a Tor daemon on the host (sudo, by you) or run a Tor
   container — or keep Tor validated against the SOCKS5 fixture only in P4.
