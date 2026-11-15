# P14 validation record (sprint 2026-10-22)

Only things **run during this sprint** are listed as results; historical results of earlier phases are
not repeated or relied on except where marked. Every result states its scope. Nothing here is a
production, multi-host or long-duration result.

## 0. Environment

Ubuntu, Linux 7.0, 12 cores, 15 GB RAM, Python 3.12.3, one machine; the compose dev stack up (ScyllaDB 6.2.3
single node RF=1 on a USB HDD, Redis 7.4.2 AOF, MinIO `pgsty/minio` single node, `fingerprinter-index-qdrant`);
`systemd` 255; Docker with images pulled only for validation: `prom/prometheus:v2.55.1` (promtool),
`grafana/grafana-oss:11.3.1`, `pgsty/mc` (unused). `uv` 0.12.21. `coverage` 7.17 was installed into `./env`
(not in the lock) to measure coverage.

## 1. Static checks (whole repository)
| Command | Result |
|---|---|
| `env/bin/ruff check .` | clean |
| `env/bin/ruff format --check .` | 503 files already formatted |
| `env/bin/mypy` (strict, 331 files) | no issues |

## 2. Automated tests run in this sprint

| Suite | Command | Result | Scope / limits |
|---|---|---|---|
| Whole default suite | `env/bin/pytest -q -p no:cacheprovider` | see §2.1 (final run) | unit + contract; integration skipped (no env flag) |
| SSRF/DNS-rebinding, page fetch | `pytest tests/unit/crawlers/test_ssrf_pagefetch.py` | 77 passed | no real DNS and no non-loopback connection; a loopback server proves no request reaches it on a flipped answer |
| Media SSRF (P8 suite, unchanged) | `pytest tests/unit/media` | passed (within the unit run) | |
| 429 back-off, bulk download, recorder trace | `pytest tests/unit/crawlers/test_backoff.py test_bulk_download.py test_recorder_trace.py` | 12 + 3 + 2 passed | back-off exercised through the runtime hook with a fake frontier; **not** measured against live 429 traffic |
| Stream reader typed errors | `pytest tests/unit/storage/test_stream_client.py` | passed | |
| Consistency-level guard | `pytest tests/unit/storage/test_consistency_levels.py` | 2 passed | static; the one deliberate `LOCAL_SERIAL` exception is asserted |
| Exporter, trace, observability config | `pytest tests/unit/test_exporter.py test_trace.py test_observability_config.py` | 5 + 4 + 11 passed | fakes; every referenced `crawler2_*` series exists in code; every alert has a firing rule test |
| Backup tooling | `pytest tests/unit/ops` | 23 passed | fake command runner; a local fake S3 for listing/reading |
| Soak harness | `pytest tests/unit/soak` | 77 passed | fake clock, synthetic workload; includes CLI end-to-end (real time, ~2 s runs) |
| Evidence report hostile values; read-side outage | `pytest tests/unit/evidence` | passed (2 added + 1 added) | synthetic |
| Chromium contract suite incl. SSRF | `RUN_BROWSER_TESTS=1 scripts/test-crawlers.sh -q` (contract + integration, real Redis/Scylla/MinIO) | **88 passed, 22 skipped** (skips are "not applicable to <fetcher>") | real Chromium, fixture web on loopback (exempted by `ssrf_allowed_networks`); `test_browser_ssrf.py` proves refusal without the exemption |
| Later tiers against the real dev stack | `scripts/test-extraction.sh` (= unit + all `tests/integration/storage`) | 202 passed, 1 skipped (Scylla-outage test needs `P8_SCYLLA_OUTAGE=1`: it pauses the shared Scylla; not run) | throwaway keyspaces; synthetic records |
| | `scripts/test-filter-discovery.sh` | 109 passed | |
| | `scripts/test-media.sh` (**TC-031: P8 real-stack tier re-run after P9**) | 125 passed, 1 skipped (same outage test) | the cross-service fingerprinter test (`scripts/test-fingerprinter-p9.sh`) was **not** re-run (needs the fp stack) |
| | `scripts/test-evidence.sh` | 58 passed | |
| | `scripts/test-analytics.sh` | 76 passed | |
| | `scripts/test-feedback.sh` | 24 passed | |
| | `scripts/test-intelligence.sh` | 199 passed | |
| Fingerprinter acquirer | `cd ../fingerprinter && env/bin/python -m pytest tests -k "acquisition or acquir"` | 57 passed (incl. 5 new rebinding tests) | real loopback server + scripted resolvers |
| Fingerprinter O-6 fix | `. scripts/p10-env.sh; pytest tests/p10/test_matrix_exit.py -k o6` | 1 passed (93 s) | real Scylla/Redis/MinIO/Qdrant |
| Fingerprinter rest | see §2.2 | | |

