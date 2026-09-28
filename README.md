# crawler2 — Anti-piracy crawler V2

V2 is a fresh architecture; V1 (`../crawler`) is a reference and benchmark
baseline only. The authoritative plan is [`docs/v2-phase-plan.md`](docs/v2-phase-plan.md).

**Current phase: P2 — Storage layer (complete).** What exists and why:
[`docs/phases/p02-storage/`](docs/phases/p02-storage/) (start with
`what-was-built.md`); earlier phases: [`p01-contracts/`](docs/phases/p01-contracts/),
[`p00-foundations/`](docs/phases/p00-foundations/).
Architecture decisions: [`docs/adr/`](docs/adr/).

## Quick start

```bash
cp .env.example .env            # then change MINIO_ROOT_PASSWORD
make install                    # uv sync into ./env (Python 3.12)
make check                      # ruff + mypy + pytest (no network needed)
make schemas                    # regenerate contract JSON Schemas after a contract change

make up                         # Redis, ScyllaDB, MinIO + app container
docker compose exec app crawler2-storage migrate   # crawler2 keyspace + raw bucket (idempotent)
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
| `crawler2/storage` | repositories (Scylla), object store (MinIO), outbox relay, consumer idempotency, `crawler2-storage` CLI (ADR-012…014) |
| `contracts/` | `antipiracy-contracts` 1.0.0: IDs, domain models, events, catalog, JSON Schemas, compat fixtures (ADR-007…011) |
| `docker/`, `docker-compose.yml` | dev stack, profiles `single` / `two-host` |
| `tests/unit`, `tests/contract`, `tests/integration`, `tests/fixtures` | tests; contract suite; local fixture web server |
| `benchmarks/v1-baseline/` | V1 baseline seed set, run script and raw results |
| `benchmarks/p2-storage/` | partition-size (1M pages) and latency benchmarks, results |
