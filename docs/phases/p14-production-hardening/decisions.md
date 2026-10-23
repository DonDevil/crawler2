# P14 decisions

## 1. Decided in this sprint (engineering choices, reversible; no owner policy invented)

| Id | Decision | Why |
|---|---|---|
| D-P14-1 | Page-fetch SSRF protection by one shared module; connection pinning where this process owns the socket; per-request checking where it does not (browser, proxy) and the limits stated | security.md §1 |
| D-P14-2 | `UNSAFE_DESTINATION` is a new fetch outcome, **final** (decision `COMPLETE`), recorded as a P1 `CONNECTION_FAILURE` with the fine-grained code in `detail` (ADR-017 pattern) — no contract change | a property of the URL; a retry cannot change it |
| D-P14-3 | Production settings refuse a non-empty SSRF exemption list and weak credentials | startup validation instead of documentation |
| D-P14-4 | Metrics ports = `metrics.port` + role offset; default bind loopback | one host runs many roles; unauthenticated endpoint |
| D-P14-5 | A `correlation_id` is minted per recorded fetch attempt; no envelope/contract change | the fields existed; the chain had no root |
| D-P14-6 | Per-domain 429 back-off uses the existing P3 `set_domain_interval`; process-local strike count, Redis-shared interval | no new API; documented caveat |
| D-P14-7 | Declared `Content-Length` over the limit ⇒ `TOO_LARGE` without streaming (X-15, partial) | saves the bulk-download time |
| D-P14-8 | Stream-reader Redis errors become the typed `StorageUnavailableError` | every loop already retries it |
| D-P14-9 | Supervision with systemd templates, bounded restart budget, per-role memory caps | deployment.md |
| D-P14-10 | Backup restores only into new names; Scylla via REST; Redis dumped but never auto-restored | recovery.md |
| D-P14-11 | A soak limit that is not decided is `INCONCLUSIVE`, never `PASS`; the plan's zero-loss rules are not configurable | soak.md §3 |
| D-P14-12 | `certifi` upgraded to fix two advisories (lock + exports regenerated; nothing else changed) | `uv audit` |

## 2. Open — owner decisions required (nothing here is decided by the sprint)

| Id | Decision | Blocks | Task ids |
|---|---|---|---|
| OD-P14-1 | **Hardware inventory**: which machines exist, roles, disks, network | RF=3, multi-host proof, soak topology, fast storage for E-R1 | TC-001, 010–019, 021, 040–047, 073 |
| OD-P14-2 | Soak definition: workload/seeds, targets, who may start, limits OD-S0…S10 ([soak.md](soak.md) §4), approval record | the 7-day run | TC-071, 073 |
| OD-P14-3 | SLO targets ([observability.md](observability.md) §3) | meaningful alerting thresholds | TC-053 |
| OD-P14-4 | **Alert destination** (person/channel/pager) | anyone being notified | TC-054 |
| OD-P14-5 | RPO/RTO, backup schedule and retention, Redis recovery model (what is rebuilt vs restored), capacity/retention plan | restore drill, M6 | TC-075, 076, 078 |
| OD-P14-6 | Secret store choice and rotation owner | production secrets | TC-065 |
| OD-P14-7 | Dependency/container scanning policy (CI gate? image scanner?) | CI gate | TC-069 |
| OD-P14-8 | Tor policy/egress (no Tor service exists in the repo) and browser/Tor network isolation to apply | R-1, Tor use | TC-067 |
| OD-P14-9 | TLS and inter-service authentication between hosts and between crawler2 and the fingerprinter | multi-host over an untrusted network | TC-066 |
| OD-P14-10 | Redis HA (replica+Sentinel) or documented single-primary acceptance | failover drill | TC-042 |
| OD-P14-11 | MinIO distributed mode or documented single-node risk | evidence durability | TC-043 |
| OD-P14-12 | **Gate G disposition** (run 3 vs. replaced by the soak) — also needs an explicit go-ahead for any run | M1 | TC-006 |

Carried from earlier phases and **not touched**: P10 O-1…O-6 (and P10-D12/D13(b)/D16), ADR-005 and
OD-P12-1…6, P4 success-metric methodology (§33a), P11 OD-2, P7 resume-or-descope, P13 OD-P13-1…5 (the real
`crawler2_m1` export was **not** run: no approval), P12 formal acceptance record (the fixture acceptance is
operator-reported only).
