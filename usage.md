# Crawler V2 usage guide

This is the operator guide for the current `crawler2` package in this
repository. Crawler V2 is a separate implementation from `../crawler` (V1).
The old `config.yaml`, V1 scripts, and the V1 `main.py` are not Crawler V2
entry points.

Status labels below refer to this checkout:

- **Code-verified:** confirmed in current source/configuration; not necessarily
  executed against every dependency.
- **Verified here:** the command was executed in this environment.
- **Environment-dependent:** needs external services, browser binaries,
  network access, or the other repository.
- **Not production-ready:** a documented acceptance gate remains open.

## Purpose, boundaries, and flow

Crawler V2 admits seed and discovered URLs, schedules URL fetch work in a
Redis frontier, fetches and records pages, extracts links and media
observations, and maintains durable crawl/evidence projections. It does not
provide a general-purpose HTTP API, a browser UI, a single `crawler2` server
command, or the V1 programmatic interface. Operators start the relevant
processes separately.

```text
seed file / operator search queries
        -> discovery admission + scope/filter policy
        -> Redis frontier (dedupe, domain interval, leases, retries)
        -> crawler2-worker --pool http|browser|tor
        -> ScyllaDB page/fetch records + MinIO page objects + transactional outbox
        -> crawler2-storage relay -> Redis Streams
        -> crawler2-extract -> links/metadata -> urls.discovered -> discovery admission

media observations -> media resolver -> probe -> full-content hash
        -> encode.requested event -> Fingerprinter P9 encoder
        -> representation.ready / encode.failed -> crawler2-media representations
```

