# P13 validation

Date: 2026-10-22. Host: the dev laptop, compose stack up (Scylla, MinIO,
Redis), branch `main`. Unit inputs are the **synthetic** world of
`tests/unit/analytics/world.py` (reserved `.example` hosts, contract-valid
fixture matches). The integration test writes synthetic rows through the real
repositories and the real P12 collector/finalizer into a throwaway keyspace.
**No real crawl data was exported and nothing was fetched.** No real matcher
output exists (P10 M3 not passed).

## 1. Commands and results

| Command | Result |
|---|---|
| `env/bin/pytest -q -p no:cacheprovider tests/unit/analytics` | **75 passed** (~80 s) |
| `scripts/test-analytics.sh -q` (unit + `tests/integration/storage/test_analytics.py` on the real stack, keyspace `crawler2_it_p13_host`) | **76 passed** (119 s) |
| `scripts/test-analytics.sh -q tests/integration/storage/test_schema.py tests/integration/storage/test_evidence.py` (regression: migrations, P12 real-stack flow whose fixture the P13 test reuses) | **84 passed** (175 s) |
| `env/bin/pytest -q -p no:cacheprovider` (whole default suite; stack tests skip) | **923 passed, 163 skipped** |
| `env/bin/ruff check .` · `env/bin/ruff format --check .` · `env/bin/mypy` | clean · all files formatted · no issues in 294 files |

Defect found and fixed during validation: the first full-suite run had 2
failures in `tests/unit/storage/test_events_unit.py` caused by the new CLI
tests — `crawler2-analytics` configured process-wide logging onto pytest's
captured stdout. The CLI now logs to stderr (its stdout is data) and the CLI
tests stub `configure_logging`; the re-run is the result above.

## 2. Coverage of the required test groups

| Group | Tests |
|---|---|
| Export (9.1) | `test_export.py`: hand-counted reconciliation of all 17 datasets (source rows, rejected, out-of-window, written, read-back, missing/unexpected keys); Parquet schema = catalog types; two date partitions + `unknown`; three sites + unknown site; `since` window; pagination with 1-row pages; segment count does not change content; empty dataset (zero-row file) and unavailable table; null optional fields and lists; exact duplicates collapsed and counted; conflicting duplicates fail, nothing published; per-segment retry; interrupted run → failed, resumable, same content id as a fresh run; unchanged re-export; changed source → rev 2 without merge, rev 1 intact; different boundary = different key; crash between rename and `CURRENT` repaired; manifest-less directory never read; publish lock; resume refuses another source; tampered/missing file detected; unknown schema version not read; verify-counts drift; writes only under the root. `test_datasets.py`: every mapper returns exactly the declared columns; catalog consistency; wire ids; timestamps; host/site normalization; malformed docs rejected; no sensitive columns; token ranges cover the ring disjointly |
| Reports (9.2) | `test_reports.py`: campaign and source reports equal hand counts; separate lifecycle states; reproducible `report_digest` across two independent exports; report names its revision/content id; missing datasets → `complete: false`; missing scope dataset / unknown campaign / unknown site → clear errors; empty export → null rates; reports run with the Scylla and Redis drivers made unimportable |
| Graph (9.3) | `test_graph.py`: typed directed site edges with provenance keys; match → content/target keeps verdict and confidence; unobserved match gets no page edge (counted); shared content = one undirected edge; stable ids across roots, `unchanged` rebuild; repeated records aggregate with weight; missing inputs → skipped edge types listed; nodes carry dimension labels/`known` |
| Clustering (9.4) | `test_cluster.py`: feature tokens from persisted facts; missing features; insufficient data; noise vs cluster; determinism and content-derived ids; `min_samples`; `max_sites` refusal; params validation; template groups; versioned, `unreviewed` outputs; AST check that clustering imports no P7 store/advice/feedback/storage module |
| Boundaries | `test_boundaries.py`: no crawler2 module outside `analytics` imports it; analytics imports no Redis/frontier/events/object store; `ScyllaRowSource` issues only `SELECT` with `token()` ranges |
| CLI | `test_cli.py`: status, validate, sql (+ error exit), campaign/source reports (`--verify`, `--require-complete`, markdown), graph, cluster, not-found exit, gc removes only failed/abandoned staging, catalog, all shipped `sql/*.sql` run |
| Integration (9.5) | `test_analytics.py` (real Scylla, 8 segments, `--verify-counts`): every dataset complete/empty, none unavailable (all migrations); `source_rows` = independent `SELECT count(*)` for **every** source table, rejected 0, re-count drift 0; `validate` ok; campaign report (2 matches, 1 confirmed, 2 candidates, latest versions complete + partial, 3 pages on 2 sites); E5 sources basis `content_verified` from the real P12 collector; source report; graph checks clean; re-export `unchanged`; one more page → revision 2 with +1 observation; revision 1 still validates |

