# What P0 built

A detailed inventory of everything that exists after P0: what each piece
is, how to use it and why it exists. Design rationale lives in
[design.md](design.md); evidence that it works is in
[validation.md](validation.md).

## 1. Python project

| Item | Details |
|---|---|
| `pyproject.toml` | Package `crawler2` 0.0.1, Python `>=3.12,<3.13`, hatchling build. Runtime deps: `pydantic`, `pydantic-settings`, `structlog`, `prometheus-client`, `redis` (8.x), `scylla-driver`, `antipiracy-contracts` (local path). Dev group: `pytest`, `hypothesis`, `ruff`, `mypy`. Tool config for ruff (rule sets E,F,W,I,B,UP,SIM,RUF,N,S,BLE,PT; line length 100), mypy (`strict`, pydantic plugin), pytest (`--strict-markers`, `integration` marker). |
| `uv.lock` | Exact resolution of everything; the source of truth. |
| `requirements.txt` / `requirements-dev.txt` | Pinned exports of `uv.lock` (runtime / runtime+dev), used by the Docker image and by anyone not using uv. Regenerate with `make requirements`; CI fails if they drift from the lock. |
| `env/` | The project virtualenv (Python 3.12.3), managed by uv via `UV_PROJECT_ENVIRONMENT=env` (set in the Makefile). Git-ignored. |
| `Makefile` | `install` (uv sync --frozen), `requirements`, `lint`, `format`, `typecheck`, `test`, `check` (lint+typecheck+test), `up`, `up-two-host`, `down`, `validate-stack`. |
| `.python-version` | `3.12`. |

Pinned versions (from the lock): pydantic 2.13.5, pydantic-settings 2.15.0,
structlog 26.1.0, prometheus-client 0.26.0, redis 8.1.0, scylla-driver
3.29.12, pytest 9.1.1, hypothesis 6.168.2, ruff 0.16.9, mypy 2.3.1.

## 2. Code (`crawler2/`)

### `core/configuration/settings.py` — the configuration system

```python
from crawler2.core.configuration import Settings, WorkerRole

settings = Settings()  # reads CRAWLER2_* from the environment
settings.host_id, settings.roles, settings.redis.host, settings.scylla.contact_points
```

- `Settings` (pydantic-settings, frozen, `extra="forbid"`), sections
  `redis`, `scylla`, `minio`, `logging`, `metrics`, `limits`.
- Env: `CRAWLER2_HOST_ID=host-1`, `CRAWLER2_ROLES=http,browser`,
  `CRAWLER2_REDIS__PORT=6380`, `CRAWLER2_SCYLLA__CONTACT_POINTS=s1,s2,s3`,
  `CRAWLER2_SCYLLA__REPLICATION_STRATEGY=NetworkTopologyStrategy`, …
- Enums: `Environment` (dev/test/prod), `WorkerRole` (http, browser, tor,
  intelligence, media_probe, finalizer, encoder), `LogFormat`,
  `ReplicationStrategy`.
- Validation: host_id `^[a-z0-9][a-z0-9-]{0,62}$`; unknown/duplicate roles
  rejected; `encoder` needs `limits.gpu_vram_mb > 0`; passwords/keys are
  `SecretStr`.
- `MinioSettings.base_url` derives `http(s)://endpoint`.

### `core/identity.py` — worker identity

```python
ident = WorkerIdentity.create(settings.host_id, WorkerRole.HTTP)
str(ident)  # "host-1:http:4242:9f0c…(32 hex)"
WorkerIdentity.parse(str(ident)) == ident
```

Immutable dataclass; validates every part; `instance_id` is a fresh
UUID4 hex per process, so identities never collide across hosts/restarts.
Used by every claim/event/evidence record from P3 on.

### `core/observability/` — logging and metrics

```python
configure_logging(settings, role="http")  # once per process
log = get_logger("frontier")
with bind_correlation_id() as cid:
    log.info("url_claimed", url=url)
# {"component":"frontier","url":"…","event":"url_claimed","level":"info",
#  "timestamp":"2026-…Z","service":"crawler2","host_id":"host-1",
#  "role":"http","correlation_id":"…"}

metrics = Metrics(settings)
metrics.counter("fetches", "Fetch attempts", labels=["outcome"]).labels(outcome="ok").inc()
metrics.histogram("fetch_seconds", "Fetch latency").observe(0.2)
metrics.serve()  # /metrics on CRAWLER2_METRICS__PORT (default 9100)
```

- JSON (default) or console rendering; level filtering from settings.
- Per-instance Prometheus registry; `crawler2_process_info` carries
  host_id/service/environment/roles.

### `diagnostics/connectivity.py` — `crawler2-check`

Console script (also `python -m crawler2.diagnostics`). Checks Redis
(`PING` + `TIME`), ScyllaDB (`SELECT release_version FROM system.local`
via a DC-aware execution profile) and MinIO (`/minio/health/ready`). It
logs one structured `backend_check` event per backend and exits 0 only if
all pass. `--wait N` retries for up to N seconds. Expected backend errors
(`OSError`, `RedisError`, `NoHostAvailable`, `DriverException`, `URLError`)
are reported as failures; any other exception propagates (no silent
swallowing). It is the app container's health check.

## 3. Contract package (`contracts/`)

