# P0 Design — Foundations & baseline

P0 builds only the foundation the rest of V2 stands on. Decisions that
outlive P0 are ADRs in [`docs/adr/`](../../adr/README.md); this document
describes how P0 applies them.

## 1. Architecture (target and P0 slice)

```
Crawler Intelligence ──► Redis Frontier ──► Capability Worker Pools ──► Persistent Observation
                                                                              │
                                   ScyllaDB (+ object store) ◄────────────────┘
                                         │
                   Intelligence / Media / Fingerprinting / Evidence
```

P0 provides the pieces every box above needs: typed configuration with
host roles, worker identity, structured logging, metrics, the dev stack
(Redis, ScyllaDB, MinIO) and the quality gates. No box is implemented.
Boundaries and dependency direction: [ADR-001](../../adr/ADR-001-repository-layout.md).

## 2. Repository structure (P0)

```
crawler2/                      Python package
  core/configuration/          Settings schema (pydantic-settings)
  core/identity.py             WorkerIdentity
  core/observability/          logging.py (structlog JSON), metrics.py (Prometheus)
  diagnostics/connectivity.py  crawler2-check (Redis/Scylla/MinIO)
contracts/                     antipiracy-contracts skeleton (ADR-007)
docker/app/Dockerfile          app image (multi-stage, non-root)
docker-compose.yml             profiles: single, two-host
scripts/validate-stack.sh      end-to-end stack validation
tests/{unit,integration,fixtures}
benchmarks/v1-baseline/        seed set, runner, analyzer, results
docs/adr/, docs/phases/p00-foundations/
.github/workflows/ci.yml
pyproject.toml, uv.lock, requirements*.txt, Makefile, .env.example
```

Only directories with P0 code exist; the other boundaries are created by
the phase that fills them (ADR-001 rule 5).

## 3. Docker topology

```
 profile "single"                     profile "two-host"
 ┌──────────┐                         ┌────────────┐   ┌────────────┐
 │ app      │ host_id=dev-1           │ app-host-1 │   │ app-host-2 │  separate root fs (read-only)
 │ all roles│                         │ http,intel,│   │ http,      │  + separate scratch volumes
 └────┬─────┘                         │ finalizer  │   │ browser,   │  host_id=host-1 / host-2
      │                               └─────┬──────┘   │ media_probe│
      │                                     │          └─────┬──────┘
 ─────┴────────────── network "backend" ────┴────────────────┴──────────
      │                     │                     │
 ┌────┴────┐          ┌─────┴─────┐          ┌────┴────┐
 │ redis   │          │ scylla    │          │ minio   │   shared by all app hosts
 │ 7.4.2   │          │ 6.2.3     │          │ 2026-08 │   named volumes: redis-data,
 └─────────┘          └───────────┘          └─────────┘   scylla-data, minio-data
```

- Containers address each other by **service name** (`redis`, `scylla`,
  `minio:9000`); host ports (127.0.0.1 only) exist for host-side tools.
- Health checks: `redis-cli ping`; `cqlsh <container-ip> -e SELECT …`
  (CQL binds to the container IP); `mc ready local`; app: `crawler2-check`.
- App containers `depends_on` all three with `condition: service_healthy`,
  run as uid 10001, `init: true`, `read_only: true`, `/tmp` tmpfs 64 MB,
  one scratch volume each, `restart: unless-stopped`.
- The app container currently runs `crawler2-check --wait 180` and then
  idles as a dev/tooling container (`docker compose exec app …`); it gets
  a real process in P3/P4.
- Images are pinned by exact tag. Development only; production deployment
  is P14.
- Future services (crawler/browser workers, fingerprinter, GPU encoder)
  join the same `backend` network as new services/profiles.

## 4. Resource budget (12 cores, 15 GB RAM, RTX 2050 4 GB)

| Service | Memory limit | CPU limit | Notes |
|---|---|---|---|
| ScyllaDB | 2 GB | 2 | `--smp 2 --memory 1400M --reserve-memory 512M --overprovisioned 1 --developer-mode 1` |
| Redis | 1 GB | 1 | `maxmemory 768mb`, `noeviction` (frontier data must never be evicted silently), AOF on |
| MinIO | 512 MB | 1 | single node |
| app (each) | 512 MB | 1 | P0 tooling only |
| **Stack total (single)** | **≈4 GB** | 5 | two-host adds 512 MB |

That leaves ~9–10 GB and ~7 cores for development tools, V1 runs, browser
pools (P4) and the fingerprinter GPU process (P9). No GPU container in P0.
Deviation from plan P0 (`--memory 2G`): Seastar's default 1.5 GB reserve
makes `--memory 2G` impossible inside a 2 GB budget; heap + explicit
reserve now fit the ~2 GB target.

## 5. Configuration model

One schema, `crawler2.core.configuration.Settings`, immutable, validated
at startup, `extra="forbid"`. Env prefix `CRAWLER2_`, nested `__`.

