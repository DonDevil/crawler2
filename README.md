# crawler2 — Anti-piracy crawler V2

V2 is a fresh architecture; V1 (`../crawler`) is a reference and benchmark
baseline only. The authoritative plan is [`docs/v2-phase-plan.md`](docs/v2-phase-plan.md).

**Current phase: P0 — Foundations & baseline.** What exists and why:
[`docs/phases/p00-foundations/`](docs/phases/p00-foundations/)
(start with `what-was-built.md`). Architecture decisions: [`docs/adr/`](docs/adr/).

## Quick start

```bash
cp .env.example .env            # then change MINIO_ROOT_PASSWORD
make install                    # uv sync into ./env (Python 3.12)
make check                      # ruff + mypy + pytest (no network needed)

make up                         # Redis, ScyllaDB, MinIO + app container
make validate-stack             # both compose profiles, end-to-end
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
| `crawler2/diagnostics` | `crawler2-check`: backend connectivity (health check) |
| `contracts/` | `antipiracy-contracts` package skeleton (ADR-007) |
| `docker/`, `docker-compose.yml` | dev stack, profiles `single` / `two-host` |
| `tests/unit`, `tests/integration`, `tests/fixtures` | tests; local fixture web server |
| `benchmarks/v1-baseline/` | V1 baseline seed set, run script and raw results |
