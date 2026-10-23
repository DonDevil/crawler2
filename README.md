# crawler2 — Anti-piracy crawler V2

V2 is a fresh architecture; V1 (`../crawler`) is a reference and benchmark
baseline only. The authoritative plan is [`docs/v2-phase-plan.md`](docs/v2-phase-plan.md).

**P8 — Media registry & identity is complete** (exit gate passed:
[`docs/phases/p08-media-registry/exit.md`](docs/phases/p08-media-registry/exit.md));
P9 (fingerprinter encoder) **is complete for its approved scope** (accepted 2026-10-15 with documented limitations; not production-deployed): on
licensed real media the S1/S2 reuse, GPU ≥ 5× (6.59×) and 1,000-video soak gates pass (soak
criteria 3/4 only vacuously); FP16 stays gated (its precision gate G-2 failed):
[`docs/phases/p09-fingerprinter-encoder/exit.md`](docs/phases/p09-fingerprinter-encoder/exit.md).
**P11 — Feedback loop is implemented; M4 is not passed** (matcher and fetch simulated in the
replay because P10's M3 is not passed; feedback is off by default):
[`docs/phases/p11-feedback-loop/exit.md`](docs/phases/p11-feedback-loop/exit.md).
**P12 — Evidence collector & finalizer is implemented; M5 is not passed.** The fixture case package
(all inputs synthetic) was reported accepted by the operator on 2026-10-22 (no durable record: reviewer,
date and scope not supplied); the real-package check is pending and ADR-005 is open:
[`docs/phases/p12-evidence-collector/exit.md`](docs/phases/p12-evidence-collector/exit.md).
**P13 — Analytics layer is implemented** (offline Parquet/DuckDB exports, reports, graph,
clustering prototype; validated on synthetic fixtures and a throwaway Scylla keyspace, real-data
export not yet run): [`docs/phases/p13-analytics/exit.md`](docs/phases/p13-analytics/exit.md).
**P10 — vector index & matcher** lives in the fingerprinter repository (`p10-impl`, unpushed): implementation
complete, **M3 not passed** (level-5 equivalence recall 0.933 vs 1.0; capacity, calibration blocked; `match.found`
publication held), recorded in the plan. **P14 — production hardening: groundwork only** (SSRF/DNS-rebinding
closure for page fetching, metrics endpoints + alerts + dashboards, supervision units, backup/restore tooling and a
local rehearsal, the 7-day soak **harness** — not the soak). **M6 is not passed and the system is not
production-ready**; status and what is still open: [`docs/phases/p14-production-hardening/exit.md`](docs/phases/p14-production-hardening/exit.md).
P7 is closed with WHEN and the M2 gate deferred, not passed (D-22):
[`docs/phases/p07-crawl-intelligence/exit.md`](docs/phases/p07-crawl-intelligence/exit.md).
Phase status for every phase is in the plan. Earlier phases:
[`p13-analytics/`](docs/phases/p13-analytics/), [`p12-evidence-collector/`](docs/phases/p12-evidence-collector/), [`p11-feedback-loop/`](docs/phases/p11-feedback-loop/), [`p07-crawl-intelligence/`](docs/phases/p07-crawl-intelligence/), [`p06-filter-discovery/`](docs/phases/p06-filter-discovery/), [`p05-extraction-page-intelligence/`](docs/phases/p05-extraction-page-intelligence/),
[`p04-fetch-workers/`](docs/phases/p04-fetch-workers/p4-fetch-layer-worker-pools.md), [`p03-frontier-scheduling/`](docs/phases/p03-frontier-scheduling/p3-frontier-scheduling.md), [`p02-storage/`](docs/phases/p02-storage/) (start with
`what-was-built.md`), [`p01-contracts/`](docs/phases/p01-contracts/),
[`p00-foundations/`](docs/phases/p00-foundations/).
Architecture: [`docs/architecture/`](docs/architecture/); decisions:
[`docs/adr/`](docs/adr/); development and test tiers:
[`docs/development.md`](docs/development.md); benchmark index:
[`docs/benchmarks.md`](docs/benchmarks.md).

## Quick start

```bash
cp .env.example .env            # then change MINIO_ROOT_PASSWORD
make install                    # uv sync into ./env (Python 3.12)
make check                      # ruff + mypy + pytest (no network needed)
make schemas                    # regenerate contract JSON Schemas after a contract change

make up                         # Redis, ScyllaDB, MinIO + app container
docker compose exec app crawler2-storage migrate   # crawler2 keyspace + raw bucket (idempotent)
make validate-stack             # both compose profiles, end-to-end
scripts/test-crawlers.sh        # P4 fetch tier from the host (browsers, fixture web)
make down
```

Configuration is one typed schema (`crawler2/core/configuration/settings.py`);
every field is overridable via `CRAWLER2_<FIELD>` / `CRAWLER2_<SECTION>__<FIELD>`.

## Layout

| Path | Purpose |
|---|---|
| `crawler2/core/configuration` | typed settings, host roles, resource limits |
| `crawler2/core/identity.py` | worker identity `{host_id}:{role}:{pid}:{instance_id}` |
| `crawler2/core/observability` | structured JSON logging, Prometheus metrics scaffold |
| `crawler2/storage` | repositories (Scylla), object store (MinIO), outbox relay, consumer idempotency, `crawler2-storage` CLI (ADR-012…014) |
| `crawler2/frontier` | Redis frontier: execution queues, shared politeness gate, leases, scheduling, retries, admission limits (ADR-015, ADR-016) |
| `crawler2/crawlers` | worker runtime, http/tor/browser fetchers, outcome mapping, recorder, `crawler2-worker` CLI (ADR-017, ADR-018) |
| `crawler2/analytics` | P13 offline analytics: read-only Scylla export → revisioned Parquet, DuckDB reports/SQL, derived graph, site/template clustering, `crawler2-analytics` CLI |
| `crawler2/core/netguard.py` | P14 SSRF guard shared by every fetch path (connection pinning, per-hop checks); `media/ssrf.py` re-exports it |
| `crawler2/soak` | P14 soak harness: config, collectors, ledger reconciliation, verdicts, reports; `crawler2-soak` |
| `crawler2/ops` | P14 `crawler2-backup` create / verify / restore (new names only) / prune |
| `crawler2/diagnostics` | `crawler2-check`; P14 `crawler2-exporter` (queue/lag/backlog SLIs) and `crawler2-trace` |
| `deploy/` | P14 systemd units, Prometheus/Alertmanager/Grafana as code, secrets and MinIO/Redis least-privilege provisioning |
| `crawler2/media` | P8 media registry: outbox resolver with per-observation claims (ADR-024), probe/hash, encode requester, `crawler2-media` CLI |
| `contracts/` | `antipiracy-contracts` 1.0.0: IDs, domain models, events, catalog, JSON Schemas, compat fixtures (ADR-007…011) |
| `docker/`, `docker-compose.yml` | dev stack, profiles `single` / `two-host` |
| `tests/unit`, `tests/contract`, `tests/integration`, `tests/fixtures` | tests; contract suite; local fixture web server |
| `benchmarks/v1-baseline/` | V1 baseline seed set, run script and raw results |
| `benchmarks/p2-storage/` | partition-size (1M pages) and latency benchmarks, results |
| `benchmarks/p4-fetch/` | V1 per-engine evaluation, exit-gate run (V1 hybrid vs V2 on 691 URLs), counting proxy, results |
| `benchmarks/p3-frontier/` | frontier throughput, 1M-claim chaos run, starvation, eligible-index, crash/heartbeat benchmarks, results |