| Section | Fields (env example) |
|---|---|
| identity | `environment` (dev/test/prod), `service`, `host_id` (`CRAWLER2_HOST_ID`), `roles` (`CRAWLER2_ROLES=http,browser`), `scratch_dir` |
| `redis` | `host`, `port`, `db`, `password` (secret), `namespace`, `socket_timeout_s` |
| `scylla` | `contact_points` (CSV), `port`, `keyspace`, `local_dc`, `replication_strategy`, `replication_factor`, `connect_timeout_s`, `username`, `password` |
| `minio` | `endpoint`, `secure`, `access_key`, `secret_key`, `bucket_raw`, `region` |
| `logging` | `level`, `format` (json/console) |
| `metrics` | `enabled`, `namespace`, `port` |
| `limits` | `max_memory_mb`, `http_concurrency`, `browser_contexts`, `gpu_vram_mb` |

Validation: host_id pattern (no `:`), known roles only, no duplicate roles,
`encoder` requires `gpu_vram_mb > 0`, secrets are `SecretStr` (never in
repr/JSON). Defaults target the compose service names, so containers need
only host-specific overrides. Credentials come from `.env` (git-ignored);
`.env.example` has placeholders only. Adding a role or host is config-only.

## 6. Multi-host invariants applied in P0

From [ADR-006](../../adr/ADR-006-multi-host-topology.md):

- **No authoritative local state**: enforced in dev by read-only root
  filesystems; the two-host profile checks that scratch is not shared.
- **Worker identity** `{host_id}:{role}:{pid}:{instance_id}`
  (`WorkerIdentity.create/parse`, round-trip property-tested).
- **Time**: Redis `TIME` is authoritative for leases/scheduling;
  `crawler2-check` reads it; wall clock only in log timestamps.

## 7. Observability foundation

- **Logging** (`configure_logging(settings, role=…)`, `get_logger(component)`):
  structlog → one JSON object per line with `timestamp` (ISO-8601 UTC),
  `level`, `service`, `host_id`, `role`, `component`, `event`, and
  `correlation_id` inside `bind_correlation_id()` (contextvars, so it
  follows asyncio tasks). Console renderer via `CRAWLER2_LOGGING__FORMAT=console`.
- **Metrics** (`Metrics(settings)`): one `CollectorRegistry` per instance
  (no module-global registry), `counter()`/`histogram()` get-or-create,
  `render()` for tests, `serve()` for `/metrics`. Host attribution uses a
  `crawler2_process_info{host_id,service,environment,roles}` info series.
  No Prometheus/Grafana deployment in P0.

## 8. Testing and CI

- `tests/unit`: settings, identity (hypothesis), logging, metrics,
  connectivity error handling, fixture server.
- `tests/fixtures/web.py`: local fixture web server (loopback, ephemeral
  port, route table). **CI uses deterministic local fixtures, never live
  sites.** Later phases add redirect/304/gzip/JS/HLS/slow/large routes.
- `tests/integration` (marker `integration`, run with
  `RUN_INTEGRATION_TESTS=1` inside app containers): service-name
  connectivity, host identity from config.
- `scripts/validate-stack.sh`: compose config (both profiles), health of
  every service, integration tests in `app`, `app-host-1`, `app-host-2`,
  distinct host_ids, scratch isolation, read-only root fs.
- CI (`.github/workflows/ci.yml`): job `quality` (uv sync --frozen,
  requirements drift check, ruff, ruff format, mypy --strict, pytest) and
  job `compose` (config validation + `validate-stack.sh`).

## 9. Baseline method

See [baseline.md](baseline.md). V1 runs read-only from a `git archive`
snapshot of `2dfb542`, with a fixed seed file, 10 minutes, 50 workers,
engine `auto`, isolated Redis db 2 on the compose Redis. Metrics come from
V1's own run report, its per-page `Processed … chain=…` log lines,
`/usr/bin/time -v`, and the media evidence set `…:assets:all`.

## 10. Deviations from the phase plan (documented, none affect P1+)

| Plan | P0 reality | Why |
|---|---|---|
| "Install Docker" | Docker Engine 29.8.1 + Compose v5.5.1 installed from Docker's apt repo (after first running P0 on a Podman shim that the user then removed) | P0 re-validated on real Docker |
| Scylla `--memory 2G` | `--memory 1400M --reserve-memory 512M`, 2 GB container | Seastar reserve; keeps the ~2 GB budget |
| MinIO official image | `pgsty/minio` rebuild | Official images withdrawn (ADR-003) |
| Baseline file `docs/phases/p00/baseline.md` | `docs/phases/p00-foundations/baseline.md` | Directory naming requested for P0 |
| "Port benchmark harness" | V1 harness reused in place; V2 harness in P3/P4 | V1 harness targets V1's Frontier API |
