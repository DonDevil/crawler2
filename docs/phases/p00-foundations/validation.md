# P0 Validation

Every claim below comes from a command run on 2026-09-28 on the dev host
(i5-11400H, 15 GiB, rootless Podman 4.9.3 + docker-compose 1.29.2).
Nothing is assumed.

## Exit gate

| Gate item | Result | Evidence |
|---|---|---|
| V2 repository foundation, logical boundaries | ✅ | commits `5fd656d`…; ADR-001; layout in `what-was-built.md` |
| V1 untouched | ✅ | `git -C ../crawler status --short` identical before and after P0 (only the user's pre-existing edits: `config.yaml`, a `.pyc`, 2 deleted WAL files, 5 untracked `burst_*` results); HEAD still `2dfb542`. The baseline ran from a `git archive` snapshot in a scratch dir |
| `docker compose up` starts the stack | ✅ | `docker compose up -d` (profile from `.env`): `redis`, `scylla`, `minio`, `app` all `Up (healthy)` |
| Redis / ScyllaDB / MinIO healthy | ✅ | compose health checks: `redis-cli ping`, `cqlsh <ip> -e SELECT…`, `mc ready local` |
| App reaches services by Compose names | ✅ | in `app`: `CRAWLER2_REDIS__HOST=redis`, `…CONTACT_POINTS=scylla`, `…ENDPOINT=minio:9000`; `getent hosts` → `redis/scylla/minio.dns.podman`; `crawler2-check` → redis `redis_time=…`, scylla `release_version=3.0.8`, minio `http_status=200`; integration test asserts no `localhost`/`127.0.0.1` |
| Two-host simulation works | ✅ | `scripts/validate-stack.sh` (fresh volumes, after `down -v`, 95 s total): `app-host-1`/`app-host-2` healthy; integration tests pass in both (2 passed each); host ids `host-1 / host-2`; marker file written in host-1 scratch is absent in host-2 (**scratch isolation: ok**); `touch /app/should-fail` fails (**read-only root fs: ok**) |
| App container non-root | ✅ | `id` → `uid=10001(crawler2)` |
| Python 3.12 reproducible | ✅ | `env/bin/python --version` → 3.12.3; `uv lock --check` → resolved 30 packages, lock up to date; `requirements*.txt` regenerated from the lock with no diff; image builds from `requirements-dev.txt` |
| ruff | ✅ | `ruff check .` → All checks passed; `ruff format --check .` → 44 files already formatted |
| mypy | ✅ | `mypy` (strict) → no issues in 25 source files |
| pytest | ✅ | host: 32 passed, 2 skipped (integration, stack-only); in containers: integration 2 passed ×3 containers |
| CI configuration | ⚠️ partially verified | workflow YAML parses; every step it runs (`uv sync --frozen`, requirements drift check, `make lint/typecheck/test`, `docker compose config` for both profiles, `scripts/validate-stack.sh`) passed locally. **Not executed on GitHub Actions**: the repository has no remote yet |
| Typed configuration, `.env.example`, host identity, roles | ✅ | `tests/unit/test_settings.py`, `test_identity.py` (incl. hypothesis round-trip) |
| Structured logging works | ✅ | `test_observability.py`; live JSON lines from containers carry `timestamp, level, service, host_id, role, component, event` |
| Metrics scaffold works | ✅ | `test_observability.py`: counter/histogram exposition, `crawler2_process_info{host_id,…}`, per-instance registry isolation |
| ADR-001…007 exist, ADR-005 OPEN | ✅ | `docs/adr/` |
| P0 design documents | ✅ | `audit.md`, `design.md`, `what-was-built.md`, `baseline.md`, this file |
| V1 baseline recorded | ✅ (with documented gaps) | `baseline.md`: 1.02 pages/s, 97.5 % page success, per-engine rates, 4.2 % browser share, 36.9 % of one core, 214 MB RSS avg, 0 media/1k pages. **Bytes/page not measurable in V1** |
| No secrets committed | ✅ | `.env` untracked; `git grep` for the generated MinIO password finds nothing |

## Idle footprint (single profile, `podman stats`, right after start)

scylla 97 MB / 2.1 GB limit · minio 88 MB / 512 MB · redis 3 MB / 1 GB ·
app < 1 MB / 512 MB. Limits are enforced (`podman inspect` shows them), so
the worst case is ≈4 GB for the single profile.

## Problems found and fixed during P0

| Problem | Fix |
|---|---|
| Scylla exited: `insufficient physical memory: needed 2042626048 available 899678208` (Seastar's 1.5 GB default reserve) | `--memory 1400M --reserve-memory 512M` in a 2 GB container |
| Scylla health check failed although `init - serving`: CQL binds to the container IP | `cqlsh "$(hostname -i)"` |
| Official MinIO images: `requested access to the resource is denied` | `pgsty/minio` pinned release (ADR-003) |
| Image `HEALTHCHECK` ignored by Podman (OCI format) | health checks declared in compose |
| scylla-driver `DeprecationWarning` (legacy load-balancing parameter) | execution profiles |
| V1 monitor CPU peak of 5463 % on 12 cores in the smoke run (sampling artifact) | peak not reported; `/usr/bin/time -v` added as an independent whole-run measure |
| V1 log lines contain ANSI colour codes → engine names mis-parsed in the smoke run | strip ANSI before parsing |

## Known limitations carried forward

- Host-side Python clients can't use Scylla through the published port
  (driver follows container IPs); run such code inside the compose network.
- Podman socket must be running (`systemctl --user enable --now podman.socket`).
- CI has not run on GitHub yet (no remote).
- Baseline is a single live-internet run; see `baseline.md` limitations.
