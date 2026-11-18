# WP1 Run 2 — Qdrant E-R1 capacity investigation (workstream D)

Date: 2026-11-18. Qdrant 1.15.1 (image digest `sha256:d122138f…`, unchanged from Run 1), disposable containers named `wp1r2-qdrant-*` with fresh volumes, gate limits (1.5 GiB RAM + 1.5 GiB memsw, 2 CPUs), unique ports, fresh random API key per run; the dev Qdrant (`fingerprinter-index-qdrant-1`, volume 908 MB) was never touched. All containers/volumes were removed after their runs (`docker ps -a | grep wp1r2` empty).
Raw: `../raw/qdrant/`. Scripts: `scripts/wp1r2_qdrant_er1_run.sh` (runs the **unmodified** `capacity_e_r1.py`), `wp1r2_qdrant_diag.py` + `wp1r2_qdrant_diag_run.sh` (diagnostics, **not a gate**), `wp1r2_cgroup_sampler.py`.
**The E-R1 acceptance rules were not changed** (no OOM; cgroup anon peak ≤ 1.35 GiB; query p95 ≤ 10 ms in phases 2, 3, 5 after 60 s warm-up; exact counts; R_req reached).

## D1 — reconstruction and reproducibility

`capacity_e_r1.py` is run exactly as in Run 1 (same corpus: 104,443 real DINOv2 segment vectors + seeded rotations; collection config of design §13.3: 768-d dot, int8 scalar quantization `always_ram`, HNSW m=32 / ef_construct=200, on-disk vectors and payload, 5 keyword + 1 integer payload indexes; phases 1–5; 760 target queries).
Four 1-copy results now exist: Run 1 steady p95 **10.111 / 10.171 ms**, Run 2 **17.318 ms** (quiet rerun, `e-r1-1copies.json`) and **10.776 ms** (repeat, `repeat-1x/`). The 17.3 ms run is a **single unexplained outlier** (its memory, rebuild p95 25.4 ms and all counts are in line with the others; the host was idle; no thermal event was recorded by the watchdog). It is reported, not discarded, and not explained. The reproducible 1-copy figure is **10.1–10.8 ms, i.e. just above the 10 ms line in 3 of 4 runs and well above it in 1 of 4**: the gate fails at 1× every time. The rebuild p95 is stable at 22.8–26.5 ms.
Audit note: the 2×/3× gate runs, all diagnostics and the payload-in-RAM test of the first pass were taken while other work (lint/tests/GPU probe) shared the host; they were **rerun on an idle host** and the first-pass files archived in `../raw/superseded-pass-1/qdrant/`. The reruns agree with the first pass within 0.2–6 % on every headline number (table below), so the first-pass conclusions stand.

## D5 — controlled scaling with the unmodified gate (Run 2, new)

| scale | N per generation / R_req | anon peak (rule ≤ 1.35 GiB) | OOM | steady p95 (rule ≤ 10 ms) | rebuild p95 | live-write p95 | verdict |
|---|---|---|---|---|---|---|---|
| 1× (Run 1, ×2) | 104,443 / 234,109 | 0.431–0.437 GiB | no | 10.111 / 10.171 ms | 23.3 / 22.8 ms | 34–36 ms | **FAIL** (latency) |
| **1× (Run 2, ×2)** | 104,443 / 234,109 | 0.4215 / 0.4423 GiB | no | **17.318** / **10.776 ms** | 25.4 / 26.5 ms | 39.4 / 39.2 ms | **FAIL** (latency) |
| **2× (Run 2 quiet; first pass)** | 208,886 / 448,217 | **0.656** (0.662) GiB | no | **25.3 ms** (24.7) | **98.4 ms** (103.1) | 48.3 ms | **FAIL** (latency) |
| **3× (Run 2 quiet; first pass)** | 313,329 / 662,325 | **0.8915** (0.903) GiB | no | **28.1 ms** (26.9) | **136.0 ms** (135.2) | — | **FAIL** (latency) |
| 6× (Run 1) | 626,658 / 1,304,649 | 1.439 GiB (rule failed) | **OOM-killed in rebuild** | not reached | 634 ms until death | 238 ms | **FAIL** (memory, incomplete) |

All other rules at 2× and 3×: no `oom_kill`, exact counts equal at every boundary, R_req reached, 0 client errors. Phase 2 ("warm") has no post-warm-up samples by the harness's own rule at these sizes (14 s phase), exactly as in Run 1.
**Highest verified capacity:** *no scale passes the full gate.* The **memory rule is verified through 3× (313,329 points per generation, anon 0.903 GiB)**; the **latency rule fails at every tested scale including 1×**. `index.max_points` stays at the provisional 250,000: no passing, gate-eligible report exists (CAP-8), so it is neither supported nor changed.
Run 1 did not run 2×/3× because the design's own reasoning (validation §13.5) says they cannot pass what 1 copy fails; the measurements agree and quantify it (25.3 ms at 2×, 28.1 ms at 3×).

