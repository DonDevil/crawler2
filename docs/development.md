# Development

Setup and everyday commands are in the [README](../README.md#quick-start).
This page covers running the test tiers, benchmarks, and the known limits
of the development machine.

## Test tiers

| Tier | Needs | Command |
|---|---|---|
| unit + contract | nothing | `make check` (ruff, mypy strict, pytest) |
| integration, all | compose stack | `make validate-stack` (runs `tests/integration` inside the app containers) |
| integration, one area from the host | compose stack | see below |

Integration tests are marked `integration` and skipped unless
`RUN_INTEGRATION_TESTS=1`. Tests that need only Redis (the frontier) can
run from the host against the published Redis port:

```bash
make up
CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=16379 RUN_INTEGRATION_TESTS=1 \
  env/bin/pytest -p no:cacheprovider tests/integration/frontier
```

Frontier tests use Redis db 9 and a random namespace per test, inject a
controllable clock (no sleeping on lease/backoff timers) and assert
`audit()` on teardown. The property-based state machine
(`test_state_machine.py`) runs 60 examples × 40 steps (~35 s).

### P7 crawl intelligence (host)

```bash
make up
scripts/test-intelligence.sh        # P7 + discovery unit tests, projection failure injection, admission
```

It uses the throwaway keyspace `crawler2_it_p7_host` and private stream
prefixes and frontier namespaces. The failure-injection test waits for
minute buckets to close, so it takes about 1.5 min.

P7 runtime use needs `crawler2-storage migrate` first (V004). The source
reader `crawler2-intel sources run` is started by the operator, one
instance; there is no compose service for it.

WHAT (increment 3) is an offline library with unit tests only
(`tests/unit/intelligence/test_what.py`, part of `make check`). Its
evaluation reads the M1 export and, for the label cards, the M1 snapshots
in MinIO, read-only:

```bash
env/bin/python benchmarks/p7-intelligence/what_sample.py population   # var/p7/what/pages.jsonl.gz
env/bin/python benchmarks/p7-intelligence/what_eval.py --split dev    # rule development only
env/bin/python benchmarks/p7-intelligence/what_bench.py               # CPU cost
```

The holdout split (`--split holdout`) is scored once per frozen policy
version. The G5 labels are never used (decisions.md §4).

WHEN readiness (no runtime code) has an offline audit over the same
export, read-only:

```bash
env/bin/python benchmarks/p7-intelligence/when_audit.py audit   # results/when-audit.json, var/p7/when/events.jsonl.gz
env/bin/python benchmarks/p7-intelligence/when_audit.py hubs    # results/when-hub-proposal.json
env/bin/python benchmarks/p7-intelligence/when_audit.py sample  # refuses below 50 candidate hub revisits
```

Another export (e.g. a future hub-only collection) is audited with
`P7_EXPORT=<dir> P7_WHEN_OUT=<dir>`.

### P8 media registry (host and two-host)

```bash
make up
scripts/test-media.sh                          # P8 unit tests + real Scylla/Redis suites
P8_SCYLLA_OUTAGE=1 scripts/test-media.sh       # also freezes Scylla ~25 s (outage test)
scripts/test-media-two-host.sh                 # rebuilds the image; C-11 and the two-host stages
```

`scripts/test-media.sh` uses the throwaway keyspace `crawler2_it_p8_host`
(recreated per session) and private Redis namespaces/stream prefixes. Its
resolver tests write `media.discovered` rows into private *future* outbox
buckets and drive an injected clock, so they never read another test's rows.
Two tests wait for real minute buckets to close (~3 min in total). The
two-host script uses keyspace `crawler2_p8_mh`. Results and the coverage
command: [P8 validation](phases/p08-media-registry/validation.md).

P8 runtime use needs `crawler2-storage migrate` (V005). Processes are
started by the operator (no compose service): see
[media-registry.md](architecture/media-registry.md).

### P4 fetch layer (host)

Fetcher contract, runtime, browser and end-to-end tests run **from the
host**: browsers live on the host (the app image has none), and the host
reaches Scylla directly at its container IP (the published port does not
work for the driver, the container IP does).

```bash
make up
env/bin/python -m playwright install chromium   # once; cached in ~/.cache/ms-playwright
scripts/test-crawlers.sh                          # contract + integration + browser tiers
RUN_BROWSER_TESTS=0 scripts/test-crawlers.sh      # without Chromium
scripts/test-crawlers.sh -k leak -s               # 1 000-page leak test with its report
```

The script reads `.env`, points Redis/MinIO at the published ports and
Scylla at the container IP, and uses the throwaway keyspace
`crawler2_p4_it` and bucket `crawler2-p4-it`. Without the stack,
`make check` still runs the fetcher contract suite (HTTP and Tor against
the fixture web and a SOCKS5 fixture) and the unit tests.

Scylla-backed tests must run inside the app containers (the Scylla driver
cannot use the published port from the host); the image bakes in `tests/`,
so run `make up` after changing tests. In containers add
`-p no:cacheprovider` (read-only root filesystem).

### P5 extraction (host)

```bash
make up
scripts/test-extraction.sh        # unit + storage integration incl. the page.observed → page.changed loop
```

Same approach as P4 (Scylla at its container IP). The storage suite uses the
throwaway keyspace `crawler2_it_p5_host` (dropped and recreated per run)
and bucket `crawler2-it-p5-host`. Golden extraction results are regenerated
with `UPDATE_GOLDEN=1 env/bin/pytest tests/unit/extraction/test_golden.py`
(only for an intended change, together with an `EXTRACTOR_VERSION` or hash
tag bump).

## Benchmarks

Benchmarks are manual tools, not CI. Each phase keeps its scripts and
committed raw results under `benchmarks/<phase>/`; the index with the
headline numbers is [benchmarks.md](benchmarks.md).

- P3 frontier: `benchmarks/p3-frontier/run.sh redis` starts a throwaway
  benchmark Redis (host network, port 16380, AOF off — V1's measurement
  setup), `run.sh all` runs everything, `run.sh stop` removes it. V1's
  scripts are run from `$V1_ROOT` with its own venv and
  `PYTHONDONTWRITEBYTECODE=1`, so V1 is only read.
- P2 storage: `benchmarks/p2-storage/run.sh` (needs `make up-two-host`).
- P8 media registry (exit gate): `benchmarks/p8-media/run.sh` runs the
  synthetic dedup benchmark from the host against the compose stack
  (keyspace `crawler2_p8_bench`, recreated per run; private Redis
  namespace; loopback fixture web; needs `ffmpeg`). Writes
  `benchmarks/p8-media/results/dedup-bench.json`; exits non-zero if the
  gate fails. About 30 s.
- P4 fetch: `benchmarks/p4-fetch/run.sh v1-engines` measures each V1 engine
  on the P0 seeds; `run.sh gate` runs the exit gate (V1 hybrid chain vs V2
  workers on the fixed 691-URL workload `w691.txt`, same session). Both
  use the **live web** and route every engine through a byte-counting
  proxy (`countproxy.py`); V1 runs read-only from a `git archive`
  snapshot with its own venv.
- P5 extraction: `benchmarks/p5-extraction/run.sh capture <urls> <dir>`
  captures HTML with the real P4 runtime into git-ignored `var/`;
  `run.sh parse <dir> [rounds]` times V1 `extract_content` (V1 venv,
  read-only, blacklist copied to scratch) and V2 `extract` on the same
  bytes; `pairs.py review|score` drives the change-detection evaluation.
- P6 filter: `benchmarks/p6-filter/throughput.py` (Gate D) and
  `fp_eval.py` (Gate E) read EasyList/EasyPrivacy from git-ignored
  `var/filter-lists/` and the P5 capture; `corpus_build.py` regenerates
  the labelled corpus fixture (deterministic).
- P6 M1: `benchmarks/p6-m1/run.sh setup|start [queries]|start-search FILE|restart-http|status|stop`
  runs the live closed loop on its own dataset (keyspace `crawler2_m1`,
  Redis namespace `m1`, streams `m1:events:*`, bucket `crawler2-m1`);
  `monitor.py` samples every 60 s into `var/p6-m1/samples.jsonl`,
  `report.py` summarises a window into `benchmarks/p6-m1/results/`.

### P6 filter + discovery (host)

```bash
make up
scripts/test-filter-discovery.sh    # unit + Scylla rule store + M1 fixture loop + Chromium interception
```

Throwaway keyspace `crawler2_it_p6_host`, bucket `crawler2-it-p6-host`.

## V1 is read-only

`~/anti_piracy/crawler` is the reference implementation and benchmark
source. Never edit, reformat or migrate it; run its tools read-only as
above.

## Development-environment limitations

- **Disk.** The Ubuntu development environment, including Docker's data
  root, runs from an external 5 400-rpm USB HDD. Scylla and MinIO are
  disk-bound on it, which is why **P2's 10× storage-latency gate is still
  open** (docs/phases/p02-storage/benchmarks.md). This is a property of
  the development machine, not of the P2 design. The machine's internal
  NVMe holds Windows and is intentionally not modified; no Docker data or
  volumes are moved to it. The P2 latency benchmark will be re-run under
  WSL on the NVMe or on another high-IOPS environment.
- **Redis** in compose runs with AOF on and a 1-CPU limit; frontier
  throughput numbers are therefore reported both for that Redis and for a
  benchmark Redis matching V1's setup (see benchmarks.md).
- Host: 12 cores, 15 GB RAM, RTX 2050 (4 GB); the full stack must fit
  alongside browsers.
