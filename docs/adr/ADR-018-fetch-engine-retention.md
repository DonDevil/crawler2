# ADR-018 — Fetch engines retained in V2: httpx + Playwright; Scrapling and Selenium dropped (D14)

Status: Accepted (P4, 2026-09-29).

## Context

V1 shipped aiohttp, httpx, Tor-over-httpx, Playwright, Selenium,
Scrapling and a hybrid chain over them. Plan A.3 kept Scrapling only if
it beat Playwright on a measured site set; D14 asked whether Selenium
earns a place. P4 measured each V1 engine on the P0 seeds (P4 design
§28, `benchmarks/p4-fetch/results/20260929T073053Z/`).

## Decision

- **HTTP: httpx** only (not aiohttp). Its per-phase timeouts map onto
  the P4 timeout model, it is V1's only live-verified Tor path
  (SOCKS5h), and aiohttp showed no success advantage (22/52 vs 26/52).
- **Browser: Playwright/Chromium** in a pooled, recycled browser.
- **Tor**: httpx over `socks5h://`; no Tor browser in P4.
- **Scrapling: dropped.** It exists to evade bot detection
  (`StealthyFetcher`, `solve_cloudflare`, `hide_canvas`, `block_webrtc`),
  which P4 must not do; measured, it reached no page another engine did
  not (0 exclusive successes) at 2.9× Playwright's bytes and 3.7× its
  memory.
- **Selenium: dropped** from the runtime. Its measured lead (34 vs 29 of
  52) reduces to 32 vs 29 after removing 404/403 pages V1 Selenium
  cannot recognise as errors; the rest is live-web flakiness. It costs a
  browser process per page and 1.7× Playwright's bytes, and offers no
  capability Playwright lacks. The `selenium` execution queue (ADR-015)
  stays in the enum, unused: no capability maps to it and no pool serves
  it. The P4 gate run re-checks for 2xx pages only Selenium can fetch.

## Consequences

- Two fetch stacks instead of six; one browser lifecycle to operate.
- Re-adding Selenium means a pooled fetcher behind the same `Fetcher`
  contract and a `selenium` worker role — evidence first (new ADR).
