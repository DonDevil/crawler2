# P4 audit — V1 fetch engines, network-failure handling, seed set

Status: **audit** (no code). Design: [design.md](design.md).
V1 is read at `~/anti_piracy/crawler` @ `2dfb542` (the P0 baseline commit)
and is not modified.

## 1. Inputs read

| Source | What it contributed |
|---|---|
| `docs/v2-phase-plan.md` P4, A.2 (D1–D4, D14), B.4, B.5 | scope, defects to design out, invariants |
| P3 phase doc, ADR-015, ADR-016, `crawler2/frontier/model.py`, `heartbeat.py` | the only worker-facing frontier API (§7) |
| P1 `models/web.py`, `events/web.py`, `docs/phases/p01-contracts/events.md`, `access-patterns.md` | `FetchAttempt`, `FetchOutcome`, `PageObservation`, `HttpValidators`; `crawler_worker` owns `fetch.completed` + `page.observed`; W1, W4, W5 |
| P2 `storage/repositories.py`, `what-was-built.md` | `FetchAttemptRepository`, `PageObservationRepository.latest()` (W5 validators), object store, outbox |
| P0 `baseline.md`, `benchmarks/v1-baseline/` | the seed set and V1 regression bar |
| V1 `docs/architecture/fetch-extractor-audit.md` (2026-08-11) | live-verified behaviour of every engine |
| V1 `network-failure-handling-{audit,design,validation}.md` (N1–N3) | local-outage vs target-failure semantics |
| V1 `crawler/*.py`, `core/{crawler_router,failure_classifier,network_health}.py`, `tor/proxy_config.py`, `utils/request_headers.py` | the code behind the above |

## 2. The seven V1 engines

`HybridCrawler` (engine `auto`, the production default) owns a
per-URL escalation chain over the six single-engine classes; each class
also has its own copy of the worker/scheduler loop (D3).

| # | Engine | Library | Lifecycle | Timeout | Internal retry | Measured in P0 (attempts → successes) |
|---|---|---|---|---|---|---|
| 1 | `async` (`AsyncCrawler`) | aiohttp, one `ClientSession` per run | shared session + cookie jar for the whole run | `crawler.timeout` 15 s, one total value | ≤ 3, 5xx/exception, 1 s sleep | 691 → 646 (93.5 %) |
| 2 | `http` (`HTTPCrawler`) | httpx `AsyncClient`, `follow_redirects=True` | shared client | 15 s | ≤ 3 | 17 → 0 (tried last, only after all others failed) |
| 3 | `tor` (`TorCrawler`) | httpx `AsyncClient(proxy=socks5h://…)` | shared tor + direct clients | 15 s (20 s class default unused) | ≤ 3 | not exercised (no `.onion` seeds) |
| 4 | `playwright` | Playwright async, Chromium | one browser per run, **new context + page per fetch** | `goto` 15 s **then** `networkidle` 15 s (≈ 30 s/attempt) | ≤ 3, 1 s sleep | 27 → 1 (3.7 %) |
| 5 | `selenium` | Selenium 4, Chrome | **new Chrome process per fetch** (0.47 s start + load), `quit()` errors swallowed silently | page-load 15 s | ≤ 3, fresh driver each | 26 → 9 (34.6 %) |
| 6 | `scrapling` | Scrapling `StealthyFetcher` (headless, **stealth**, network idle) | library-managed browser per call, in `to_thread` | library default | ≤ 2 | 45 → 18 (40 %) |
| 7 | `hybrid` (`HybridCrawler`) | router + the six above | lazily starts Playwright/Selenium; semaphores http 10, tor 5, scrapling 3, playwright 2, selenium 1 | per engine | per engine **×** chain **×** frontier (D4) | 674 visited / 691 processed = **97.5 %** |

