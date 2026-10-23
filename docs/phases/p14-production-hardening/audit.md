# P14 audit — state at the start of the hardening sprint (2026-10-22)

Source: `to_complete.md` (audit register, same date, preserved unchanged) plus the checks recorded in
[validation.md](validation.md). Only findings that shaped the sprint are repeated here.

## 1. What existed
* Contracts, storage (RF=1), frontier, fetch, extraction, filter/discovery, media registry, feedback,
  evidence, analytics — each validated on **one host**, mostly with synthetic or limited data.
* A media-only SSRF guard (`media/ssrf.py`) that explicitly did not solve DNS rebinding; the same in the
  fingerprinter acquirer.
* Prometheus metric objects in most components, but `/metrics` served only by `storage` and `discovery`
  — *corrected on inspection*: `media`, `intelligence` and `evidence` CLIs also served it, all on the same
  default port 9100; the worker pools and `extract` did not serve at all.
* A log-only `correlation_id`; envelope `correlation_id`/`causation_id` fields existed but the chain had no root.
* No supervision, dashboards, alerts, SLOs, backup tooling, soak definition or harness, runbooks; Redis without
  auth; the application using MinIO root credentials; no `docs/README.md`/`installation.md`.

## 2. Findings that changed the plan
| # | Finding | Consequence |
|---|---|---|
| A-1 | Page fetching (HTTP, Tor, browser) had **no** SSRF guard at all; browser sub-resources and WebSockets were unchecked | built (security.md §1) |
| A-2 | `RedisStreamReader.claim_stale`/`ack` raised raw `redis` errors → the consumer process exited on a Redis blip (the read path was already typed) | fixed |
| A-3 | A 429 domain was re-claimed at full rate (25 % of M1 run 2) | per-domain back-off built |
| A-4 | The reconciliation signals existed only as pieces (`p6-m1/monitor.py`, fp soak) | unified in `crawler2.soak` |
| A-5 | `redis_exporter`-style signals (stream lag, oldest pending, outbox lag, dead letters) were not exposed by anything | `crawler2-exporter` |
| A-6 | The correlation chain started at `extraction` (no id on `page.observed`) | root minted in the recorder |
| A-7 | `to_complete.md` said evidence-report HTML escaping test status unknown | it existed for titles/campaign names; extended to every field |
| A-8 | `nodetool` inside the dev Scylla container crashes (`fs.aio-nr` = `fs.aio-max-nr`: the running Scylla consumed the whole AIO budget) | backup tooling uses the Scylla REST API |
| A-9 | `uv audit` found 2 advisories (certifi) | upgraded, re-audited clean |
| A-10 | The test suites build raw Redis clients without credentials in 5 places | fixed (`connect_stream_client`) so the suites can run against a secured Redis |

## 3. What cannot be done here
No second machine, no fast disk, no 3-node Scylla, no labelled hard negatives, no owner decisions on
ADR-005/O-1…O-6/hardware/alert destination/RPO-RTO. These are listed with owners in [decisions.md](decisions.md).
The P10 matcher failure (T4 recall) was reproduced (0.760, matching the record) and is a documented
sampling/low-bitrate limit with no implementation defect found; it is an owner decision, not engineering
(validation.md §6).