## D2 — memory

Peak cgroup **anon** memory versus peak resident points R (two generations + media): 0.4215–0.4423 GiB @ 234,198, 0.656 GiB @ 448,292, 0.8915 GiB @ 662,410 (quiet reruns; first pass 0.434 / 0.662 / 0.903) — **linear, ≈ 1.18 KB per resident point + 0.178 GiB** (fitted on the first pass; the quiet reruns lie 1.3–3.0 % *below* the line, so the model is slightly conservative and was kept). The slope is consistent with int8 quantized vectors held in RAM (768 B) plus HNSW graph links (m=32) plus id/payload-index structures — a *consistent explanation, not a measured component breakdown* (Qdrant 1.15 telemetry was not instrumented per structure). Extrapolating to 6× (R = 1.30 M) gives **1.61 GiB**, above the 1.5 GiB limit, **which matches the observed OOM-kill** (anon 1.439 GiB at death, the page cache being reclaimable). Therefore the 6× OOM is explained by resident-point count × per-point quantized-vector/graph memory with two generations coexisting — not by vector count × dimensions (which would give float32 ≈ 3 KB/point and a very different number).
`file` (page cache) is separate: it fills the remaining cgroup space and is reclaimable; its size drives payload/original-vector read latency (below). Segment count, deleted-point overhead and optimizer activity were not separately instrumented.

## D3 — latency attribution at 1 copy (single variable at a time, `qdrant-latency-diag.json`, **not a gate**)

Same data and configuration as the gate; 400 sequential queries per row after warm-up; REST rows report Qdrant's own server-side time; recall@100 is measured against an exact brute-force top-100 over the same 104,443 vectors (mean / worst single query; the worst-case value reflects near-duplicate clusters and is identical across variants).

| config | variant | client ms p50 / p95 / p99 | server ms p50 / p95 / p99 | recall@100 mean / min |
|---|---|---|---|---|
| C0 gate config | V0_gate_params | 2.049 / 2.356 / 2.512 | 0.984 / 1.250 / 1.397 | 0.9681 / 0.08 |
| C0 gate config | V1_no_payload | 1.306 / 1.591 / 1.775 | 0.529 / 0.737 / 0.858 | 0.9681 / 0.08 |
| C0 gate config | V2_oversampling_1 | 2.020 / 2.387 / 2.486 | 0.973 / 1.231 / 1.304 | 0.9639 / 0.08 |
| C0 gate config | V3_hnsw_ef_64 | 2.105 / 2.561 / 2.876 | 1.011 / 1.283 / 1.516 | 0.9681 / 0.08 |
| C0 gate config | V5_rest_paced_2x5qps | 3.185 / 4.007 / 4.242 | 1.592 / 2.081 / 2.202 | — |
| C0 gate config | V4_grpc_harness_sequential | 6.126 / 6.641 / 7.014 | — | 0.9681 / 0.08 |
| C0 gate config | V4_grpc_harness_paced_2x5qps | 9.164 / 10.701 / 12.230 | — | — |
| C1 originals in RAM | V0_gate_params | 1.767 / 2.051 / 2.222 | 0.808 / 1.017 / 1.153 | 0.968 / 0.09 |
| C1 originals in RAM | V5_rest_paced_2x5qps | 2.501 / 3.067 / 3.437 | 1.072 / 1.377 / 1.593 | — |
| C1 originals in RAM | V4_grpc_harness_sequential | 6.168 / 6.853 / 7.141 | — | 0.968 / 0.09 |
| C1 originals in RAM | V4_grpc_harness_paced_2x5qps | 8.220 / 10.406 / 11.161 | — | — |

Findings (measured):
1. **Qdrant's own work is ≈ 1.0–2.1 ms per query at 1 copy** (server p50 0.98 ms sequential; p95 2.1 ms under the paced load). Disk is not the limiter at 1 copy: keeping the original vectors in RAM (C1) changed nothing (server 0.81 vs 0.98 ms; paced client p95 10.4 vs 10.7 ms).
2. **The 8–10 ms the gate measures is mostly client-side.** Through the `qdrant-client` gRPC client the harness uses, the same queries cost p50 6.1 ms back-to-back and 9.2 ms paced (p95 10.7 ms); through plain REST: 2.0 ms sequential and 3.2 ms paced (p95 4.0 ms). About 4–5 ms per query is Python/gRPC/protobuf handling of 100 hits with payload.
3. **The production query path is REST, not that client**: `fingerprinter/vindex.py` `search_batch` posts to `/collections/{name}/points/search/batch` with `with_payload: true`. So the gate's measurement method (gRPC client) is **not** the production path. This is a gate-definition/measurement-method finding. **It was not acted on:** the harness and the 10 ms threshold are unchanged, no pass is claimed, and a change to what E-R1 measures is a separately reviewed decision (see "Proposals").
4. Payload retrieval is ≈ 50 % of the server cost at 1 copy (no-payload server p50 0.53 vs 0.98 ms).
5. `hnsw_ef` 64 and oversampling have negligible latency effect at 1 copy; oversampling 1.0 lowers recall by 0.4 points here (1.1 in the first pass) for ≈ 0.01–0.08 ms saved — **not a worthwhile trade**; no matching-quality change is recommended.
6. The rebuild-phase p95 (22.8–26.5 ms at 1×, 98 ms at 2×, 136 ms at 3×) is measured while the optimizer builds a second generation and live writes arrive on the same 2 CPUs; it was **not** separately decomposed in this run (see "Remaining").