Chain for a clearweb URL: `async → scrapling → playwright → selenium →
http`; `.onion` → `tor` only. Escalation happens on any failure and when
`CrawlerRouter.needs_browser_upgrade()` flags the fetched HTML (markers
such as `__next_data__`, `id="root"`, `enable javascript`,
`cf-browser-verification`, `captcha`, or ≥ 6 `<script>` with ≤ 1 `<a>`), or
the failure string contains `captcha/cloudflare/403/401/429/202/…`.
`prefers_browser()` exists but has no callers.

### Behaviour inventory

| Aspect | V1 behaviour (evidence) |
|---|---|
| Result | `(html | None, error_string)` only — no status, headers, final URL, timings, bytes (D1) |
| Success | status **== 200** only; any other status is a failure string `HTTP nnn` |
| Redirects | followed by the library (aiohttp default 10, httpx `follow_redirects=True`); hops not recorded; `is_suspicious_redirect` rejects some final URLs (D7, P6) |
| Headers | `utils/request_headers.py`: browser-mimicking set (`Sec-Fetch-*`, `DNT`, …) and a random `fake_useragent` UA unless `crawler.user_agent` is set (config sets `AntiPiracyBot/1.0`) |
| Cookies | the run-wide aiohttp/httpx session keeps one cookie jar **shared by every URL of every domain**; Playwright contexts are per fetch (isolated) |
| Decoding | `response.text()` (library charset detection); `Accept-Encoding: gzip, deflate, br` (brotli installed) |
| Body size | **unbounded** — `response.text()` everywhere, no cap |
| Media | on a media content-type `async` calls `await response.text()` → **whole media body read into memory** (D2). Playwright aborts `media` requests except manifests and reads manifests in full |
| JS | Playwright `page.content()` after `domcontentloaded` + `networkidle`; route handler aborts image/font/beacon, ad/blacklisted hosts (P6 logic inline) |
| Cancellation | `CancelledError` re-raised in Playwright; Selenium/Scrapling run in `to_thread` and cannot be interrupted (thread runs to its own timeout) |
| Cleanup | Playwright context/page closed in `finally`; browser closed only on graceful shutdown; Selenium `quit()` failure is the one fully silent handler in V1 |
| Crash handling | none beyond per-fetch retry; a dead Playwright browser stays dead (`_browser` never recreated) |
| Error classification | strings; `core/failure_classifier.py` (N3) maps strings/exceptions to 8 categories |
| Retries | engine-internal loop × escalation chain × frontier `max_retries` → up to 9+ attempts hidden from the frontier (D4) |
| Metrics | none per fetch; engine usage only in a summary log line; `ResourceMonitor` samples process RSS/CPU |
| Tor | `tor/proxy_config.py`: `TOR_SOCKS_PROXY` env, else `TOR_SOCKS_PORT`, else probe 9050/9150, else **silently** `socks5h://127.0.0.1:9050`; no "Tor down" signal; `tor_manager.py`/`onion_router.py` dead |

## 3. Network-failure handling (V1 N1–N3)

Implemented in V1 and validated (`network-failure-handling-validation.md`):

- 8 categories: HTTP response, target connection, target DNS, *local
  network* (only assigned at completion while confirmed offline), timeout,
  TLS, engine failure, unknown.
- Categories 2/3/5 (connection, DNS, timeout) are *ambiguous*; a
  process-wide run of them without an interleaved success
  (`trigger_threshold` 10) moves `HEALTHY → SUSPECT` and starts an HTTPS
  HEAD probe round against ≥ 2 operator endpoints (no redirects, any
  status = reachable). Two failed rounds `confirm_delay` apart →
  `OFFLINE`; `recovery_confirm_rounds` (2) successes → `HEALTHY`.
- **Classification never grants retry exemption; only confirmed
  `OFFLINE` at completion time does** (anti-abuse property).
- While `OFFLINE`: in-flight claims short-circuit and are *deferred*
  (attempt refunded, fixed delay); new claims pause. `SUSPECT` changes
  nothing.
- Health state is per process, in memory, never shared through Redis
  (host A offline must not pause host B).