The first flow can operate without Fingerprinter. The second requires the
Fingerprinter P9 package and shared Redis Streams, ScyllaDB, and MinIO
configuration. The Crawler P8→Fingerprinter P9 contract is implemented and
has a cross-service integration test, but is not a production deployment.
P10 vector matching is in Fingerprinter and its M3 gate is not passed;
`match.found` is consequently held, not a production result. See
[integration details](#media-registry-and-fingerprinter-side).

Implemented components include HTTP, Playwright browser, and SOCKS/Tor fetch
pools; link/media extraction; URL admission and filtering; frontier
politeness; fetch and media persistence; optional intelligence/feedback;
evidence collection; and offline analytics. Optional/limited functionality:

- Browser fetching requires Chromium installed in the environment running
  the worker. It is not an independently configured Compose service.
- Tor fetching requires an operator-managed Tor SOCKS endpoint. Compose does
  not start Tor.
- P7 source advice and P11 feedback-driven priority are off by default.
- P8 media registry defaults are approved initial assumptions, not
  performance-validated.
- P12 evidence collection/finalization is implemented; its real-package
  acceptance gate remains open.
- P13 analytics is offline and read-only against Scylla. Validation is on
  synthetic data and a throwaway keyspace, not a production export.
- P14 contains hardening groundwork, backup/restore tooling, a soak harness,
  monitoring-as-code, and service units. The seven-day soak has not run and
  M6 is not passed; the system is not production-ready.

## Prerequisites

### Minimal local development stack

- Linux is the documented development target; no minimum CPU/RAM guarantee
  is established. `docs/installation.md` records the existing development
  stack's approximate resource needs, not a supported minimum.
- Python `>=3.12,<3.13`; `uv` and GNU Make.
- Docker Engine and the Docker Compose v2 plugin.
- The Compose development services: Redis 7.4.2, ScyllaDB 6.2.3, MinIO.
- A writable filesystem for Docker volumes and `var/` scratch/output.
- Network access to obtain the locked Python packages and container images
  during initial setup.

### Optional or complete-workflow dependencies

- Playwright Chromium plus its OS libraries for browser workers.
- Tor with a SOCKS proxy for the Tor pool.
- Fingerprinter checkout, its compatible `antipiracy-contracts` package,
  DINOv2 weights, FFmpeg, and its P9 backend/runtime configuration for media
  encoding.
- P10 additionally requires the Fingerprinter-owned Qdrant service, but P10
  is not an accepted end-to-end matching deployment.
- GPU is not required by the ordinary crawler pools. GPU encoding belongs to
  Fingerprinter and requires a matching CUDA-enabled PyTorch installation;
  it is not verified here.

## Inputs

### Operator seed file

`crawler2-discover seeds FILE --source NAME` is the supported user-facing URL
input. The file is UTF-8 text, one URL per line. Empty lines and lines
starting with `#` are ignored. Each non-comment line must already contain
`http://` or `https://`; the loader does not invent a scheme or repair a URL.
Duplicates are counted and admitted once per import. Invalid lines are
reported in the JSON output. The original file hash, line, source, and
operator metadata are stored as provenance. Seed admissions are idempotent.

```text
# seeds.txt
https://example.org/
https://example.org/catalog
# comments and blank lines are ignored
```

```bash
env/bin/crawler2-discover seeds seeds.txt --source approved-seed-list --meta owner=ops
```

The command needs reachable Redis and ScyllaDB (it writes the seed, scope,
and admission state). It prints a JSON report with valid/duplicate/invalid
counts and admission outcomes. No work starts until a compatible worker is
running.

### Operator search query file

`crawler2-discover search --queries FILE` accepts UTF-8, one search query per
line. Whitespace is collapsed, blank/comment lines are skipped, duplicates
are removed, and each query is limited to 300 characters. The configured
search adapters execute requests; blocked/captcha pages are recorded and
put that adapter into cooldown. The code does not solve captchas or evade
access controls. Queries and results are recorded before admission. This
path requires outbound network access and must comply with engine terms and
applicable access rules.

### Internal inputs and unsupported methods

- `crawler2-discover admit` consumes the internal `urls.discovered` stream;
  it is not a file/API interface.
- `crawler2-worker` takes tasks from the Redis frontier; it does not accept a
  URL or input file argument.
- Media probing and hashing consume internal Redis queues created from
  stored media observations. No user-facing local-media upload command or
  crawler HTTP API exists.
- Configuration comes from `CRAWLER2_*` environment variables; there is no
  Crawler V2 YAML config reader. `.env` is read by Compose for variable
  substitution, not automatically by `Settings()` in a host process.
- The Python repositories and contract models are available to code, but
  no stable public URL-list API is declared beyond the CLI above.

## Runnable components

The Compose `app` containers are dependency-ready shells: their image
health-checks the backends and then sleeps. They do not automatically start a
crawler worker. Run commands in the container with `docker compose exec`, or
run the host commands from the activated project environment.

| Component | Purpose | Entry point | Dependencies | Inputs | Run / stop | Verification |
|---|---|---|---|---|---|---|
| Local stack | Redis, Scylla, MinIO, idle app container(s) | `docker-compose.yml`, Makefile | Docker Compose, `.env` | backend settings | `make up`; `make down` (retains volumes) | `docker compose ps`; `crawler2-check` |
| Fetch worker | HTTP/browser/Tor frontier consumer | `crawler2-worker` | Redis; Scylla/MinIO unless `--no-record`; browser/Tor optional | frontier tasks | `crawler2-worker --pool http`; Ctrl-C/SIGTERM | worker start/stop logs and `crawler2-check` |
| Schema/storage | Migrations, status, check, outbox relay | `crawler2-storage` | Scylla; MinIO for migrate; Redis for relay | command options | `migrate`, `status`, `check`, `relay` | `crawler2-storage check` |
| Discovery | Seed/search input and stream admission | `crawler2-discover` | Redis + Scylla; network for search | seed/query files or stream | subcommands below; Ctrl-C for loops | JSON seed/search report; `decisions URL` |
| Extraction/filter/intelligence | Stream consumers and rule operations | `crawler2-extract`, `crawler2-filter`, `crawler2-intel` | Redis + Scylla; MinIO for extraction | internal events or rule/query files | run the selected CLI; Ctrl-C for consumers | logs, storage status, component CLI |
| Media registry | Resolve, probe/hash, encode request and projection | `crawler2-media` | Redis + Scylla + MinIO; Fingerprinter for encoding | internal media observations and Fingerprinter events | subcommands below; Ctrl-C for loops | `crawler2-media show MEDIA` |
| Evidence | Match-event collection and evidence operations | `crawler2-evidence` | Redis + Scylla + MinIO | `match.found` stream and evidence IDs | subcommands below | evidence validation/status |
| Analytics | Durable-to-Parquet export, validation, reports, and offline analysis | `crawler2-analytics` | Scylla for export; local disk for exports | Scylla rows or export/query files | one-shot CLI commands | `status`, `validate` |
| Diagnostics | Backend check, Prometheus scrape exporter, trace | `crawler2-check`, `crawler2-exporter`, `crawler2-trace` | depends on command; Redis for trace | settings/needle | one-shot or metrics process | exit code / `/metrics` |
| Operations | Backup/verify/restore/prune and soak harness | `crawler2-backup`, `crawler2-soak` | Docker/Scylla/MinIO as selected | backup path or TOML soak config | one-shot/controlled workflow | verifier/report |
| Tests/stack validation | Unit/contract/integration checks | Makefile and `scripts/` | see testing section | test fixtures / isolated services | `make check`, targeted script | exit code and pytest report |

There is no `main.py` or single V2 server process. The public installed
commands are declared in `pyproject.toml`: `crawler2-check`, `crawler2-storage`,
`crawler2-worker`, `crawler2-extract`, `crawler2-filter`, `crawler2-discover`,
`crawler2-intel`, `crawler2-media`, `crawler2-evidence`,
`crawler2-analytics`, `crawler2-soak`, `crawler2-exporter`,
`crawler2-backup`, and `crawler2-trace`. After updating the environment, use
`make install` to synchronize all declared commands.

## CLI reference

Argparse supplies `-h/--help` for all commands and nested subcommands. Use
the `env/bin/` executable in the repository root. Each listed argument is
defined by the current parser; no version option is defined.

### Primary run commands

| Command | Positional inputs | Options (default) | Behavior |
|---|---|---|---|
| `crawler2-worker` | none | `--pool {browser,http,tor}` (required); `--max-claims INT` (unlimited); `--no-record` (false; benchmark-only) | `processes` comes from `CRAWLER2_WORKERS__<POOL>__PROCESSES`; SIGINT/SIGTERM stops pool. |
| `crawler2-extract` | none | `--exit-when-idle INT` (unset; test utility) | consumes page-observed events until signalled. |
| `crawler2-discover admit` | none | `--exit-when-idle INT` (unset; test utility) | consumes URL discovery events. |
| `crawler2-discover seeds FILE` | seed path | `--source NAME` (required), repeated `--meta KEY=VALUE`, `--every SECONDS` (one-shot if unset) | imports and admits seed URLs. |
| `crawler2-discover search` | none | `--queries FILE` (required), `--every SECONDS` (one-shot if unset) | executes configured search adapters and admits results. |
| `crawler2-discover decisions URL` | URL | none | prints current admission state and recorded decisions. |
| `crawler2-storage migrate` | none | `--keyspace NAME` (configured keyspace), `--lock-wait SECONDS` (60.0), `--skip-bucket` (false) | applies pending schema migrations and, unless skipped, ensures raw MinIO bucket. |
| `crawler2-storage status` / `check` | none | `--keyspace NAME` (configured keyspace) | JSON migration status; `check` exits nonzero if schema is not current. |
| `crawler2-storage relay` | none | `--keyspace NAME`; `--once` (false); `--sweep-hours FLOAT` (0); `--replay-from ISO`; `--replay-to ISO` (now) | relay loop; replay range takes precedence. Stop loop with Ctrl-C/SIGTERM. |
| `crawler2-check` | none | `--wait SECONDS` (0) | retry backend checks for up to the duration; returns 0 only if Redis, Scylla, and MinIO respond. |

### Media, telemetry, recovery, and offline tools

| Command | Syntax and options | Notes |
|---|---|---|
| `crawler2-media resolve run` | no arguments | one lease holder resolves `media.discovered`; Ctrl-C stops. |
| `crawler2-media resolve status` | no arguments | JSON lag/lease/gaps; exit 0/1/2 for healthy-warning/critical/retention-gap states. |
| `crawler2-media resolve accept-gap` | `--note TEXT` required; `--by NAME` optional | audited explicit acceptance of data loss; do not use as routine recovery. |
| `crawler2-media probe run`, `hash run` | no arguments | process internal queues; hash can emit P9 encode requests. |
| `crawler2-media encode sweep` | `--once` (false) | one sweep with report or continuous recovery sweep. |
| `crawler2-media representations run` | `--exit-when-idle INT` (unset) | projects P9 `representation.ready`/`encode.failed` events. |
| `crawler2-media rebuild MEDIA` | `MEDIA` ID or locator URL; `--since ISO` required; `--until ISO` defaults to now | rebuilds derived records for a time window; writes projections. |
| `crawler2-media show MEDIA` | media ID or locator URL | JSON registry/probe/hash view. |
| `crawler2-trace NEEDLE` | needle length >= 8; `--max-scan INT` (200000 per stream); `--json` | scans retained event streams; old events may be trimmed. |
| `crawler2-exporter` | no arguments | serves Prometheus scrape metrics; see `crawler2-exporter --help`. It is a process, not a dashboard. |
| `crawler2-backup create DEST` | repeat `--keyspace`, `--bucket`, `--repo`; `--scylla-container`, `--scylla-data-dir` (`/var/lib/scylla/data`), `--redis-container`, `--tag` | creates and verifies a new backup set; backend arguments are needed only for selected data. |
| `crawler2-backup verify DEST` | destination | read-only integrity verification. |
| `crawler2-backup restore DEST` | destination; repeat `--keyspace-map SRC=DST`, `--bucket-map SRC=DST`; `--scylla-container`, `--scylla-data-dir`, `--scratch` (`var/backup-scratch`) | restore refuses invalid backup and writes only to **new** destination names. |
| `crawler2-backup prune ROOT` | root; `--keep INT` required; `--yes` | defaults to dry-run; `--yes` deletes older backup-set directories. |
| `crawler2-soak prepare, start, dry-run CONFIG` | TOML config | preflight/start/dry run. Dry-run requires `dry_run=true` or synthetic mode. |
| `crawler2-soak resume, status, stop, report RUN_DIR` | run directory | resume/status/graceful stop/report; report can return nonzero for non-pass. |

The following reference includes the current parser options for the nested
operator CLIs. `-h/--help` is supplied by `argparse` at every command level.
Global `crawler2-filter --by NAME` must precede the subcommand. Other listed
flags belong after their command. These tools connect to configured services;
help does not require those services, but running a command usually does.

```bash
env/bin/crawler2-filter --help
env/bin/crawler2-filter import-abp --help
```

#### Filtering

Syntax: `crawler2-filter [--by NAME] COMMAND ...`; `--by` defaults to
`operator` and records import/activation provenance.

| Command | Arguments and flags (defaults in parentheses) |
|---|---|
| `import-v1 PATH` | `--manifest PATH` (unset), `--dry-run`, `--force` |
| `import-abp` | required `--source {easylist,easyprivacy,ublock}`; exactly one of `--url URL` or `--file PATH`; optional `--license TEXT`, `--dry-run`, `--force` |
| `import-builtin` | `--dry-run`, `--force` |
| `import-operator PATH` | `--dry-run`, `--force` |
| `publish` | repeatable required `--source NAME@REVISION` or `NAME@latest`; `--threshold FLOAT` (0.9); `--note TEXT` (empty) |
| `activate RULESET` | no additional flags |
| `rollback`, `status` | no additional flags |
| `explain URL` | `--source-url URL` (unset); `--context {link,request,redirect}` (`link`); `--type TEXT` (`document`, used for `request` context); `--ruleset ID` (active configured ruleset) |

`--dry-run` reports without writing; `--force` accepts a large rule-count
drop during import. ABP `--url` downloads a list and therefore needs approved
network access. The parser does not constrain `--type` to a fixed set.
`explain` reports the decision and candidate rules; an absent active ruleset
or non-HTTP(S) URL is an error. Example:

```bash
env/bin/crawler2-filter --by ops import-builtin --dry-run
```

To explain a decision, use an existing active ruleset or pass its ID with
`--ruleset`; the parser does not create or activate one.

#### Intelligence

| Command | Arguments and flags (defaults) |
|---|---|
| `sources run`, `sources rebuild`, `sources status` | no additional arguments |
| `sources report` | `--all` (false; include every host rather than only negative states) |
| `feedback run` | `--exit-when-idle N` (unset; otherwise stops after N empty polls) |
| `feedback rebuild` | `--months N` (3) |
| `feedback show` | `--url URL` (unset); `--base INT` (50) |

`sources run` and `feedback run` are consumers; stop them with Ctrl-C.
`feedback` uses P11 events, but the crawler feedback setting is disabled by
default. These commands do not accept a config-file or command-line service
override; they use `CRAWLER2_*` settings.

#### Evidence

| Command | Arguments and flags (defaults) |
|---|---|
| `collect run` | `--exit-when-idle POLLS` (unset) |
| `collect refresh EVIDENCE_ID` | `--finalized` (false) |
| `collect rebuild` | required `--target ID`; `--months INT` (3) |
| `list` | exactly one of required `--campaign ID` or `--target ID`; `--months INT` (3); `--status TEXT` (unset; parser accepts any string) |
| `show EVIDENCE_ID` | `--audit` (false); `--detail` (false; redacted provenance detail) |
| `validate EVIDENCE_ID` | `--dry-run` (false; do not store validation result) |
| `finalize EVIDENCE_ID` | `--allow-partial`, `--dry-run`; `--actor NAME` (`operator`) |
| `supersede EVIDENCE_ID` | `--allow-partial`, `--dry-run`; `--actor NAME` (`operator`); required `--reason TEXT`; `--no-recollect` (recollection is on by default) |
| `campaign create CAMPAIGN_ID` | required `--name TEXT`, `--since YYYY-MM`; repeatable `--target ID` (none) |
| `campaign add-target CAMPAIGN_ID TARGET_ID` | no additional flags |
| `campaign list` | no additional flags |
| `campaign stats CAMPAIGN_ID` | no additional flags |
| `export` | required `--campaign ID`; `--out PATH` (parser default unset; supply it for a non-dry-run export); `--archive`, `--no-artifacts`, `--dry-run` |

`collect run` consumes the `match.found` stream. `export --dry-run` previews
package contents without writing; normal export writes a new output
directory, and `--archive` also writes `<out>.tar.gz`. `--no-artifacts`
includes artifact references without copying artifacts. `finalize` and
`supersede` change evidence state; validate and review first. Example
read-only inspection:

```bash
env/bin/crawler2-evidence list --campaign CAMPAIGN_ID --months 3
env/bin/crawler2-evidence show EVIDENCE_ID --detail
```

#### Analytics

Global syntax is `crawler2-analytics [--root PATH] COMMAND ...`;
`--root` defaults to `CRAWLER2_ANALYTICS__ROOT` (itself `var/analytics` by
default) and precedes the subcommand.

| Command | Arguments and flags (defaults) |
|---|---|
| `export` | `--keyspace NAME` (configured Scylla keyspace); `--since ISO` (unset); `--until ISO` (now); repeatable `--dataset NAME` (all datasets); `--segments INT` (configured/default); `--verify-counts`; `--resume RUN_ID` (unset) |
| `status`, `datasets` | no additional arguments |
| `validate EXPORT` | `--revision INT` (current revision); `--quick` (skip file SHA-256) |
| `sql EXPORT [QUERY]` | `--revision INT` (current); `--file PATH` (unset); `--format {tsv,json}` (`tsv`); provide inline SQL or `--file` in practice; if both are set the file takes precedence |
| `report campaign EXPORT` | required `--campaign ID`; `--revision INT` (current); `--format {json,markdown}` (`json`); `--out PATH` (stdout); `--verify`; `--require-complete` |
| `report source EXPORT` | `--site DOMAIN` (all sites); `--revision INT` (current); same `--format`, `--out`, `--verify`, and `--require-complete` options |
| `graph build EXPORT` | `--revision INT` (current); `--replace` |
| `cluster run EXPORT` | `--revision INT` (current); `--min-pages INT` (3); `--min-similarity FLOAT` (0.6); `--min-samples INT` (3); `--max-sites INT` (2000) |
| `gc` | `--older-than-hours FLOAT` (24); `--force`; `--dry-run` |

Exports, SQL, reports, graph, and clustering operate on durable or local
analytics data as applicable. `cluster run` is explicitly a site/template
clustering prototype, not a production detector. `gc` removes only old
unpublished staging directories; `--force` includes staging runs still
marked running. Start with `--dry-run` and do not use it while an export is
active. ISO time boundaries are inclusive for `--since` and exclusive for
`--until`.

`crawler2-exporter` is a foreground metrics process with
`--no-scylla` (skip the outbox lag source), `--gpu` (read `nvidia-smi` on a
GPU host), and `--min-interval SECONDS` (15.0). It uses the configured
metrics port and exits nonzero if metrics cannot be served; Ctrl-C/SIGTERM
stops it. Example: `env/bin/crawler2-exporter --min-interval 30`.

#### Soak configuration and lifecycle

`crawler2-soak prepare|start|dry-run CONFIG` reads strict TOML
(`extra="forbid"`); `resume|status|stop|report RUN_DIR` takes the generated
run directory. No command has flags beyond argparse `-h/--help`.

| TOML field | Type/default | Behavior |
|---|---|---|
| `run_name`, `mode` | required; mode `synthetic` or `production` | Run identity and whether real topology or fake/generated workload is used. |
| `dry_run`, `duration_h`, `sample_interval_s`, `heartbeat_stale_s`, `run_root` | false, 168 hours, 60 seconds, 300 seconds, `var/soak` | Production runs must be at least 168 hours unless explicitly marked dry-run; a dry run is never M6-eligible. |
| `topology`, `repos`, `watch_paths` | empty map/lists | Recorded topology and code/config inputs fingerprinted during the run; changes invalidate the run. |
| `approval` | fields `approved_by`, `approved_at`, `reference`, all unset | All three are required for M6-eligible production preflight. |
| `thresholds` | optional limits default to `None`; `growth` is an empty map | Required but unapproved limits remain inconclusive; the harness does not invent pass thresholds. |
| `collectors` | empty list | Strict collector records; types are `host`, `process`, `gpu`, `prometheus`, `redis`, `http_health`, and `synthetic`; each has a name, options map, and series list. |
| `redis` | unset | Optional Redis source: host, port, db, namespace, credential environment-variable names, and stream prefix. |
| `edges`, `required_stages` | empty lists | Event reconciliation contracts and event types that must be observed. An empty required-stage list makes that criterion inconclusive. |
| `evidence` | enabled; validates 200 samples; 120-second timeout | Validation command defaults to `crawler2-evidence validate {id} --dry-run`; can be configured in TOML. |

`prepare` performs preflight without creating a run. `start` performs
preflight then runs until duration/stop; `dry-run` refuses an M6-eligible
production config. `stop` requests graceful completion of a round and
checkpoint, and `resume` records an interruption before continuing.
`report` writes JSON, Markdown, and HTML outputs. The examples under
`scripts/soak/` include synthetic mechanics checks and an unapproved
production template; they are not approval to run a soak. A PASS from a
synthetic/dry run is not production evidence.

The CLI references above describe parser behavior, not a guarantee that a
service-backed operation has been exercised in this environment. The help
commands remain the fastest way to confirm syntax for the installed version.

## Configuration reference

`crawler2.core.configuration.settings.Settings` is the sole typed settings
schema. Pydantic loads `CRAWLER2_<FIELD>` and nested
`CRAWLER2_<SECTION>__<FIELD>` variables; names are case-insensitive in the
environment. CSV-list fields accept comma-separated values. Unknown fields
are rejected (`extra="forbid"`). There is no YAML config or automatic `.env`
read for host processes, and no CLI config-file override. Compose reads
`.env` only to substitute Compose variables and explicitly supplies app
settings. In-process defaults apply when an env override is absent.

For example, `CRAWLER2_WORKERS__HTTP__CONCURRENCY=8` overrides the HTTP
worker pool concurrency; `CRAWLER2_REDIS__HOST=127.0.0.1` points a host
process at the loopback-published Redis port. In the app container use
`redis`, `scylla`, and `minio` service DNS names, not `localhost`.

| Setting path | Type/default | Purpose and validation |
|---|---|---|
| `environment` | `dev` (`dev`,`test`,`prod`) | Prod requires authenticated non-placeholder Redis/Scylla/MinIO credentials and forbids SSRF allowlists. |
| `service`, `host_id`, `roles`, `scratch_dir` | `crawler2`, `dev-1`, empty list, `/tmp/crawler2` | service/identity, worker role list (comma-separated), scratch only (not authoritative state). |
| `redis.host/port/db/username/password/namespace/socket_timeout_s` | `redis`/6379/0/none/none/`crawler2`/5 | frontier, clock and Streams; password is secret typed. |
| `scylla.contact_points/port/keyspace/local_dc/replication_strategy/replication_factor/connect_timeout_s/request_timeout_s/username/password` | `scylla`/9042/`crawler2`/`datacenter1`/SimpleStrategy/1/10/10/none/none | durable schema; contact points accepts CSV; keyspace syntax and positive/range constraints validated. |
| `minio.endpoint/secure/access_key/secret_key/bucket_raw/region/request_timeout_s/max_object_bytes/spool_memory_bytes` | `minio:9000`/false/none/none/`crawler2-raw`/`us-east-1`/30/256 MiB/8 MiB | object storage; production requires separate service credentials. |
| `events.stream_prefix/stream_maxlen/stream_max_bytes/relay_batch_size/relay_settle_s/relay_poll_interval_s/relay_retry_max_s` | `events:`/100000/64 MiB/500/600/1/30 | event stream names, bounded retention/backpressure, outbox relay. |
| `frontier.default_interval_s/lease_ttl_s/max_attempts/base_backoff_s/max_backoff_s/defer_delay_s/max_inflight_per_domain/promote_batch/recover_batch/recovery_interval_s/dead_ttl_s/dead_max/max_depth` | 1/90/3/5/300/10/2/256/200/30/7 days/100000; queue caps HTTP 200000, browser/Tor 50000, Selenium 10000 | Redis scheduling, lease/retry and per-domain rate/concurrency. `0` for max inflight means unlimited. |
| `workers.http`, `workers.browser`, `workers.tor` pool settings | fields `processes`, `concurrency`, `idle_poll_min_s`, `idle_poll_max_s`, `attempt_grace_s`, `shutdown_grace_s`, `report_retry_s`; processes default 1, concurrency 32/2/8 by pool, other defaults 0.2/2/5/20/0.5 | local pool capacity; does not replace shared domain politeness. |
| `workers.fetch.*` | user-agent `crawler2/0.1`; connect/read/total timeouts 10/15/30 s; redirects 10; page 5 MiB; manifest 1 MiB; media probe 64 KiB; sniff 256 KiB; proxy unset; SSRF CIDRs empty | HTTP limits. Timeouts positive; redirect limit 0..30; SSRF exceptions are not for production. |
| `workers.browser_engine.*` | contexts 2; pages/context 1; recycle 50; browser recycle 500; RSS 1200 MiB; navigation 30 s; settle 3 s; operation/acquire 10/30 s; launch 30 s; retries 3; body 5 MiB; blocked types image,font,media; headless true; proxy unset | Playwright lifecycle/network settings. Browser interception also consults filter settings. |
| `workers.tor_network.socks_proxy/probe_ports/health_interval_s` | unset / `[9050,9150]` / 30 | SOCKS endpoint; Tor pool does not start a Tor daemon. |
| `workers.network_health.*`, `workers.rate_limit_backoff.*`, `workers.recover_every_s` | enabled; trigger 10; timeout 5; built-in external probe endpoints; 5 s confirm, 15 s recovery, 2 rounds; 429 backoff enabled 5..600 s/min window 60; recover 30 s | outage pause and rate-limit recovery. Probe endpoints require outbound access. |
| `logging.level/format` | `INFO` / JSON | level `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`; format `json` or `console`. |
| `metrics.enabled/namespace/port/bind` | true / `crawler2` / 9100 / `127.0.0.1` | Prometheus `/metrics`; role-specific offset; expose only on a private interface if scraped remotely. |
| `limits.max_memory_mb/http_concurrency/browser_contexts/gpu_vram_mb` | 512/64/2/0 | resource guards; `encoder` role requires positive GPU VRAM limit. |
| `extraction.consumer_group/batch_size/block_ms/claim_idle_ms/archival_sample_rate` | `extraction`/50/2000/60000/0.05 | page-observed consumer; sample rate 0..1. |
| `filter.ruleset_name/reload_interval_s/browser_interception/decision_ttl_s` | `default`/15/true/90 days | ruleset runtime; actual threshold/policy is stored with immutable ruleset. |
| `discovery.consumer_group/batch_size/block_ms/claim_idle_ms/revisit_after_s/seed_revisit_s/scope_refresh_s/priority_seed/search/link/leaf` | `discovery-admission`/50/2000/60000/86400/21600/60/70/60/50/40 | stream admission, rooted scope, priorities (0..100). |
| `search.engines/max_results/timeout_s/blocked_cooldown_queries/pause_s` | `duckduckgo,bing,brave,ahmia`/20/15/999/2 | configured adapters; adapter/network availability varies. |
| `intelligence.*` | advice disabled; projection `sources`; poll 30 s; retry max 300 s; 60 buckets; retention 14 days; dedupe 200000; snapshot refresh/max age 300/1800 s; max lag 3600 s | P7 projection/advice; stale/missing advice falls back. |
| `feedback.*` | disabled; group `crawl-feedback`; batch 50; block 2000 ms; claim idle 60000 ms; snapshot refresh/max age 60/900 s; tau 14 days; source/pattern/total boost 15/5/20; exploration 20% | P11; admission priority boost is opt-in. |
| `media.*` | reader `resolver`; lease 60 s; poll 15; retry max 300; outbox retention 14 days; lag warn/critical 3/7 days; probe after 7 days; rehash 30 days; probe/hash concurrency 8/2; per-host 2 s/1; max hash 8 GiB; hash timeout/read 3600/60 s; queue block 2 s; SSRF allowlist empty; claim grace 300 s; sweep 60 s/lookback 2 days; group `media-registry`; batch 50/block 2000/claim idle 60000 | P8 resolution and media workers; cross-field validation ensures critical lag precedes retention expiry. |
| `evidence.*` | group `evidence-collector`; batch 50/block 2000/claim idle 60000; media/sources 50 each; sighting window 90 days; artifact/package cap 32/256 MiB | P12 collector/finalizer; package is assembled in memory. |
| `analytics.root/segments/max_retries/retry_backoff_s/duckdb_memory_limit/duckdb_threads` | `var/analytics`/64/3/2 s/`1GB`/2 | offline export workspace and bounded DuckDB execution. |

The schema supplies additional field-level numeric bounds and cross-field
checks; consult
[settings.py](crawler2/core/configuration/settings.py) before changing
operational values. Configuration examples in
[.env.example](.env.example) are Compose-oriented and do not enumerate all
settings.

## Normal operation

### Start a minimal local crawl

Complete [installation.md](installation.md) first. From the repository root:

1. Start the local backends and the idle app container.

   ```bash
   make up
   ```

2. Wait for dependency health and apply the explicit schema/bucket
   initialization. Migration is idempotent and is the only schema-changing
   command; repositories do not migrate on open.

   ```bash
   docker compose ps
   docker compose exec app crawler2-check --wait 180
   docker compose exec app crawler2-storage migrate
   docker compose exec app crawler2-storage check
   ```

3. Start consumers before the relay so stream groups are created before
   publishing. Run each in a separate terminal:

   ```bash
   docker compose exec app crawler2-discover admit
   ```

   ```bash
   docker compose exec app crawler2-extract
   ```

   ```bash
   docker compose exec app crawler2-storage relay
   ```

4. Start the HTTP pool in another terminal:

   ```bash
   docker compose exec app crawler2-worker --pool http
   ```

5. In a separate terminal, submit a deliberately small seed file:

   ```bash
   docker compose exec -T app sh -c \
     "printf '%s\\n' 'https://authorized.example.test/' > /tmp/crawler-seeds.txt"
   docker compose exec app crawler2-discover seeds /tmp/crawler-seeds.txt --source operator-smoke
   ```

   `.test` is reserved and this example demonstrates parsing/admission only;
   it is not expected to fetch successfully. For a real crawl, replace it
   with a URL you are authorized to access, or copy your UTF-8 seed file with
   `docker compose cp /path/to/approved-seeds.txt app:/tmp/crawler-seeds.txt`.
   The seed report confirms admission, not successful fetching.

6. Watch JSON logs in the foreground terminals. `crawler2-trace` can search
   retained events if you have an ID of at least eight characters. Check
   Scylla/media projections using their CLIs. There is no single “crawl
   finished” status for an unbounded frontier.

7. Stop foreground processes with Ctrl-C; they handle SIGINT/SIGTERM. Then
   stop the stack:

   ```bash
   make down
   ```

   `make down` stops containers and retains named volumes. Do not use
   `docker compose down -v` unless you intentionally want to erase the local
   Redis, Scylla, MinIO, and scratch volumes.

The exact commands are **code-verified**. A complete live crawl was not run
as part of this documentation task.

### Alternative fetch pools and bounded work

```bash
docker compose exec app crawler2-worker --pool browser --max-claims 10
docker compose exec app crawler2-worker --pool tor --max-claims 10
```

Each worker only claims the queue for its pool. Browser/Tor infrastructure
must be configured separately. `--max-claims` limits claims for a bounded
smoke run; it does not bound the number of seeds admitted or the duration of
each fetch. `--no-record` is for benchmark-only fetching and is not the
normal crawl mode. Concurrency is in `CRAWLER2_WORKERS__HTTP__CONCURRENCY`,
`...__BROWSER__CONCURRENCY`, and `...__TOR__CONCURRENCY`; process count is
configured similarly. Do not increase concurrency to evade a remote site's
limits.

### Filtering and discovery operations

`crawler2-filter` imports immutable source revisions, publishes rulesets,
activates/rolls back the active pointer, and explains URL decisions. Rule
imports may use built-ins, legacy V1 blacklist, ABP lists (local or HTTPS),
or an operator TOML file. Operator input is UTF-8 TOML containing `[[rule]]`
records. Every record requires non-empty `kind`, `pattern`, `classification`,
`reason`, and `note`. Accepted `kind` values are `host`, `host_label`, `path`,
`url_pattern`, `redirect`, and `selector`; classifications are `content`,
`media`, `player`, `navigation`, `ad`, `tracker`, and `unknown`. Optional
read fields are `confidence` (float 0..1, default 1.0), `action`
(`allow|classify|block`), `exception` (boolean, default false; exceptions
must allow), `types` (resource-type list, default all), `party`
(`any|first|third`, default `any`), `contexts` (`link|request|redirect`,
default all), and `domains` (source-domain list, default empty). Resource
types are `document`, `subdocument`, `script`, `image`, `stylesheet`,
`xmlhttprequest`, `media`, `font`, `ping`, `websocket`, `popup`, and `other`.
The TOML loader does not consume similarly named fields such as `enabled`,
`important`, `not_types`, or `not_domains`; do not rely on them in operator
files. Invalid enum values, empty patterns, out-of-range confidence, and
missing required fields fail import. Imports write provenance and may require
`--force` for a large rule-count decrease. Do not activate unreviewed rules
on a live deployment.

Minimal operator-file example:

```toml
[[rule]]
kind = "host"
pattern = "ads.example.test"
classification = "ad"
reason = "Example only"
note = "Replace with an approved, reviewed rule"
```

`crawler2-filter import-operator rules.toml --dry-run` parses and reports
without persisting the source revision.

P7 `crawler2-intel sources run` projects source health from the outbox;
`sources rebuild/status/report` inspect it. P11 `feedback run` consumes
`match.found`/target events, and `feedback show/rebuild` inspect/rederive
feedback. Advice and feedback-driven admission remain disabled unless their
settings are explicitly enabled.

### Media registry and Fingerprinter side

For P8/P9, keep resolver/probe/hash and Fingerprinter processes running as
separate services. `crawler2-media resolve status` reports outbox lag and
retention gaps; `crawler2-media show MEDIA` reads state; `representations
run` consumes Fingerprinter results. A hash result above
`media.hash_max_bytes` is not encoded. The registry issues at most one
logical request for the default spec and does not automatically re-request a
failed encode. The Fingerprinter service must use the same shared Redis
event prefix and compatible Scylla/MinIO services. See the Fingerprinter
[usage guide](../fingerprinter/usage.md).

### Complete supported local stack

`make up` starts Redis, ScyllaDB, MinIO, and one or two idle app containers
depending on the Compose profile. It does not run the crawler workers, stream
relay, extraction, admission, media registry, Fingerprinter, or Qdrant.
There is no one-command full pipeline. Start only the processes needed for
the workflow; for the URL crawl, the startup sequence above is sufficient.
For media encoding, also start:

```bash
docker compose exec app crawler2-media resolve run
docker compose exec app crawler2-media probe run
docker compose exec app crawler2-media hash run
```

Start these in separate terminals after storage migration. Start
Fingerprinter P9 intake/encoder/scheduler/reconcile roles as described in
its own guide. Startup order for integration: healthy services and migrated
schemas; create required stream consumer groups; run P9 intake; run
Fingerprinter P9 encoder/scheduler/reconcile; run crawler media resolver and
probe/hash pools; run crawler media representation consumer. Stop producer
loops before consumers and backends. This procedure is environment-dependent
and is not equivalent to a production deployment.

## Logs, health, and monitoring

- `crawler2-check [--wait SECONDS]` checks Redis, Scylla, and MinIO only. A
  healthy response does not mean a worker or queue is making progress.
- Compose health checks `redis-cli ping`, Scylla CQL readiness, MinIO
  readiness, and app backend connectivity. Use `docker compose ps` and
  `docker compose logs -f redis scylla minio app`.
- Applications emit structured JSON logs to stdout by default. Set
  `CRAWLER2_LOGGING__LEVEL` and `CRAWLER2_LOGGING__FORMAT`.
- Metrics are Prometheus `/metrics` endpoints on role-offset ports, enabled
  by default and bound to `127.0.0.1`; the exporter and dashboards are
  operational code/configuration, not evidence that Prometheus/Grafana are
  running or validated.
- `crawler2-media resolve status` is the resolver lag/gap diagnostic.
  `crawler2-storage status` reports schema state. `crawler2-trace` scans
  retained Redis event streams; it cannot recover trimmed events.
- No general API health endpoint, built-in log viewer, or hosted dashboard
  is provided.

## Operational safety

- Observe site terms, applicable law, robots/access restrictions, and
  explicitly configured rate limits. Keep frontier domain interval and
  in-flight caps conservative. 429/`Retry-After` widens local per-domain
  intervals; do not treat that as permission to continue aggressively.
- Production SSRF checks reject unsafe destinations; allowlisted CIDRs are
  for development fixtures and are refused in production configuration.
  Browser and media fetch paths are guarded in current code, but do not
  weaken the checks to reach private/third-party infrastructure.
- Redis frontier and event streams, Scylla records/outbox, MinIO objects,
  and Compose named volumes contain persistent state. `/tmp/crawler2` and
  configured analytics/backup directories are local files, not authoritative
  backend storage.
- Stop processes before maintenance. Never delete live data directories or
  Docker volumes as a routine restart. Backup restore writes to new names;
  backup prune with `--yes` permanently removes old backup sets.
- Analytics export performs full token-range scans and may consume
  substantial storage; analytics query/graph operations write to the
  configured local analytics root. Scylla source reads are documented
  read-only.
- Credentials belong in ignored local secret files or a secret manager, not
  in command history, committed files, or logs.

## Testing

| Category | Location/command | Dependencies and side effects | Limitations |
|---|---|---|---|
| Unit + contract | `make test`; full local gate `make check` | No backend services; pytest unit/contract suites, plus Ruff and mypy in `make check` | Browser-specific tests are opt-in; CI modifications in this checkout have not run remotely. |
| Browser contract | `RUN_BROWSER_TESTS=1 env/bin/pytest tests/contract/crawlers -q` | Chromium installed | Not run here. |
| Integration | `RUN_INTEGRATION_TESTS=1 env/bin/pytest tests/integration -q` | Disposable Compose services and integration env; test suites use throwaway keyspaces/namespaces | Do not point at production backends. |
| Stack / multihost simulation | `make validate-stack` / `scripts/validate-stack.sh` | Builds/runs both Compose profiles, migrates, writes test records and includes explicit test reset operations | Use only isolated development Compose project and volumes; not physical multi-host validation. |
| Feature suites | `scripts/test-extraction.sh`, `test-filter-discovery.sh`, `test-media.sh`, `test-intelligence.sh`, `test-feedback.sh`, `test-evidence.sh`, `test-analytics.sh` | Compose stack; scripts use feature-specific integration keyspaces/prefixes | `test-media-two-host.sh` and `test-media.sh` outage options have infrastructure side effects. Inspect scripts before using. |
| P9 cross-service | `scripts/test-fingerprinter-p9.sh` | Both repos, compatible contracts, stack, model/cache and FFmpeg | Environment-dependent; not run here. |
| Benchmarks | `docs/benchmarks.md`, `benchmarks/` | May fetch/benchmark; dataset and long-running workloads vary | Not routine install checks. |
| Soak | `crawler2-soak` and `scripts/soak/` | Dedicated config/data; long-running | Harness exists; required seven-day production soak not run. |

`pyproject.toml` configures pytest `testpaths=["tests"]`,
`addopts="-ra --strict-markers"`, and marker `integration`. `make test`
runs `env/bin/pytest`; `make check` runs Ruff check/format-check, mypy, and
pytest. Test exit 0 means the selected tests passed; skipped tests appear in
the pytest summary and do not prove the skipped service/browser workflow.
Rerun one failing file with `env/bin/pytest PATH -q`.

**Verified here:** `crawler2-check --help`, primary installed CLI help, and
the repository parser/source definitions were inspected. The test suites,
benchmarks, and Compose workflow were not run for this documentation task.

## Troubleshooting

| Symptom | Likely cause | Diagnose | Corrective action | Verify |
|---|---|---|---|---|
| `make up` refuses missing variables | `.env` absent or missing MinIO values | `test -f .env`; compare variable names to `.env.example` without sharing values | Copy example, replace dev credentials locally | `docker compose config -q` |
| App is healthy but does no crawling | App container deliberately sleeps after backend health check | `docker compose ps`; `docker compose exec app ps` | Start worker/consumers explicitly with their CLI | Foreground startup log and queue activity |
| `crawler2-check` exits 1 | Redis/Scylla/MinIO unavailable or credentials/endpoints differ | `docker compose ps`; service logs; `crawler2-check --wait 180` | Wait for readiness, correct `CRAWLER2_*`, resolve port/resource conflicts | `crawler2-check` exits 0 |
| Scylla restarts or readiness is slow | memory or Linux AIO budget; Scylla initialization | `docker compose logs scylla`; inspect host memory/AIO limits per `docs/installation.md` | provide required resources; adjust host AIO only with admin approval | Scylla Compose health becomes healthy; CQL check passes |
| `crawler2-storage check` reports pending migrations | Schema not initialized/current | `crawler2-storage status` | Run explicit `crawler2-storage migrate` against the intended keyspace | `crawler2-storage check` returns 0 |
| No frontier work | no admitted valid seed, worker pool mismatch, or discovery blocked it | inspect seed JSON, `crawler2-discover decisions URL`, and filter state | supply valid authorized seeds; run the matching pool; review rules/scope | worker claims work |
| Links do not lead to more URLs | relay/extraction/admission consumer absent or stream backlog | check those foreground logs, `crawler2-storage relay`, `crawler2-discover admit` | start missing consumer and relay; create groups before publishing | discovery logs show processed entries and frontier claims increase |
| Browser worker launch fails | Chromium/OS libraries absent or pool settings invalid | run Playwright install in worker's environment; inspect browser logs | install Chromium with Playwright's supported installer | bounded browser contract test passes |
| Tor pool has no network | no reachable SOCKS listener or wrong proxy scheme | inspect `CRAWLER2_WORKERS__TOR_NETWORK__SOCKS_PROXY`; check Tor listener independently | configure an authorized Tor SOCKS endpoint; never assume Compose provides it | harmless reachability test through configured proxy |
| Media resolver status reports a gap | outbox retention exceeded or cursor is behind | `crawler2-media resolve status` | reconcile retained history; explicit `accept-gap --note` records loss and is not recovery | status reports resumed state, with audited gap visible |
| Fingerprinter results never project | P9 unavailable, prefix/schema mismatch, or its event is held | inspect P9 logs, `crawler2-media representations run`, shared event prefix and consumer group | align compatible settings and create consumer group before P9 publication | projection shows ready/failed event |
| Metrics endpoint unavailable | metrics disabled, role process absent, port occupied/bind mismatch | inspect metrics settings and process logs | set a free role-offset port/private bind; avoid public exposure | curl local `/metrics` returns exposition text |
| Disk/memory pressure | persistent backend volumes, large object/analytics/backup output, excess concurrency | `docker system df`, filesystem usage, process/container metrics | stop workload; expand storage or reduce concurrency/output scope; preserve persistent data | rerun bounded workload and verify headroom |

Exact errors vary by dependency/version; no unverified log string is promised.

## Quick reference

```bash
# Verify local Python/tool setup
python3.12 --version
uv --version

# Start and check local services
make up
docker compose ps
docker compose exec app crawler2-check --wait 180

# Initialize (only after backends are healthy)
docker compose exec app crawler2-storage migrate
docker compose exec app crawler2-storage check

# Start a bounded worker
docker compose exec app crawler2-worker --pool http --max-claims 10

# Input, help, tests, shutdown
docker compose exec app crawler2-discover seeds /tmp/crawler-seeds.txt --source operator-smoke
env/bin/crawler2-worker --help
make check
make down
```

For initial setup, see [installation.md](installation.md). Historical phase
status and deployment limitations remain in [the project README](README.md)
and [phase plan](docs/v2-phase-plan.md).
