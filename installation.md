# Crawler V2 installation

This guide installs the current Crawler V2 development stack in this
repository. It is not a production deployment recipe. Production topology
and remaining acceptance gates are documented under
`docs/phases/p14-production-hardening/`; M6 is not passed and the system is
not production-ready.

## Supported installation paths

| Path | Use | Status |
|---|---|---|
| `uv` host development environment | Unit/contract tests, lint/type checks, and running CLI processes from the host | Declared by `Makefile`/`pyproject.toml`; environment-dependent |
| Docker Compose `single` | Local Redis + ScyllaDB + MinIO and one idle app container | Supported development stack; not production |
| Docker Compose `two-host` | Two app containers sharing local backends, for simulated host tests | Simulation only |
| Secure Compose overlay | Local hardening rehearsal with Redis ACL and credentials | P14 groundwork; not production topology |
| Browser/Tor/GPU | Optional fetch/encoder facilities | Browser needs installed Chromium; Tor is external; GPU is Fingerprinter-only and environment-dependent |

## Requirements

- Linux x86-64 is the inspected development environment. Other OS support is
  not established.
- Python `>=3.12,<3.13` (the checked-in `.python-version` and
  `pyproject.toml` agree).
- `uv`, GNU Make, Git, Docker Engine, and Docker Compose v2.
- About 8 GB free RAM and 20 GB disk are recorded for the local Compose
  stack as development guidance, chiefly for ScyllaDB; these are not
  benchmarked minimums or production sizing.
- Scylla may need a larger Linux AIO budget on some hosts. Check
  `/proc/sys/fs/aio-nr` and `/proc/sys/fs/aio-max-nr` if it fails. Changing
  a host sysctl requires operator/admin approval.
- Browser workers additionally need Playwright Chromium and its Linux
  libraries. Tor workers require an external Tor SOCKS proxy.

## Fresh setup

1. Obtain the repository. If already checked out, begin at step 2.

   ```bash
   git clone <crawler-v2-repository-url> crawler2
   cd crawler2
   ```

   Use the project-provided checkout URL; it is not embedded in this guide.

2. Verify tools and repository version constraints.

   ```bash
   python3.12 --version
   uv --version
   docker --version
   docker compose version
   make --version
   ```

   Python must be 3.12.x and `<3.13`; Docker Compose must support the v2
   `docker compose` command. The project pins Python through
   `.python-version`/`pyproject.toml`, and the dependency graph through
   `uv.lock`.

3. Create the local Compose environment file. It contains development
   credentials; use unique values and do not commit it.

   ```bash
   cp .env.example .env
   ```

   Edit `.env`: change `MINIO_ROOT_PASSWORD` from the example placeholder.
   `MINIO_ROOT_USER` is a local development account, not suitable for
   production. Host ports default to loopback-only Redis `16379`, Scylla
   `19042`, MinIO `19000`, and MinIO console `19001`. Change a port only if
   it conflicts locally. Never paste actual credentials into documentation
   or issue reports.

4. Install the locked Python package and development tools. The Makefile
   targets `./env`; `uv sync --frozen` installs from `uv.lock`.

   ```bash
   make install
   ```

   Expected: `env/bin/python`, `env/bin/pytest`, `env/bin/ruff`, `env/bin/mypy`,
   and declared `crawler2-*` commands exist. It may need network access on
   first install. Do not substitute `pip install -e .`; the supported
   install is the locked `uv` sync.

5. Verify the code/test environment before starting services.

   ```bash
   make check
   ```

   This runs Ruff lint and format checks, strict mypy, and pytest unit/contract
   tests. It is not a test of a live backend. A nonzero exit identifies the
   failed command; do not proceed as though installation passed.

6. Validate Compose configuration and start the single-host dev stack.

   ```bash
   docker compose config -q
   make up
   ```

   `make up` sets profile `single` and builds the app image. It starts Redis,
   ScyllaDB, MinIO, and an app container that waits for the backends then
   sleeps. The app container is not a running crawler worker.

7. Wait for actual backend readiness; a running container alone is
   insufficient.

   ```bash
   docker compose ps
   docker compose exec app crawler2-check --wait 180
   ```

   Success is exit code 0 after Redis `PING`, a Scylla CQL query, and MinIO
   readiness all succeed. If Scylla remains unhealthy, inspect its logs and
   host memory/AIO settings.

8. Initialize the Crawler V2 schema and raw object bucket explicitly.

   ```bash
   docker compose exec app crawler2-storage migrate
   docker compose exec app crawler2-storage status
   docker compose exec app crawler2-storage check
   ```

   Migration is explicit/idempotent. It applies pending Scylla migrations
   and ensures the configured MinIO raw bucket. `status` prints current and
   target schema versions; `check` exits nonzero if the schema is not
   current. Do not run schema commands against production state without
   the deployment procedure and backup.

9. Optional: install browser binaries for host-run browser workers.

   ```bash
   env/bin/playwright install --with-deps chromium
   ```

   `--with-deps` may request OS package changes; review the package-manager
   prompt. This installs into the host environment, not the Compose app
   image. Browser availability is not required for HTTP-only crawling.

10. Optional: configure Tor. Start and secure a Tor service separately, then
    export the SOCKS URL for host processes (or set the corresponding
    Compose environment override):

    ```bash
    export CRAWLER2_WORKERS__TOR_NETWORK__SOCKS_PROXY=socks5h://127.0.0.1:9050
    ```

    The CLI does not install or start Tor. Do not route traffic through
    proxies without authorization.