### Why 2× and 3× fail — steady-state scale diagnostic (one generation, REST, `qdrant-latency-diag-{2x,3x}.json`)

| scale | payload returned: server ms p50 / p95 (sequential) | **payload not returned**: server ms p50 / p95 | REST paced 2×5 q/s: server p50 / p95 | gRPC (harness client) paced p50 / p95 |
|---|---|---|---|---|
| 1× | 0.984 / 1.250 | 0.529 / 0.737 | 1.6 / 2.1 | 9.2 / 10.7 |
| 2× | 3.885 / 8.378 | 0.564 / 0.728 | 1.7 / 12.6 | 8.8 / 14.5 |
| 3× | 19.607 / 46.848 | 0.631 / 0.888 | 1.8 / 30.5 | 9.1 / 18.2 |

**Vector search is flat in scale (≈ 0.5–0.9 ms server p95 with no payload at 1×/2×/3×); the growth is entirely in returning the payload of the 100 hits** (control: identical queries with `with_payload=false` stay at 0.5–0.9 ms). **Confirmed:** the scale-dependent steady-state latency is payload retrieval, not HNSW search, quantization or rescoring. **Inferred, not measured:** the reason is random reads of the on-disk payload missing the page cache inside a 1.5 GiB cgroup that also holds 0.4–0.9 GiB of anonymous memory (no per-query I/O counters were collected in this diagnostic; a HDD-era Run 1 diagnostic did measure block reads).

## D4 — engineering options (evidence-based; none adopted)

| Option | Evidence | Matching-quality impact | Status |
|---|---|---|---|
| Re-measure E-R1 through the production REST path (or measure both) | 1×: client p95 9.8 → 3.5 ms | none (same retrieval) | **Proposal for separate review** — changes what the gate measures; owner/reviewer decision |
| Return only the payload fields the matcher needs / fetch payload for the final top-k | no-payload server p95 ≈ 1 ms flat from 1× to 3× vs 49 ms with payload at 3× | none if the same hits/scores are returned | **Product change** (`matching.py` consumes the full payload at line 701); needs design + tests; not done |
| Payload in RAM (`on_disk_payload=false`) | tested at 3× with one generation, twice (first pass and idle rerun): the rerun reached cgroup anon **1.488 GiB (> 1.35 rule; limit hit 68,896 times)** and the container then **disappeared at the 1.5 GiB limit** (client: connection refused) — consistent with an OOM-kill; docker's final `OOMKilled` flag was not captured in either run | none | **Rejected** within the 1.5 GiB budget |
| Originals in RAM (design option "D2 change") | no effect on latency at 1× (C1) | none | Not needed at 1×; memory cost not tested at scale (payload, not originals, is the limiter) |
| Larger memory limit (design D14), SSD index host, different latency criterion | listed in the design; not tested here | — | Owner decisions, unchanged |
| Lower `hnsw_ef` / oversampling | negligible latency effect, recall −1.1 pt for oversampling 1.0 | slightly worse | Not recommended |
| Bounded indexing concurrency / optimizer CPU budget (rebuild-phase p95) | not tested | — | **Remaining host-local work** |

## D6 — automated capacity protection

Implemented (tested): `wp1r2_hostguard.py qdrant-build --target-points N` — estimates peak anon memory from the fitted line above and **refuses** (FAIL, exit 2) a rebuild whose prediction × 1.10 exceeds the 1.35 GiB rule; `N = 250,000` (provisional cap) predicts 0.76 GiB → OK; 626,658 (the OOM scale) → FAIL; 1×–3× reproduce the measurements within 3 % (`tests/unit/test_wp1r2_hostguard.py`). The model is valid only for the gate configuration and covers memory, **not** latency (the binding constraint).
Not implemented: runtime monitoring of Qdrant memory pressure (the `containers` check + `wp1r2_cgroup_sampler.py` give the data, no controller exists), indexing-concurrency limiting and interrupted-index recovery (no evidence gathered; needs a design).

## Limitations and uncertainty

Rotated copies preserve norms and intra-copy geometry, so they are valid for memory and latency, not for recall (recall was measured at 1 copy only). The 2×/3× gate runs exist twice each (first pass and idle rerun, agreeing within 6 %); their failures are large (2.5–13×). The memory model has three scale points, each measured twice. The diagnostic's gRPC-vs-REST comparison is one 1× dataset on one thermally limited host (CPU pinned near its throttle point), which inflates client-side times for both clients; the ratio is the robust part.
