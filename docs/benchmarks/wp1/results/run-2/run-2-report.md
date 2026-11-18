# WP1 Run 2 — host baseline completion, automated gate enforcement and blocker closure

Date: 2026-11-18. Host `<host>`: ASUS TUF Gaming F15 FX506HF, i5-11400H (6C/12T), 16 GiB RAM, RTX 2050 4 GiB, Samsung MZVLQ512HBLU-00B00 NVMe, Ubuntu 24.04.5, kernel 7.0.0-43-generic, Docker 29.9.3. Tools used: fio 3.36, smartmontools 7.4, nvme-cli 2.8 (approved and installed for this run), Python venvs of `crawler2/env`, `fingerprinter/env` (torch 2.13.0+cu130), Qdrant 1.15.1.
**Scope: this one host, single node, RF = 1.** No acceptance threshold, scope, method or workload was changed to obtain a result. Statuses: CLOSED / PARTIALLY_CLOSED / OPEN / BLOCKED / NOT_TESTED.

## 1. Executive outcome
Run 2 completed everything that can be measured on this host and recorded the rest as blocked. **No blocker is closed beyond what its scope allows; the Qdrant capacity gate (E-R1, FP-05) still fails at every tested scale; the P2 Scylla gate passed a third time; NVMe health is clean.** Details: `run-2-summary.md`, `blocker-assessment.md` (Run 2 section).

## 2. What was run, and in what order
A repo/evidence audit → NVMe SMART/thermal read-outs → canonical fio (3 repeats) → sustained-write investigation → CPU thermal probes → Qdrant E-R1 1×/2×/3× + diagnostics → GPU probe → Docker/Scylla/MinIO → storage model → automation (`wp1r2_hostguard.py`) → this documentation. The operator interrupted the run once and the host was rebooted twice (clean power-offs; unsafe shutdowns unchanged at 242). On resuming, **every dataset was audited for validity; anything contaminated by concurrent work, missing a counter, or taken without repetition was rerun** (table in section 17).

## 3. Host state and constraints honoured
No Windows partition or raw device touched; ≥ 20 GiB free kept (lowest ≈ 58 GiB); system Redis (6379) untouched; no Docker volume or project data deleted; no BIOS/fan/undervolt/power-profile/firmware change; no service or schedule created; no push; the 168 h soak and WP2 not started; `index.max_points` unchanged at 250,000. sudo was used only through an operator-entered password dialog (never stored) for read-only SMART/RAPL reads. Pre-existing user modifications (crawler2 `docs/development.md`, `tests/integration/crawlers/conftest.py`; fingerprinter `var/`) were left alone.

## 4. Workstream A — repo and evidence audit
Run 1 evidence in `docs/benchmarks/wp1/` was inventoried; its open items (no SMART, no fio, sustained-write cause unseparated, E-R1 at 1× only plus 6× OOM, Scylla on NVMe twice) define Run 2's targets. `docs/benchmarks.md` was out of date (P2 row said the gate was unmet); it was corrected (the NVMe local single-node RF=1 gate passed; HDD history kept; RF=3/multi-host open).

## 5. Workstream B — NVMe qualification → `nvme/nvme-qualification.md`
* SMART clean: critical warning 0, media errors 0, error log 0, spare 100 %, **percentage used 8 → 9 % during this run** (~1.1 TB written by the tests), unsafe shutdowns 242 (unchanged).
* Controller thermal management: TMT1 81 °C, TMT2 83 °C, WCTEMP 83, CCTEMP 85. The "82.85 °C" plateau is TMT2 being reached.
* fio (file-based, 10 s jobs × 3 repeats, all 45 runs valid, CV < 7 %): seq read **2,666 MiB/s**, seq write **1,701 MiB/s**, QD1 4 KiB read **11.9 k IOPS**, QD1 write **55.4 k IOPS**; random-write is a burst figure.
* Sustained write: cliff after **8.4–14.2 GiB** (5.4 in the superseded pass), at 52–75 °C and before the first ≥ 80 °C reading in every run; a run with **zero** controller thermal events (29 °C margin) shows the same cliff → **not thermal; consistent with write-buffer exhaustion** (not proven to be SLC; its size varies for an unidentified reason). Heat adds a modest later loss.
* Sustained sequential **read** throttles after ≈ 15–17 s at 2.8 GB/s (82.85 °C, −19 %); characterising it needs deliberate operation in the 81–84 °C zone (owner decision).

## 6. Workstream C — host thermals → `thermal/thermal-investigation.md`
CPU package pins at 92–96 °C even with one AVX worker; throughput saturates at 8 workers (444 ops/s; 12 add nothing), so **worker concurrency is not a thermal lever**. RAPL PL1 = 200 W / PL2 = 83 W against a 45 W design power and `powerprofilesctl` = performance → firmware limits far above design (confirmed); inadequate cooling is plausible, needs physical inspection (not tested). CPU recovers in ≈ 1 s; NVMe needs ≈ 39 s to ≤ 65 °C, 4.4 min to ≤ 60 °C, 16 min to ≤ 50 °C. Single-sample thresholds are unusable (1 s boost spikes to 86–95 °C at idle), so the guard uses a sustained-heat rule (≥ 95 °C for 3 consecutive 1 Hz polls, or ≥ 100 °C once).

