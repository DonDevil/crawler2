# Installation (development)

This installs the **development stack on one machine**. It is not a production deployment (see
[deployment.md](phases/p14-production-hardening/deployment.md)). Last executed end to end on the project's
Ubuntu dev host; the steps below were not re-run from a clean machine in the P14 sprint.

## Prerequisites
* Linux x86-64, Docker with the compose plugin (user in the `docker` group), ~8 GB free RAM for the stack
  (ScyllaDB is the large one), ≥ 20 GB disk. **ScyllaDB needs AIO budget**: on a host where
  `cat /proc/sys/fs/aio-nr` is close to `fs.aio-max-nr`, raise it
  (`sudo sysctl -w fs.aio-max-nr=1048576`) or Scylla will not start and `nodetool` will crash.
* [`uv`](https://docs.astral.sh/uv/) (version pinned in `.github/workflows/ci.yml`); Python 3.12 is installed by uv.
* For the browser tier: Chromium (`env/bin/playwright install --with-deps chromium`).
* For GPU work (fingerprinter encoder, in the other repository): NVIDIA driver + CUDA runtime.

## Steps
```bash
git clone <repo> crawler2 && cd crawler2
cp .env.example .env                 # then change MINIO_ROOT_PASSWORD (never commit .env)
make install                         # uv sync --frozen -> ./env  (also installs the crawler2-* commands)
make check                           # ruff + mypy + pytest, no network or services needed
make up                              # Redis, ScyllaDB, MinIO + app container (profile "single")
docker compose exec app crawler2-storage migrate     # creates the keyspace and raw bucket (idempotent)
docker compose exec app crawler2-check               # every backend must answer
```

## Optional
| What | How |
|---|---|
| Test tiers against the stack | `scripts/test-*.sh` ([development.md](development.md)) |
| Browser contract suite incl. SSRF in Chromium | `RUN_BROWSER_TESTS=1 env/bin/pytest tests/contract/crawlers` |
| Secured local stack (Redis ACL, service credentials, `environment=prod`) | `deploy/secure/provision-secrets.sh && docker compose -f docker-compose.yml -f docker-compose.secure.yml up -d`, then `deploy/secure/provision-minio.sh` ([security.md](phases/p14-production-hardening/security.md)) |
| Metrics, dashboards, alerts | [observability.md](phases/p14-production-hardening/observability.md) |
| Soak harness dry run (2 min, synthetic) | `env/bin/crawler2-soak dry-run scripts/soak/dryrun-synthetic.toml` |

## Verify
`make check` green; `docker compose ps` shows redis/scylla/minio/app healthy; `scripts/validate-stack.sh`
passes both compose profiles. Troubleshooting: Scylla restarts → AIO limit and memory; MinIO unhealthy →
wrong credentials in `.env`; tests skipped → `RUN_INTEGRATION_TESTS=1`.

Teardown: `make down` (keeps volumes); `docker compose down -v` deletes all data of the dev stack.