- Redis/frontier unavailability is a separate class
  (`FrontierUnavailable`): never reported as a fetch outcome.
- Known limitation: exceptions were stringified before classification, so
  a bare `TimeoutError` became `unknown`; and aiohttp's `ssl:default`
  boilerplate once misclassified every connector error as TLS.

P3 already provides the frontier half (`defer` refunds the attempt,
`defer_delay_s`); P4 ports the classifier and health controller.

## 4. KEEP / CHANGE / DROP

| V1 behaviour | Decision | Reason |
|---|---|---|
| Cheap HTTP first, browser only when needed | **KEEP** | 93.5 % of V1 pages finished on the first HTTP attempt |
| HTML "needs JS" markers (`needs_browser_upgrade` html branch) | **KEEP** as a fetcher *fact* (`needs_js`), not a routing decision | V1-tuned signal; routing moves to runtime/P7 |
| Anti-bot/captcha markers (`captcha`, `cf-browser-verification`, `verify you are human`, `just a moment`, 429) | **KEEP** as outcomes `captcha`/`blocked` | facts P7 learns from |
| Status 200 == success | **CHANGE** | any status is an observation (P1); 2xx/304 = content, 4xx = complete, 5xx/429 = retryable failure |
| `(html, error)` result | **CHANGE** → `FetchResult` (D1) | status, headers, redirects, timings, bytes, validators |
| Library-followed redirects | **CHANGE** → manual, hop-by-hop, capped, recorded | redirect chain is evidence (B.5 #6); per-hop limits |
| Unbounded `response.text()` | **CHANGE** → streamed, capped read | memory safety, slowloris |
| Reading media bodies (D2) | **DROP** | probe only: headers + ≤ N bytes |
| Engine-internal retry loops, hybrid chain (D4) | **DROP** | one frontier attempt = one fetch execution |
| Copy-pasted worker loops (D3) | **DROP** → one runtime | |
| Run-wide shared cookie jar across all domains | **DROP** | cross-site state leak; no cookies persisted in P4 (a fresh jar per attempt, used only across that attempt's redirects) |
| Random browser-mimicking UA and `Sec-Fetch-*` headers | **CHANGE** → honest configurable UA (V1 config's `AntiPiracyBot/1.0` style) and plain `Accept*` headers | P4 must not impersonate to defeat controls |
| Playwright shared browser + per-fetch context | **KEEP/CHANGE**: shared browser, context **reused** for N pages, recycled by count/RSS, crash → relaunch | D14; V1 never recreated a dead browser |
| Playwright `goto` + `networkidle` double timeout | **CHANGE** → one navigation deadline shared by both waits | V1 audit P2 finding |
| Playwright route handler with ad/blacklist logic | **CHANGE** → interception *hook* with a no-op default | ad logic is P6 |
| Playwright aborting image/font/media requests | **KEEP** as the default resource policy (not a filter decision: it is cost control) | bytes/page |
| Selenium new process per fetch, silent `quit()` | **decided by measurement** (design §14); if kept: pooled driver, logged cleanup | D14 |
| Scrapling `StealthyFetcher` | **DROP from the runtime** (design §14): its purpose is anti-detection stealth, which P4 must not implement; it is still *measured* for the record | safety requirement; ADR |
| `tor/proxy_config.py` resolution order | **KEEP**, but "no proxy found" becomes an explicit `tor unavailable` health state instead of a silent default | V1 audit §4 |
| `tor_manager.py`, `onion_router.py` | **DROP** (dead) | D15 |
| N1–N7 classifier + health controller | **KEEP** (port); classify from exception types first, never from `str(exc)` alone | fixes V1's stringification limitation |
| `is_suspicious_redirect`, blacklist writes in fetch path | **DROP** from P4 | P6 (D6/D7) |
| Media-evidence writes from the fetch path | **DROP** from P4 | P5/P8 |

## 5. P0 seed set and fixture web

- **Seed set**: `benchmarks/v1-baseline/seeds.txt` — 51 live piracy-site
  URLs (~50 domains), from V1 `seeds/piracy_sites.txt`. Started by
  `benchmarks/v1-baseline/run.sh`; there is no reset (live web).
  Measured through V1's per-page log line (`analyze.py`). V1's 10-minute
  run from these seeds **processed 691 URLs** (seeds + links V1 extracted),
  recorded in `results/20260927T193459Z/crawl.log.gz`.
- **Fixture web**: `tests/fixtures/web.py` — P0 mechanism only (threaded
  `http.server`, route table of canned responses, ephemeral port); used by
  `tests/unit/test_fixture_site.py`. It cannot yet express redirects with
  state, 304, compression, slow/stalled bodies, streaming large bodies or
  malformed responses; P4 extends it (design §23).
- **Conflict**: P4 has no link extraction (P5), so V2 cannot reproduce
  V1's "crawl 10 minutes from 51 seeds" workload. The design proposes a
  fixed-URL workload taken from that run (design §28) — needs approval.

## 6. Environment facts found

- Neither `aiohttp` nor `httpx` is a crawler2 dependency yet.
- V1's venv has `aiohttp 3.14.3`, `httpx 0.28.1`, `playwright 1.62.0`,
  `patchright 1.62.1`, `selenium 4.47.0`, `scrapling 0.4.15`, `brotli`.
  Playwright Chromium builds are cached in `~/.cache/ms-playwright`;
  Selenium Manager has a cached Chrome + chromedriver in `~/.cache/selenium`.
- **No Tor daemon** is installed or listening (9050/9150 closed,
  `tor.service` inactive). Correctness tests can use a local SOCKS5
  fixture; a live Tor check needs Tor installed or a Tor container.
- **Host processes can reach Scylla at its container IP** (172.18.0.4:9042
  verified with the driver), so browser workers can run on the host
  against the real compose stack; the app container (read-only, 512 MB,
  1 CPU, no browsers) is not a browser host.
- Free RAM with the stack up: ~9 GB available of 15 GB.

## 7. P3 interface P4 must use

Synchronous `Frontier` protocol: `claim(queue)`, `heartbeat(claim)`,
`complete(claim)`, `fail(claim, reason, next_queue=None)`,
`defer(claim, delay_s=None)`, `recover()`, `lease_ttl_s`; helper
`run_with_heartbeat` (renews every `lease_ttl/3`, raises `ClaimLostError`,
tolerates `FrontierUnavailableError`). `fail(next_queue=…)` is the
escalation mechanism and consumes one attempt; `defer` refunds it.
`reason` ≤ 64 chars. Open P3 items handed to P4: where `recover()` runs,
heartbeat cadence, transport-level retry budget inside one attempt, and
whether `selenium` stays a queue (D14).

## 8. Conflicts between the P4 brief and existing documents

| # | Conflict | Proposed resolution |
|---|---|---|
| 1 | Brief says implement; plan B.1 requires design approval first | stop after this audit + design for approval |
| 2 | Brief names `docs/phase/p4-…`; convention is `docs/phases/pNN-name/` | `docs/phases/p04-fetch-workers/` |
| 3 | Brief outcome names (`needs_js`, `captcha`, `redirect_error`, …) vs P1 `FetchOutcome` (9 values) | runtime-internal outcome enum mapped onto P1; fine-grained code in `FetchAttempt.detail`; no P1 change (design §9) |
| 4 | Gate "bytes/page ≤ V1": V1 has no value (P0 limitation 3) | measure both V1 and V2 through the same counting proxy (design §28) |
| 5 | Gate workload: V2 cannot crawl from seeds without P5 | fixed URL list from the P0 run (design §28) |
| 6 | Plan's `fetch.outcome` event vs P1's `fetch.completed` | P1 already folded it into `fetch.completed` (`FetchAttempt`); use it |
| 7 | Scrapling "keep if it beats Playwright" (plan A.3) vs brief's ban on stealth to defeat controls | measure, but do not retain (design §14) |