## 7. Workstream D — Qdrant E-R1 → `qdrant/qdrant-investigation.md`
Unmodified `capacity_e_r1.py`, gate limits (1.5 GiB, 2 CPUs), disposable containers, idle host:

| scale | anon peak | steady p95 (≤ 10 ms) | rebuild p95 | verdict |
|---|---|---|---|---|
| 1× (Run 1 ×2; Run 2 ×2) | 0.42–0.44 GiB | 10.1, 10.2, **17.3 (outlier)**, 10.8 ms | 23–27 ms | FAIL (latency) |
| 2× (×2) | 0.656 / 0.662 | 25.3 / 24.7 ms | 98 / 103 ms | FAIL (latency) |
| 3× (×2) | 0.8915 / 0.903 | 28.1 / 26.9 ms | 136 / 135 ms | FAIL (latency) |
| 6× (Run 1) | 1.439 (OOM-killed) | — | — | FAIL (memory) |

* Memory is linear (anon ≈ 0.178 GiB + 1.18 KB × resident points) and predicts the 6× OOM (1.61 GiB vs 1.5 GiB limit) → encoded as the `qdrant-build` pre-flight check.
* **Confirmed:** the scale-dependent latency is payload retrieval (no-payload server p95 stays 0.7–0.9 ms from 1× to 3×; with payload 1.25 → 8.4 → 46.8 ms). **Inferred, not measured:** page-cache misses on the on-disk payload inside the 1.5 GiB cgroup.
* **Measurement-method finding (not acted on):** the gate measures through the gRPC `qdrant-client` (≈ 4–5 ms client overhead at 1×) while production (`vindex.py`) uses REST; recorded as a proposal for separate review, gate untouched.
* Payload-in-RAM fails the memory rule (anon 1.49 GiB; container died at the limit, twice).
* No scale passes; `index.max_points` stays 250,000 (neither supported nor changed).

## 8. Workstream E — GPU → `gpu/gpu-qualification.md`
CPU 6.35 fps (12 threads, batch 8, 98 % utilisation, 96 °C) vs GPU **42.53 fps** at batch 2 → **6.7×** (Run 1: 6.7×). CPU/GPU cosine min 0.99999994, max abs diff 5e-7, 0 external connect attempts. Batch 16 collapses to 26 fps; the GPU reaches 87 °C with software thermal slowdown (0x20) at batches 4 and 8 (throughput stable within 30 s). The project's own `throughput/v1` calibration already selects batch 2. torch runtime 2.13.0+cu130 matches the lock; the environment holds duplicate torch/torchvision/triton/cudnn `dist-info` directories (cause = hypothesis: interrupted `uv` upgrade) — **not changed**; recreating `fingerprinter/env` needs approval.

## 9. Workstream F — Docker and databases → `docker-database/docker-database.md`
P2 Scylla 10× gate on NVMe: **third pass** (16/16 p99 targets, 99.9–100 units/s/host, 0 failures, lag p99 2.54 s; Scylla cgroup current peak 1,466 MiB of 2 GiB, 0 OOM). MinIO's 512 MiB limit: **anon peak 284 MiB (55 %)**; the file cache fills the limit (6,616 reclaim events, 0 OOM, ≈ 50 MiB swap) → the limit holds under this load, with reclaim pressure. RF=3 / multi-host untested.

## 10. Workstream G — storage → `storage/storage-capacity-model.md`
Root 58 GiB free (usable above the 20 GiB floor ≈ 38 GiB); measured growth and temporary-space figures per workload; no production workload/retention exists in the register (H-08) so none is invented.

## 11. Workstream H — automation → `automation/README.md`
`scripts/wp1r2_hostguard.py` (stdlib, read-only; exit 0/1/2/3 = OK/WARN/FAIL/UNKNOWN): NVMe temperature and SMART health (`--sudo`), thermal events (delta vs baseline), free space after the planned operation, CPU sustained heat, GPU temperature (warn 87 / fail 95), container OOM/restart/health/memory %, Qdrant rebuild memory budget, combined `preflight`. **UNKNOWN is never healthy** (without sudo the SMART checks report UNKNOWN, exit 3). Safety thresholds, operational throttling thresholds and performance gates are kept separate; the performance gates are untouched. 33 unit tests. No service/timer created.

## 12. Gate results (nothing weakened)
P2 Scylla 10× (local, RF = 1): **PASS (3×)**. E-R1 1×/2×/3×: **FAIL (latency)**; 6×: **FAIL (memory/OOM)**. NVMe: no numeric floor is defined in the register → figures reported, no pass/fail invented. GPU numerical agreement: pass. Seven-day soak, RF=3 Scylla, multi-host topology, cross-machine GPU validation: **BLOCKED**. Redis HA, MinIO durability, network isolation: **NOT_TESTED** (outside WP1).

