# P0 Audit — Foundations & baseline

Scope: only what P0 needs (dependencies, environment, benchmark harness).
Per-component audits for later phases happen in those phases.

## V1 inspected (`~/anti_piracy/crawler`, HEAD `2dfb542`)

| Component | Why inspected | Finding |
|---|---|---|
| `requirements.txt` | dependency inventory | 26 packages, **none pinned** (`aiohttp`, `playwright`, `selenium`, `scrapling[fetchers]`, `redis`, `psycopg2-binary`, …). V1 is not reproducible from its requirements; its `env/` is the only record. |
| V1 `env/` | can V1 run for the baseline? | Python 3.12.3; all engines importable; Playwright browsers present in `~/.cache/ms-playwright`. **`psutil` missing**, so V1's `--monitor-resources` records `null` CPU/RSS (as in all committed V1 reports). |
| `tests/benchmarks/` (README, `common.py`) | reuse the harness (plan A.1) | Frontier-only synthetic benchmarks (no real fetches). `ResourceMonitor` samples the **main process only** by default (browser children excluded) using instantaneous `psutil.cpu_percent`; peaks are unreliable. |
| `main.py` + `tests/report_lib.py` | crawl-level metrics | `main.py --runtime --monitor-resources --output` writes a JSON run report: timing, visited/processed/failed counts, throughput, resources, Redis stats, config. This is the reusable crawl benchmark. No per-engine, byte or media metrics in the report. |
| `crawler/hybrid_crawler.py` | per-engine attribution | Every completion logs `Processed (N): <url> [<status>] via <engine> chain=<e1> -> <e2>`, giving engine attempts and outcomes without code changes. |
| `core/config.py`, `utils/url_utils.py` | can V1 run without being modified? | `config.yaml` and `datasets/domain_blacklist.txt` are resolved **relative to cwd**, and the blacklist is **mutated at runtime** (plan D6). Running V1 in place would modify V1 → baseline runs from a `git archive` snapshot. |
| `storage/redis_media_evidence_store.py` | media-found metric | Unique media assets are in the sorted set `<namespace>:assets:all`. |
| `config.yaml`, `seeds/` | fixed seed set | Only `seeds/piracy_sites.txt` is non-empty (51 URLs); the other seed files are empty. V1's working tree has uncommitted `config.yaml` edits (concurrency 300, localhost Redis); the baseline uses committed HEAD plus explicit overrides. |
| `benchmark/results/burst_10m_*cc.json` | prior numbers | Earlier 10-min live runs (25–300 concurrency) exist but used a remote Redis, all seed files, and had no CPU/RSS; not reused as the baseline. |

## Fingerprinter inspected (`~/anti_piracy/fingerprinter`)

Only its dependency manifests (`requirements.txt`, `requirements-dev.txt`,
`pyproject.toml`):

- Runtime: `redis>=8,<9`, `requests`, and **unpinned** `torch`,
  `torchvision`, `transformers`, `Pillow`, `numpy`. Dev: `pytest>=8,<10`.
- It speaks Redis Streams (the reason for ADR-004's transport choice).
- P0 consequence: crawler2 pins redis-py 8.x (same major). The contract
  package has **no runtime dependencies**, so it can't conflict with the
  fingerprinter's ML stack (ADR-007).
- Its GPU stack stays out of crawler2; no GPU container in P0.

## Environment findings

| Finding | Impact / handling |
|---|---|
| `docker` is **Podman 4.9.3 (rootless) behind the `podman-docker` shim**; `docker compose` delegates to `docker-compose` 1.29.2 over the Podman socket. | Works for the whole P0 stack. Compose file uses only features both Docker Compose v2 and 1.29 support. `systemctl --user start podman.socket` is required (enable it to persist). Image `HEALTHCHECK` is ignored in OCI builds, so health checks are declared in compose. |
| Official `minio/minio` images are no longer pullable (Docker Hub and Quay deny access). | Using `pgsty/minio` (community rebuild of AGPL MinIO), pinned by release tag. Recorded in ADR-003. |
| Scylla/Seastar reserves 1.5 GB for non-Seastar memory by default and refuses to start in a 2 GB container. | `--memory 1400M --reserve-memory 512M` in a 2 GB container (design → resource budget). |
| Scylla's driver discovers the node's container IP; host-side Python clients cannot use the published port (`NoHostAvailable ['10.89.x.x']`). | Integration tests run **inside** app containers (the real topology). The port is kept for `cqlsh` / tooling from the host. |
| Host Redis (V1) already listens on `127.0.0.1:6379`. | Compose publishes on `16379/19042/19000/19001`, bound to 127.0.0.1. |
| `fs.aio-max-nr = 65536`. | Enough for a 2-shard dev node; CI raises it to 1048576. |
| cgroup v2 delegates `cpu memory pids` to the user. | `mem_limit` and `cpus` limits are enforced under rootless Podman. |

## Reusable V1 components for later phases (not ported in P0)

From plan A.1, confirmed present: Redis frontier Lua scripts + Redis TIME
(P3), claim/lease/heartbeat (P3), failure classifier + network health
(P4), deterministic `sha256(clean_url)` identity (P1), URL normalization
and media URL classification (P5), HLS/DASH parser (P5), search-engine
adapters (P6), Tor proxy config (P4), fingerprinter acquirer/encoder/matcher (P9–P10).

## Deliberately not ported in P0

Everything that executes a crawl: frontier, workers, fetchers, extraction,
media, filters, intelligence, evidence, bridge. The V1 benchmark harness is
**reused in place** (run from a snapshot), not copied, because its
frontier scripts target V1's `Frontier` API which V2 will not have; the
V2 benchmark harness is written against V2 interfaces in P3/P4.

## P0 risks

1. **Baseline is a live-internet crawl** (piracy sites change, go down,
   block): numbers are noisy and not repeatable to the page. Mitigation:
   fixed seed file, fixed commit and config, raw artifacts committed;
   compare V2 on the same seed set, same duration and concurrency, close in time.
2. V1 cannot report bytes/page (D1) → no byte baseline; V2 must measure it
   from P4 onward (`FetchResult`).
3. Podman/compose-1.29 vs. Docker/compose-v2 differences may appear in
   later phases (GPU passthrough for the encoder in P9 especially).
4. MinIO community image provenance (ADR-003).