## 3. Export completeness, idempotency, reconciliation (summary)

- Synthetic world: all invariants of design §6 hold for all datasets; the
  disclosed exclusions are 1 rejected observation (corrupt doc), 1 row per
  windowed table after the boundary, 0 duplicates.
- Real Scylla (throwaway keyspace): scanned rows equal an independent
  `count(*)` per table; recount drift 0.
- Idempotency: unchanged scope → `unchanged`, no files written (unit and real
  stack); changed source → new revision; failed runs leave no revision.

## 4. Report reproducibility example (synthetic fixture)

```bash
env/bin/python -m tests.unit.analytics.world var/p13-fixture        # SYNTHETIC export
K=$(ls var/p13-fixture/exports)
env/bin/crawler2-analytics --root var/p13-fixture validate $K
env/bin/crawler2-analytics --root var/p13-fixture report campaign $K --campaign synthetic-case --format markdown
env/bin/crawler2-analytics --root var/p13-fixture report source $K
env/bin/crawler2-analytics --root var/p13-fixture graph build $K
env/bin/crawler2-analytics --root var/p13-fixture cluster run $K --min-pages 1 --min-samples 2
```

Two runs of the campaign report gave the same `report_digest`
(`e4aaff5f…` on this machine).

## 5. Not run / untested paths

- **Real-data export (OD-P13-1).** The read-only export of the M1 history was
  prepared but its launch (a memory-capped `systemd-run --user` unit) was
  refused by this session's permission policy, so it was not run and no
  real-data numbers exist. Command for the owner:

  ```bash
  ip=$(sg docker -c "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' crawler2-scylla-1")
  mkdir -p var/p13
  systemd-run --user --unit=p13-m1-export --collect -p MemoryMax=4G -p MemoryHigh=3G \
    -p MemorySwapMax=0 -p CPUWeight=50 -p IOWeight=50 --working-directory="$PWD" \
    --setenv=CRAWLER2_SCYLLA__CONTACT_POINTS=$ip --setenv=CRAWLER2_SCYLLA__REQUEST_TIMEOUT_S=60 \
    /bin/sh -c 'env/bin/crawler2-analytics --root var/p13/analytics export --keyspace crawler2_m1 \
      --until 2026-10-06T00:00:00Z --verify-counts > var/p13/m1-export.log 2>&1'
  ```

  Expected: P7–P12 tables absent in `crawler2_m1` → those datasets
  `unavailable` (not empty); a source report is possible, a campaign report
  is not (no campaigns/matches).
- Performance at real volume, peak memory, and the clustering on real sites
  are unmeasured.
- Multi-host shared output location, concurrent exporters on different hosts
  (the lock is a local file) — untested.
- No coverage measurement (no coverage tool in the dev dependencies).

## 6. Repository

New: `crawler2/analytics/` (incl. `sql/`), `tests/unit/analytics/`,
`tests/integration/storage/test_analytics.py`, `scripts/test-analytics.sh`,
`docs/phases/p13-analytics/`, `docs/architecture/analytics.md`. Changed:
`pyproject.toml` (dependency, script, S608 ignore), `uv.lock`,
`requirements*.txt`, settings (`AnalyticsSettings`), `.gitignore`
(`*.duckdb`, `*.parquet`), README, `docs/architecture/README.md`,
`docs/architecture/intelligence.md`, `docs/development.md`,
`docs/v2-phase-plan.md`. No migration; no generated data tracked.
