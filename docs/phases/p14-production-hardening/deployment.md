# Deployment, supervision, upgrade and rollback

Status: **units and launcher written and syntax-checked; never run on a deployed host.** The physical
topology (hosts, roles, disks) is undecided (TC-001); nothing below proves multi-host behaviour. The
single-host dev stack remains `docker-compose.yml` ("Development only").

## 1. Process supervision (TC-041)

systemd, because it is already on every Linux host and needs no new framework.

| File | Purpose |
|---|---|
| `deploy/systemd/crawler2-role@.service` | one template unit; the instance name is the **role** (`worker-http`, `worker-browser`, `worker-tor`, `extract`, `relay`, `admit`, `resolve`, `probe`, `hash`, `representations`, `intel`, `feedback`, `evidence-collect`, `exporter`) |
| `deploy/systemd/run-role` | the one place that maps a role to its command (`exec`, so systemd supervises the real process) |
| `deploy/systemd/crawler2.target` | groups the roles enabled on a host |
| `deploy/systemd/crawler2-role@worker-{browser,tor}.service.d/` | memory budget; **network isolation (example, untested)** |
| `deploy/systemd/crawler2.env.example`, `secrets.env.example` | configuration (0644) and credentials (0600), both `EnvironmentFile=` |
| `deploy/systemd/crawler2-soak.service` | runs the soak runner under a supervisor ([soak.md](soak.md)) |

Contract of the template unit:

* **Restart on failure with bounded back-off:** `Restart=on-failure`, `RestartSec=5`, `RestartSteps=6`,
  `RestartMaxDelaySec=120`; **5 failures in 10 minutes ends in `failed`** (`StartLimitBurst`), which is
  visible to alerts instead of crash-looping.
* **Start ordering:** `After=network-online.target`; each start first runs `crawler2-check --wait 120`, which
  exits 0 only when Redis, Scylla and MinIO all answer. Order across roles is not needed: consumers wait on
  their backends and are idempotent. Start data services first, then `relay`, then consumers, then
  worker pools, `exporter` last.
* **Graceful stop:** SIGTERM, 60 s. Workers return in-flight claims without charging the target;
  consumers leave unacknowledged entries for redelivery (handlers are idempotent). `SuccessExitStatus=0 143`.
* **Per-role memory cap** (`MemoryHigh/Max`) so a runaway process is stopped by its unit instead of by the
  host OOM killer picking a victim (on the dev laptop `oomd` has killed terminals).
* **Hardening:** `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, writable only under
  `/var/lib/crawler2` and `/var/log/crawler2`.

Verified: `systemd-analyze verify` (systemd 255) reports only the missing `/opt/crawler2` paths of a
host that has no installation; `bash -n run-role`. **Not verified:** that the units start, restart
or back off on a real host (no system-unit install was done on the dev machine).

## 2. Configuration validation
* `Settings()` is validated at process start (pydantic, `extra=forbid`): a typo in `CRAWLER2_*` aborts startup.
* With `CRAWLER2_ENVIRONMENT=prod` the process additionally **refuses to start** with a non-empty SSRF
  exemption list, missing Redis password / Scylla credentials, or a root-looking/short MinIO key
  ([security.md](security.md) §2).
* `crawler2-storage status` prints the schema state; roles that use repositories refuse an outdated schema.

## 3. Installation on a host (outline; the executable dev version is `docs/installation.md`)
1. `useradd --system crawler2`; unpack the release to `/opt/crawler2`; `uv sync --frozen` (Python 3.12).
2. `deploy/secure/provision-secrets.sh` on an admin machine; copy `crawler2.secrets.env` to
   `/etc/crawler2/secrets.env` (0600) and write `crawler2.env` from the example.
3. `install -m 644 deploy/systemd/crawler2-* /etc/systemd/system/`; `systemctl daemon-reload`.
4. `systemctl enable --now crawler2-role@<role>` for each role of this host; enable `crawler2.target`.
5. Browser hosts: `env/bin/playwright install --with-deps chromium`; then apply
   `isolation.conf` (replace the placeholders) before enabling `worker-browser`/`worker-tor`.

## 4. Upgrade
1. Read the release's migration list (`crawler2/storage/scylla/schema/V*.cql`); migrations are
   **forward-only** and additive (existing code keeps working on the new schema until it is stopped).
2. Take a backup first ([recovery.md](recovery.md) §1) and `crawler2-backup verify` it.
3. Stop producers (`worker-*`, `admit`), let consumers drain (`ConsumerStalled`/lag dashboards flat), stop the rest.
4. Install the new release; run `crawler2-storage migrate` **once**, as the administrative role.
5. Start in the order of §1. Confirm `crawler2-storage status` and the dashboards.
6. Flags that change behaviour stay off until decided: `CRAWLER2_FEEDBACK__ENABLED` (P11 OD-2).

## 5. Rollback
* **Code only (schema unchanged):** reinstall the previous release directory, restart roles.
* **After a migration:** the schema is newer than the old code. By code reading, `Migrator.require_current()`
  refuses a schema it does not recognise, so old code will not start against it. Roll *forward* with a fix,
  or restore the pre-upgrade backup into a **new** keyspace and repoint (`CRAWLER2_SCYLLA__KEYSPACE`).
  This path has not been rehearsed.
* Event contracts are additive (B.5 #5); a consumer on an older minor ignores new fields.

## 6. Not proven
Physical multi-host behaviour, RF=3, Redis HA/failover (single primary + AOF only), distributed MinIO,
network partitions, restart behaviour of the units, cross-host clock/identity behaviour, the real GPU host.