Note on one earlier failure: `test_e2e.py::test_b51_…` failed in two runs made while heavy jobs
(a coverage run, other tiers) were running on the same machine, and passed in the final quiet run above. It is a
timing-dependent test (it kills a worker 1.5 s after spawn and expects the worker to have held a lease). The failure
also occurred once on the pre-change tree, so it is not attributable to this work; it is flaky under load.

### 2.1 Final default-suite run
`env/bin/pytest -q -p no:cacheprovider` (unit + contract; integration/browser skipped without their flags):
**1147 passed, 165 skipped in 194.68 s**, after the last change. An earlier full run of the same command had **1 failure**,
`tests/unit/soak/test_cli.py::test_dry_run_status_and_report_end_to_end` (verdict `INTERRUPTED` instead of `INCONCLUSIVE`): a
genuine defect of the harness, not of the test. The DURATION criterion measured the span between the first and the last
*sample*; a slow round under machine load shortened it below the 2.16 s of that short run. Fixed: the criterion now uses the
run's own start → `completed` record (the sample log is judged by METRICS/SAMPLES/CONTINUITY), the event timestamps follow the
runner's clock, and a dead runner that nobody resumed is reported `INTERRUPTED` instead of `RUNNING` (3 new tests).

### 2.1a Two defects found by a 3-hour repeat-run on one host (2026-11-14), fixed afterwards
* **`earliest_wins` boundary (`storage/layout.py`, `tests/unit/storage/test_layout.py`).** The write timestamp is
  `2·anchor − at` and must stay `> 0` (the test's own invariant). With `anchor = 2020-01-01`, `2·anchor` is
  **2069-12-31T00:00:00Z**, not 2070-01-01, so that instant maps to 0 and is rejected (`mirrored <= 0`) while the
  documented range ("valid until 2070") and the Hypothesis strategy (`max_value=2069-12-31`) included it: the property
  test failed intermittently (2 of 25 suite runs, whenever Hypothesis drew the boundary). The implementation was
  correct; range wording and the strategy were off by one. Corrected: valid instants are strictly before
  2069-12-31T00:00:00Z (last valid `…T23:59:59.999999Z` → timestamp 1); docstring, error message and ADR-012 now say so;
  the strategy ends at the last valid µs; new tests cover the anchor, the last valid instant, the boundary and later
  instants (rejected), and offset-aware inputs. Behaviour of the function is unchanged.
* **Slow soak preflight test (`tests/unit/soak/test_cli.py`).** `test_the_shipped_example_configs_parse_and_the_production_one_is_not_startable`
  calls `runner.preflight()` on `production.example.toml`, whose collectors and Redis point at the placeholder host
  `REPLACE-ME`. Instrumenting `socket.getaddrinfo` showed ~26 lookups of `REPLACE-ME`; on a run where the host's
  resolver timed out each blocked 10–15 s (77 s test), otherwise they return in ms. The delay is host-resolver latency,
  not the code under test. The test now makes name resolution an immediate NXDOMAIN, asserts only the placeholder is
  looked up, and that the `redis` check fails as unreachable, in addition to the `approval`/`thresholds` assertions.
  No production code changed. A ~8 s deterministic floor remains (redis-py's own connection-retry back-off ≈7 s and
  local `nvidia-smi`/git subprocesses), independent of the network.

### 2.2 Fingerprinter (excluding `tests/p10`, which needs its own stack env)
`cd ../fingerprinter && env/bin/python -m pytest tests -q -p no:cacheprovider --ignore=tests/p10`: **553 passed, 118 skipped**
(skips need the Scylla/Redis/MinIO/GPU stacks or env flags). `tests/p10` was run only for the O-6 test (above), not as a whole.

## 3. Restricted Redis user (ACL) against the real suites (TC-065)

A throwaway Redis 7.4.2 container (`--aclfile` from `deploy/secure/redis.acl.template` rendered by
`provision-secrets.sh`, `default` user disabled) was started on a spare port, the generated secrets lived in the
session scratch directory, and the suites were run with `CRAWLER2_REDIS__USERNAME=crawler2` and its password:

| Check | Result |
|---|---|
| anonymous `PING` | `AuthenticationError` (HELLO before auth) |
| app user `FLUSHALL`, `KEYS`, `CONFIG GET`, `ACL LIST`, `SHUTDOWN` | all denied |
| monitor user `INFO`; monitor `SET` | allowed; denied |
| `pytest tests/integration/frontier` as the app user | **53 passed** (Lua scripts, state machine, outage tests) after the tests' raw Redis clients were made credential-aware |
| `tests/integration/storage/{test_outbox_objects,test_stream_retention,test_media_resolver,test_evidence,test_feedback_loop}.py` as the app user | 36 passed at `maxmemory 256mb`; the 1 failure (`test_a_stopped_registry_…`) is **maxmemory-dependent** (byte-budget back-pressure): it passes at 768 mb (the dev Redis setting), also as the ACL user (1 passed) and on the normal dev Redis |
| secured compose overlay | `docker compose -f docker-compose.yml -f docker-compose.secure.yml config -q` valid; `up -d redis` under a separate project: container healthy, unauthenticated `PING` → `NOAUTH`, app user → `PONG`; torn down with its volume |

Not verified: Scylla authentication (a second Scylla cannot start: `fs.aio-nr` = `fs.aio-max-nr`; enabling auth on the shared dev node
would disrupt it).

## 4. Observability configuration
| Check | Result |
|---|---|
| `promtool check config` (v2.55.1, targets copied from the examples) | valid |
| `promtool check rules` | 27 rules found |
| `promtool test rules deploy/observability/rules-tests/crawler2.rules.test.yml` | **SUCCESS** — all 27 alerts fire once in a constructed scenario; `RedisMemoryCritical` and an early `FrontierNotDraining` shown silent when they should be |
| Grafana 11.3.1 with the provisioning files | all three dashboards (`c2-pipeline` 8 panels, `c2-workers`, `c2-infra`) provisioned (`meta.provisioned = true`); only log noise about unused optional provisioning folders. **Panels were not rendered against live data** (no Prometheus scraping a running stack was set up) |
| `systemd-analyze verify` (255) on the role unit | only "not executable: No such file" for `/opt/crawler2` paths; `bash -n run-role` ok. **Units were not started** |
| `.github/workflows/ci.yml` | parses as YAML (5 jobs); its commands were run locally as above; **no remote CI run exists** |

Commands (as in CI job `observability`): `docker run --rm -v <dir>:/etc/prometheus --entrypoint promtool prom/prometheus:v2.55.1 check config|check rules|test rules …`.

## 5. Backup / restore rehearsal (local, NOT the M6 drill)
`set -a; . ./.env; set +a; export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:19000 CRAWLER2_MINIO__ACCESS_KEY=$MINIO_ROOT_USER CRAWLER2_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD; sg docker -c "env/bin/python scripts/backup/rehearsal.py"`

```
rows_src 50 → rows_dst 50, rows_identical true; objects_src 20 → objects_dst 20, objects_identical true
backup 0.7 s, restore 2.0 s, total 25 s; flipped byte detected by `verify` (exit 1) and repaired; second restore refused
passed: true
```
Scope: one Scylla node (RF=1), 50 synthetic rows, 20 objects incl. an evidence-shaped manifest with its artefact,
throwaway names removed afterwards. It exercises the tooling, not durability, RF=3, real evidence, RTO or a restore on an
empty machine. Two real defects of the first drafts were found by running it (native `nodetool` unusable here; ambiguous
table directories after a dropped table of the same name) and fixed.

## 6. Soak harness runs
| Run | Command | Result |
|---|---|---|
| Synthetic dry run | `env/bin/python -m crawler2.soak.cli dry-run scripts/soak/dryrun-synthetic.toml` | 2 min, 42 samples, verdict **PASS**, `scope=synthetic`, `m6_eligible=false`; 215 `encode.requested` ↔ 207 `representation.ready` reconciled within the grace; all 17 criteria PASS |
| Local-stack dry run | `env/bin/python -m crawler2.soak.cli dry-run scripts/soak/dryrun-local-stack.toml` | 4 min against the dev Redis/MinIO and this host with real collectors and real stream tailing: verdict **INCONCLUSIVE**, `scope=production-dry-run`: no `match.found`/edges/limits ⇒ GROWTH, QUEUE_AGE, EVENT_LAG, DISK, RETRIES, LOST_WORK, DUPLICATES, STAGES, EVIDENCE all INCONCLUSIVE, as designed. The first execution found a real defect (a not-yet-created stream raised "no such key" as a tail error 80 times); fixed with a test. Re-run after the fix (4 min): verdict INCONCLUSIVE, 26 samples, **0** tail errors, the same nine criteria INCONCLUSIVE for the same reasons |

These validate the pipeline of monitoring → checkpoint → reconciliation → report. They say **nothing** about
168-hour behaviour, multi-host reliability or production acceptance.

## 7. Other measurements
* **P10-T4 reproduction (2026-10-22):** `cd ../fingerprinter && env/bin/python benchmarks/p10-index/eset_eval.py --eset ~/anti_piracy/datasets/p10/e-set --vectors ~/anti_piracy/datasets/p10/vectors/e-set --out <scratch>/e-set-v1.json` →
  production `equiv/v1` offline recall **0.76** (228/300, Wilson 0.709–0.805), 12,720 pairs, 0 negatives linked — identical to the
  recorded value. No implementation defect was found; the misses are the documented low-bitrate/scene-cut sampling limits
  (fp validation §13.19, §13.27). No tuning was attempted: the target (1.0) and the choice among no-fix / denser sampling / amended
  criterion are owner decision O-2.
* **Dependency audit:** `uv audit --frozen`: 2 advisories on `certifi 2026.7.22` → `uv lock --upgrade-package certifi` (2026.10.19), requirements re-exported (`make requirements`: only the two certifi lines changed) → "no known vulnerabilities in 48 packages". The installed `./env` still holds the old certifi until `uv sync`.
* **Contract pin:** `diff -rq crawler2/contracts/src/antipiracy_contracts fingerprinter/env/lib/python3.12/site-packages/antipiracy_contracts` → identical (1.1.0).
* **Repository secret scan:** regex scan of tracked files found only AWS's published SigV4 example key in a unit test.
* **Least-privilege MinIO user** (`deploy/secure/provision-minio.sh` against the dev MinIO, user/policy/bucket removed afterwards): put/get/list in its bucket worked; list and put in another bucket, object delete, and bucket creation returned 403.

### 7.1 Coverage (unit tests only, branch coverage, `coverage run --branch --source=crawler2 -m pytest tests/unit`)
Measured with the unit tests only (the real-stack tiers of §2 exercise repositories, CLIs and the stores but were not
instrumented), so the numbers *understate* the covered code of store/CLI modules and are **not** a finding that those modules are untested.

| New P14 code | Cover | | Existing packages touched by P14 or named in TC-093 | Cover |
|---|---|---|---|---|
| `core/netguard.py` | 90 % | | `crawlers/http.py` | 77 % |
| `crawlers/backoff.py` | 100 % | | `crawlers/recorder.py` | 81 % |
| `soak/` (all modules) | 84–100 % (config 99, evaluate 92, runner 91, store 95, collectors 94, report 88, reconcile 84) | | `evidence/` (P12) | **70 %** |
| `ops/backup.py`, `ops/cli.py` | 87 %, 80 % | | `analytics/` (P13) | 93 % |
| `diagnostics/exporter.py`, `trace.py` | 72 %, 80 % | | `intelligence/` (P7 + P11) / feedback modules | 82 % / 84 % |
| all of the above together | ~88 % | | `media/` (P8) | **70 %** |
| | | | whole `crawler2` package | **79 %** |

The 85 % new-code target is met for the harness, backup tool, SSRF guard and back-off, and **missed** for
`diagnostics/exporter.py` (72 %: `build`/`main` need live Redis/Scylla; the collector logic is covered) and
`diagnostics/trace.py` (80 %: `main`). P10 (fingerprinter repo) was not measured. P11–P13 per-phase targets are not met by
unit tests alone for evidence (70 %) and media (70 %); the integration tiers were not instrumented.

## 8. Not run (and why)
Real multi-host, RF=3, physical network, Redis failover, MinIO distributed mode, chaos matrix on a deployment, restore drill, the
7-day soak, a 24 h run, the real end-to-end chain, `crawler2_m1` export (no approval), P10 gates other than the T4 reproduction,
`test-fingerprinter-p9.sh`, `test-media-two-host.sh`, the Scylla outage test (shared dev Scylla), Tor (no service), unit start of the
systemd units, application of the network isolation drop-in, secret rotation, Scylla auth, remote CI.

## 9. Preparation for the three-hour real-webpage validation (2026-11-15)

Scope: infrastructure and integration preparation only. **No crawl, no live query, no soak was run**; nothing here
is a P14 gate result. Evidence is labelled V (verified in this session), I (inferred from code/docs), U (untested
assumption), B (blocked).

### 9.1 What was verified (V) and what blocks (B)

| Stage | Status | Evidence / blocker |
|---|---|---|
| 1 Qdrant | **PASS** | 1.15.1 by digest, loopback 16333/16334, API key (401 without/with a wrong key, 200 with), health check `healthy`, 1.5 GiB limit, volume persistence, collection `fp_a4df3d192528_target_g1` 768-d Dot/int8, 42 MiB resident. fingerprinter repo `docs/p10-matcher.md` §2, §10 |
| 2 Blast P10 | **PARTIAL** | 1,700/1,700 vectors indexed per role, manifests equal, matcher tasks `DONE` with 0 candidates (own-content exclusion, design N-13). No positive decision exists (needs a distinct copy of Blast) → NOT TESTED. Calibration 2.1 `UNAVAILABLE` (unchanged) |
| 3 `match.found` | **BLOCKED** | The matcher builds/publishes `match.found` only for a CALIBRATED technique (`matching.py`); `match publish-held` refuses otherwise. Not bypassed, nothing fabricated. Verified instead: groups `crawl-feedback` and `evidence-collector` registered on `match.found.v1`; `target.registered.v1` (Blast) published after registering `crawl-feedback`, delivered (pending 0, lag 0), crawler2 `targets` projection filled; envelope `correlation_id` null (operator-originated) |
| 4 Evidence | **BLOCKED** | Needs a real `match.found` with crawler2 registry provenance. Only verified: `crawler2-evidence collect run` starts, joins group `evidence-collector`, and is the registered required consumer. No evidence record, object or integrity check exists for a new match |
| 5 GPU | **PASS** (bounded) | CUDA FP32 cosine ≥ 0.9999999 vs CPU, 40.7 frames/s vs 8.9 (batch 8), 608 MiB VRAM, GPU ≤ 61 °C, CPU ≤ 86 °C vs 96 °C on CPU inference; seconds-long runs, sustained thermals U. fingerprinter `docs/v2-encoder.md` §8.1. `cuda` runtime variant **not admitted** |
| 6 Plan | **PARTIAL** | `scripts/live-validation/three-hour.sh` (preflight 12/13 pass on the real stack; supervisor ordering, early stop and orphan check tested with `STUB=1`; the real roles were not started) |

**Consequence for the three-hour run.** With technique 2.1 `UNAVAILABLE`, matches found during the run are *stored and held*
(`match summary` → `unpublished`); `match.found` events, the P11 feedback consumer and the P12 evidence collector will receive
nothing. The run can validate discovery → crawl → extraction → media → encode → index → matcher decisions, but **not**
the match → evidence leg. That is an owner decision (O-3) and is not solved by this document.

### 9.2 Owner decisions required before starting

1. `python -m fingerprinter intake init --from-start` **or** `--from-now --reason "…"` (not initialised: preflight FAIL; changes how
   old `encode.requested` entries are treated).
2. `python -m fingerprinter runtime admit-cuda` (records the CUDA variant of `rt-1`; the script defaults to `--device cuda` and its preflight
   refuses otherwise; `ENCODER_DEVICE=cpu` runs the CPU path that reached 97 °C).
3. Authorisation to contact duckduckgo, bing and brave with the four queries of `~/Documents/query.txt` (unmodified, 4 lines).
4. Acceptance that matching/evidence stay held (§9.1).

### 9.3 Role order and data flow (I, from code and docs)

`crawler2-discover search` → `urls.discovered` → `admit` (dedupe, filter, frontier) → `worker-http`/`worker-browser` → outbox →
`relay` → `extract` (pages, media URLs, `media.discovered` in the outbox) → `resolve` (reads settled outbox buckets, **10 min settle**,
so media work lags discovery by ≥ 10 min) → `probe` → `hash` (reads the whole file; ≤ 8 GiB, 1 per host) → `representations`
(emits `encode.requested`, reads `representation.ready`/`encode.failed` via group `media-registry`) → fingerprinter `intake`
→ `scheduler` → `encoder` (acquires the media itself, writes the representation, `representation.ready`) → `reconcile` (publisher, intents)
→ `matcher reconcile` (hints → index upserts) → `matcher run` (searches against the Blast target index; decisions held). `feedback`
and `evidence-collect` consume `match.found`. Discovered media enter the **existing** pipeline only through
`media.discovered` → `encode.requested`; the script adds no alternative path. Targets are matched implicitly: every indexed media
subject is searched against all indexed targets (here only Blast, `tgt_01a1dfb096c67b3fab003ec6677832e7`).

### 9.4 Configuration (set by the script; U = not performance-validated)

Search engines: `CRAWLER2_SEARCH__ENGINES=duckduckgo,bing,brave` (verified to parse; ahmia/Tor excluded). The runner records a status per
engine and query (`results|empty|blocked|parse_error|error|cooldown|unavailable`) and never substitutes another engine. After a CAPTCHA
the engine sits out `blocked_cooldown_queries` = 999 queries, i.e. **for the rest of the run**. CAPTCHA, rate limits and access controls are not bypassed.
Queries repeat every `SEARCH_EVERY_S` = 3,600 s (3 runs; later runs mostly yield duplicate URLs).

| Role | Count | Setting | Why |
|---|---|---|---|
| worker-http | 1 process | concurrency 16 (default 32); per-domain in-flight 2 | network-bound; leaves CPU/thermal headroom |
| worker-browser | 1 process | 2 contexts × 1 page; Chromium RSS limit 1,200 MB (default) | ≈ 1.5 GB worst case on a 15.7 GiB host |
| probe / hash | 1 each | probe 4 (default 8); hash 2, 1 per host | hash streams whole files; bandwidth, not CPU |
| fp-encoder | 1 (one per host by design) | `cuda`, `max_batch` 32 (≈ 1 GiB VRAM of 4), decode threads 2 | GPU 4.5× CPU at lower CPU heat; batch 8 was fastest in the smoke test, the encoder calibrates the batch |
| matcher | 1 `run` + 1 `reconcile` | defaults | index is 42 MiB, queries ≈ 1 ms |
| fp-intake/scheduler/reconcile, relay, admit, extract, resolve, representations, feedback, evidence | 1 each | defaults | consumers are idempotent; more instances add nothing on one host |

Headroom (I/U, estimate not measured): host baseline 5.5 GiB used of 15.7; Scylla 0.55/2 GiB, MinIO 0.21/0.5 GiB (watch: tightest limit),
Redis 18 MiB/1 GiB, Qdrant 42 MiB/1.5 GiB. Scratch is on NVMe under `~/anti_piracy/var-live3h` (62 GB free; stop at < 15 GB).

### 9.5 Commands

```bash
cd ~/anti_piracy/crawler2
scripts/live-validation/three-hour.sh plan         # exact commands and values; starts nothing
scripts/live-validation/three-hour.sh preflight    # 13 checks; starts nothing
nohup scripts/live-validation/three-hour.sh run > /tmp/live3h.out 2>&1 &     # ONLY after the owner's go
touch "$RUN_DIR/STOP_REQUESTED"                    # early, ordered stop ($RUN_DIR is in the first line of /tmp/live3h.out)
```

Start order: relay, admit, extract, resolve, probe, hash, representations, fp-reconcile, fp-intake, fp-scheduler, fp-encoder,
fp-match-reconcile, fp-match, feedback, evidence, worker-http, worker-browser, then `search` (T0, 8 s between starts). The
supervisor measures `DURATION_S` = 10,800 s on its monotonic clock from T0; or an early stop on any `STOP` condition below.

### 9.6 Duration control and shutdown

Producers stop at T0 + 10,800 s exactly; the rest drain for at most `DRAIN_S` = 300 s and then stop in data-flow order, each phase
SIGTERM + up to 60 s (the role unit's `TimeoutStopSec`; workers return in-flight claims without charging the target, consumers leave
entries unacknowledged for redelivery, I): (1) search, worker-browser, worker-http; drain; (2) extract, admit; (3) relay, resolve, probe,
hash, representations; (4) fp-intake, fp-scheduler, fp-encoder; (5) fp-match, fp-match-reconcile, fp-reconcile (publisher last, so
held/pending intents flush); (6) feedback, evidence. Worst case about T0 + 10,800 + 300 + 6 × 60 s ≈ 11,460 s. A role that ignores SIGTERM is
SIGKILLed and logged (`alerts.log`); its claims recover by lease expiry. The frontier, outbox, streams, Scylla rows and Qdrant state are durable, so work
that is not drained stays queued and is not lost, but it is **not counted as completed** in the report. A final snapshot and an orphan scan
(`pgrep` for role commands and Chromium) end the run (`SHUTDOWN_COMPLETE`). Tested with stub roles: normal end, early stop, no orphans (V);
real-role SIGTERM latency under load is U.

### 9.7 Monitoring and thresholds

`resources.csv` every 30 s (CPU package °C, GPU °C/util/VRAM, MemAvailable, swap, disk free, load, dead roles); `snapshots/t<sec>/` every 300 s
(`docker stats`, restart/OOM/health of the four containers, Redis memory and ops, `XLEN`/`XINFO GROUPS` for eight streams, Qdrant collections and
memory, `match summary`, `encode status`, `publisher status`, `index status`, and each role's `/metrics`: crawler2 9100 + offset, fingerprinter 9201–9206).

| Signal | WARN | STOP (ordered shutdown) |
|---|---|---|
| CPU package | ≥ 90 °C | ≥ 97 °C for 5 consecutive samples (the CPU-only encode reached 96–97 °C) |
| GPU | ≥ 80 °C | ≥ 87 °C |
| MemAvailable | ≤ 2 GiB | ≤ 1 GiB |
| swap used | ≥ 1 GiB | – |
| disk free | – | < 15 GB |
| role process gone | always | – (alert only; supervisor does not restart roles) |

Where each requested signal lives (metric names from code; `grep` the `.prom` files):
discovery `search_queries_total{engine,result}`, `discovery_admissions_total`; crawl `fetch_attempts_total`, `fetch_duration_seconds`, `fetch_rate_limited_total`,
`fetch_escalations_total`, `frontier_depth`, `queue_depth`; extraction/media `media_observations_total`, `media_probes_total`, `media_hashes_total`, `media_encode_claims_total`,
`media_reader_lag_seconds`; events `consumer_events_total`, `stream_group_oldest_pending_age_seconds`, `outbox_checkpoint_lag_seconds`; Redis `redis_memory_used_bytes`;
fingerprinting/matching `fingerprinter_*` from the six fingerprinter ports, `encode status`, `match summary` (`unpublished`), `index status`; evidence `evidence_candidates_total`
(will stay 0, §9.1). ScyllaDB errors are read from `docker logs crawler2-scylla-1` at the end (not sampled). Search-engine failures: `search_queries_total` per engine plus the
per-query JSON reports in `logs/search.log`.

### 9.8 Final verification (after `SHUTDOWN_COMPLETE`)

```bash
grep -E 'ALERT|SIGKILL' $RUN_DIR/alerts.log                 # expect none (or explained)
cat $RUN_DIR/supervisor.log | grep -E 'stopped .* rc=|orphan'      # rc 0 or 143 for every role; "no orphan processes"
cat $RUN_DIR/snapshots/final/restarts.txt                   # restarts=0 oom=false health=healthy for four containers
cat $RUN_DIR/snapshots/final/streams.txt                    # pending / lag per group; compare with the t0 snapshot
python -m fingerprinter match summary; python -m fingerprinter encode status; python -m fingerprinter index check --spec <digest> --role media
python -m fingerprinter publisher status                    # media-registry group present; held streams
```

### 9.9 Known limitations of this plan

No `match.found`/evidence leg (§9.1); one host, no failure injection; every numeric setting above is an initial, unvalidated value; real search engines
may block or change markup (recorded, not bypassed); the 10-minute resolver settle delays media work; `hash` and the encoder download whole files (8 GiB cap) so bandwidth and
disk, not CPU, may bind; the sampled GPU data come from seconds-long runs; Scylla/MinIO limits were not raised (no evidence). Results of a three-hour run would be a
single-host observation, not a P14 acceptance gate.
