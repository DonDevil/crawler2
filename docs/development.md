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
- P4 fetch: `benchmarks/p4-fetch/run.sh v1-engines` measures each V1 engine
  on the P0 seeds; `run.sh gate` runs the exit gate (V1 hybrid chain vs V2
  workers on the fixed 691-URL workload `w691.txt`, same session). Both
  use the **live web** and route every engine through a byte-counting
  proxy (`countproxy.py`); V1 runs read-only from a `git archive`
  snapshot with its own venv.

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