11. Optional: configure host-run processes. `Settings()` reads only
    `CRAWLER2_*` environment variables, not `.env`. For a host process use
    loopback-published service ports:

    ```bash
    export CRAWLER2_REDIS__HOST=127.0.0.1
    export CRAWLER2_REDIS__PORT=16379
    export CRAWLER2_SCYLLA__CONTACT_POINTS=127.0.0.1
    export CRAWLER2_SCYLLA__PORT=19042
    export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:19000
    ```

    Provide MinIO access/secret credentials through a protected local
    environment mechanism when required. Do not place secrets in shell
    scripts or commit them. Container processes already receive service DNS
    names and values from Compose.

12. Verify optional host configuration, or run the minimal smoke command:

    ```bash
    env/bin/crawler2-check --wait 30
    ```

    It must reach every configured backend. To run a crawl, follow
    [usage.md](usage.md#normal-operation); the stack itself does not start
    worker processes.

## Secure local Compose overlay

`docker-compose.secure.yml` is a local rehearsal overlay, not the production
multi-host topology. It changes Redis to ACL-authenticated access, removes
published backend ports, and requires generated files under `./secrets/`.
The provisioning script writes local secret files; inspect its behavior and
protect the directory:

```bash
deploy/secure/provision-secrets.sh
docker compose -f docker-compose.yml -f docker-compose.secure.yml config -q
docker compose -f docker-compose.yml -f docker-compose.secure.yml up -d
set -a
. ./.env
set +a
deploy/secure/provision-minio.sh http://127.0.0.1:9000 crawler2-raw
unset MINIO_ROOT_USER MINIO_ROOT_PASSWORD
```

The secure overlay currently leaves the MinIO container's root credentials
coming from `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` in `.env`; the
`secrets/minio_root_password` file generated by `provision-secrets.sh` is
not wired into this Compose service. Set a unique local root password in
`.env` before starting this rehearsal and keep that file private. The
provisioning helper runs `mc` inside the MinIO container; its endpoint
argument is required by the script interface but is not used to select the
connection. The helper's default internal endpoint is
`http://127.0.0.1:9000`. Sourcing `.env` exports the root user and password
required by the helper into this shell; do not print or copy those values.
If Compose uses a non-default project name, set `COMPOSE_PROJECT_NAME`
consistently for both Compose and the helper.

Run service health/migration checks with the same combined Compose files.
This overlay still does not provide TLS, all inter-service authentication,
container egress controls, or a production topology; see the security and
deployment documents before using it for anything beyond local validation.

## Installation verification checklist

- [ ] Python is 3.12.x and `uv` is available.
- [ ] `make install` succeeds from the lockfile.
- [ ] `make check` exits 0 (no network service required).
- [ ] `docker compose config -q` succeeds.
- [ ] `docker compose ps` shows `redis`, `scylla`, `minio`, and `app` healthy.
- [ ] `docker compose exec app crawler2-check --wait 180` exits 0.
- [ ] `crawler2-storage status` reports no pending migration and `check`
      exits 0 after explicit migration.
- [ ] Optional browser path: Chromium installation and the browser contract
      test pass in the same environment that will run the browser worker.

The backend check does not prove that workers consume work. For an
end-to-end check, use a small authorized seed and the workflow in
[usage.md](usage.md#start-a-minimal-local-crawl).

## Updating and reinstalling

Update code with the project's normal version-control workflow, then from
the repository root run:

```bash
make install
make check
```

`uv sync --frozen` enforces the committed lockfile. If project dependency
manifests changed, update the lock through the project maintenance process
before using `--frozen`. Schema changes are separate: inspect migration
status, back up the intended data, then use `crawler2-storage migrate` under
the applicable deployment procedure. Do not delete volumes or use
`docker compose down -v` to “reinstall”.

## Common installation failures

| Symptom | Diagnose | Corrective action / verification |
|---|---|---|
| `uv sync` rejects Python | `python3.12 --version`; inspect `.python-version` | Install/select 3.12 and rerun `make install`; verify `env/bin/python --version`. |
| `make install` cannot resolve package | Check network/proxy and `uv.lock`; do not bypass frozen lock | Restore package access or correct approved lock changes; rerun `make install`. |
| Compose says required variable unset | Compare `.env` variable names with `.env.example`; do not print secret values | Create/fix local `.env`; `docker compose config -q` must pass. |
| Docker daemon permission denied | `docker info`; check user/group and daemon availability | Follow OS Docker permission guidance; reopen the login if group membership changed; `docker compose ps` verifies. |
| Port already allocated | Compose startup output and `ss -ltn` | Change the relevant host-port value in `.env`; validate with `docker compose config -q` and `ps`. |
| Scylla startup/restarts | `docker compose logs scylla`; host RAM/AIO | Allow startup time and provision resources; ask host admin before changing AIO sysctl. Verify `crawler2-check --wait 180`. |
| MinIO unhealthy | `docker compose logs minio`; local `.env` values | Correct MinIO values consistently and recreate only the service if needed; check backend readiness. Avoid deleting the data volume. |
| Schema check fails | `crawler2-storage status` | Run explicit migration after backups; verify `crawler2-storage check`. |
| Chromium not found | `env/bin/playwright --version`; inspect the worker environment | Install Chromium in the same host/container environment using Playwright; run browser contract test. |
| Disk exhaustion | Docker disk usage and filesystem free space | Stop workload; expand storage or preserve-and-move data according to backup/recovery runbook. Do not prune persistent volumes blindly. |

## Quick start

```bash
cp .env.example .env
# Edit .env locally: replace the development MinIO password placeholder.
make install
make check
make up
docker compose exec app crawler2-check --wait 180
docker compose exec app crawler2-storage migrate
docker compose exec app crawler2-storage check
```

The shortest verified operational state is a healthy local backend plus
current schema. Starting a crawler requires the explicit consumers, relay,
worker, and authorized seed described in [usage.md](usage.md).