`antipiracy-contracts` 0.0.1: `pyproject.toml`, `src/antipiracy_contracts/__init__.py`
(`__version__`), `py.typed`, README with the versioning rules. No schemas
yet (P1). Installed into crawler2 as a path dependency; in the image it
is built as a normal wheel. See ADR-007.

## 4. Docker development stack

| File | Content |
|---|---|
| `docker-compose.yml` | Services `redis` (7.4.2-alpine), `scylla` (6.2.3), `minio` (pgsty/minio RELEASE.2026-08-04), `app` (profile `single`), `app-host-1` + `app-host-2` (profile `two-host`); network `backend`; volumes `redis-data`, `scylla-data`, `minio-data`, `scratch-dev-1`, `scratch-host-1`, `scratch-host-2`. Health checks, `depends_on: service_healthy`, `mem_limit`/`cpus`, restart policies, 127.0.0.1-only host ports. |
| `docker/app/Dockerfile` | Two stages on `python:3.12.11-slim-bookworm`: build a venv from `requirements-dev.txt` (arg `REQUIREMENTS` switches to runtime-only) + the package; runtime stage as uid/gid 10001 `crawler2`, `/var/lib/crawler2/scratch`, tests copied in for in-container integration runs. |
| `.dockerignore` | Allow-list (only pyproject, README, requirements, `crawler2/`, `contracts/`, `tests/`). |
| `.env.example` | `COMPOSE_PROJECT_NAME`, `COMPOSE_PROFILES=single`, MinIO root credentials (placeholders), host ports. Copy to `.env` (git-ignored). |
| `scripts/validate-stack.sh` | End-to-end validation of both profiles (see design §8). |

Usage:

```bash
cp .env.example .env && $EDITOR .env
make up                                  # or: docker compose up -d
docker compose ps                        # all "healthy"
docker compose exec app crawler2-check
docker compose exec app env RUN_INTEGRATION_TESTS=1 python -m pytest -q tests/integration
make up-two-host                         # app-host-1 + app-host-2
make validate-stack
make down                                # add -v to docker compose down to drop data volumes
```

Host access for tools: Redis `127.0.0.1:16379`, Scylla `127.0.0.1:19042`
(`cqlsh 127.0.0.1 19042`; Python drivers must run in-network, see
audit), MinIO API `127.0.0.1:19000`, console `http://127.0.0.1:19001`.

## 5. Tests

| Path | Tests |
|---|---|
| `tests/unit/test_settings.py` | defaults, env + nested + CSV overrides, invalid host_ids, unknown/duplicate roles, encoder GPU rule, extra-field rejection, secret masking, immutability |
| `tests/unit/test_identity.py` | format, uniqueness, hypothesis round-trip, malformed inputs |
| `tests/unit/test_observability.py` | required JSON fields, correlation scope, level filter, metrics exposition + process info, registry isolation |
| `tests/unit/test_connectivity.py` | per-backend results, unexpected errors propagate |
| `tests/unit/test_fixture_site.py` | fixture server serves routes on loopback, 404 |
| `tests/integration/test_stack.py` | backends reachable by service name (never localhost), host identity from config |
| `tests/fixtures/web.py` | `serve(routes)` context manager / `fixture_site` pytest fixture |

## 6. CI (`.github/workflows/ci.yml`)

`quality`: uv 0.12.17, Python 3.12, `uv sync --frozen`, requirements
drift check, `make lint`, `make typecheck`, `make test`.
`compose`: throwaway `.env`, `docker compose config` for both profiles,
`fs.aio-max-nr` raise, `scripts/validate-stack.sh`, logs on failure,
`down -v` always. No external sites are contacted by tests.

## 7. V1 baseline tooling (`benchmarks/v1-baseline/`)

| File | Purpose |
|---|---|
| `seeds.txt` | Fixed seed set: V1 `seeds/piracy_sites.txt` at `2dfb542` (51 URLs) |
| `run.sh [MINUTES] [CONCURRENCY]` | Snapshot V1 with `git archive`, add psutil on a side path, write config overrides (seed file, concurrency, Redis 127.0.0.1:16379 db 2, `bench_v1_baseline*` namespaces), flush db 2, run `main.py --runtime --crawler-engine auto --monitor-resources --output` under `/usr/bin/time -v`, then analyze |
| `analyze.py <run_dir>` | Produces `summary.json`: pages/s, success rate overall and per engine, browser share, CPU/RSS, media per 1k pages; `bytes_per_page` is explicitly null (not measurable in V1) |
| `results/<run_id>/` | `environment.txt`, `config.yaml`, `v1_report.json`, `time.txt`, `crawl.log.gz`, `summary.json` |

## 8. Documentation

- `docs/adr/ADR-001` … `ADR-007` (+ index). ADR-005 is **open**.
- `docs/phases/p00-foundations/`: `audit.md`, `design.md`,
  `what-was-built.md` (this file), `baseline.md`, `validation.md`.
- `README.md`: quick start and layout.

## 9. Not built in P0 (by design)

Scylla keyspaces/tables and repositories, Redis frontier, events/outbox,
workers and fetchers, extraction, media registry, filtering, intelligence,
fingerprinting, evidence, analytics, dashboards, production deployment,
contract schemas. Each belongs to the phase listed in the plan.