## 13. Root causes — confirmed vs hypotheses
| Finding | Status |
|---|---|
| Sustained-write cliff is not thermal | **Confirmed** (zero-event run, cliff precedes throttle zone in all runs) |
| Cliff = pseudo-SLC buffer exhaustion; size varies 8–14 GiB | Consistent, **not proven**; size cause unknown |
| CPU pins at the thermal limit because firmware PL1/PL2 (200/83 W) ≫ design 45 W | **Confirmed** (readable limits); cooling inadequacy **hypothesis** |
| Qdrant memory linear in resident points; explains 6× OOM | **Confirmed** (6 measured points) |
| Scale latency = payload retrieval | **Confirmed**; page-cache-miss mechanism **inferred** |
| Gate's gRPC client ≠ production REST path | **Confirmed** (measured both) |
| Duplicate torch dist-info from interrupted uv upgrade | **Hypothesis** |
| 1× E-R1 17.3 ms outlier | **Unexplained** |
| GPU clock reason 0x400 | **Not decoded** |

## 14. Engineering changes
New: hostguard + 33 tests, fio suite/summarizer, sustained-write + analyzer, thermal probe, GPU probe, Qdrant gate runner/diagnostics, cgroup sampler, SMART collector. Corrected: `docs/benchmarks.md`. Nothing in the product code, gates or thresholds was changed.

## 15. Remaining blockers
Physical infrastructure: H-03, H-04, H-09 (and the 168 h soak). Owner workload/retention: H-08. FP-05: no gate-passing configuration on 1.5 GiB. See `blocker-assessment.md`.

## 16. Limitations
One host, one boot lineage, one drive; 10 s fio jobs (bursts); no run held in the 81–84 °C zone; Qdrant 2×/3× use rotated copies (valid for memory/latency, not recall); P2 is 60 s × 3; thermal results depend on a laptop whose cooling was not physically inspected; wear (9 % used) read, not extrapolated.

## 17. Data audit — what was reused and what was rerun
| Dataset | Decision | Why |
|---|---|---|
| fio single-pass (20 s) | **Rerun** (3 × 10 s, 45 runs); old kept in `raw/nvme/fio-single-pass-before-fix/` | no repeat, variability unknown |
| Sustained-write A, B | **Rerun** (A′, B′) with SMART counters | counters missing; warmer starts |
| Sustained-write C | Reused | valid: cold, counters captured |
| CPU thermal probes | Reused | valid, unaffected by later work |
| GPU probe | **Rerun** with CPU leg | no CPU-side measurement; thermals mixed with other load |
| E-R1 2×/3×, Qdrant diagnostics | **Rerun** idle (1× rerun twice) | measured while other work shared the host |
| P2 Scylla / MinIO | Reused | clean, idle, complete |
| Storage model | Refreshed | snapshot updated |

## 18. Recommendation and next WP1 actions
Do not start WP2: host-local WP1 work is not exhausted — it needs the owner decisions below. (1) Permit a bounded sustained read/write test into 81–84 °C (the safety bound currently 81/82 °C was not raised). (2) Approve a reboot-reverting PL1 (e.g. 45 W) or `powerprofilesctl balanced` A/B test. (3) Approve recreating `fingerprinter/env` from `requirements-p9.lock`. (4) Review the E-R1 measurement-method proposal (gRPC client vs REST). (5) Approve returning only required payload fields (product change) — the lever that removes the scale-dependent latency. (6) Provide a workload/retention model (H-08) and a target topology (H-01/H-03/H-04).

Large raw `.jsonl` time series (> 1 MiB) are gzipped in place (`*.jsonl.gz`); the NVMe serial number and host name are redacted in committed files.

### Appendix: scripts, tests, commands
Scripts (`scripts/`): `wp1r2_hostguard.py`, `wp1r2_fio_suite.sh`, `wp1r2_fio_summarize.py`, `wp1r2_sustained_write.py`, `wp1r2_sustained_analyze.py`, `wp1r2_thermal_probe.py`, `wp1r2_gpu_probe.py`, `wp1r2_qdrant_er1_run.sh`, `wp1r2_qdrant_diag.py`, `wp1r2_qdrant_diag_run.sh`, `wp1r2_cgroup_sampler.py`, `wp1r2_nvme_smart_collect.sh`.
Tests executed: `pytest tests/unit` → **998 passed** (incl. 33 in `tests/unit/test_wp1r2_hostguard.py`); `ruff check` + `ruff format --check` on all new files → clean; `mypy` (whole project, 332 files) → no issues; `bash -n` on the four shell scripts → OK.
Key commands: `wp1r2_fio_suite.sh OUT WORKDIR 4g 10` (×3 with `REP=-rN`), `wp1r2_sustained_write.py --label A|B|C [--rate-mib 500]`, `wp1r2_qdrant_er1_run.sh <copies> <outdir> 300`, `wp1r2_qdrant_diag_run.sh <outdir> [copies]`, `wp1r2_gpu_probe.py out.json` (fingerprinter venv), `COMPOSE_PROFILES=two-host RATE=100 DURATION=60 benchmarks/p2-storage/run.sh latency`.
