# P9 Audit — fingerprinter service: media-centric encoder + target manager

Status: **audit complete (2026-10-10).** Design approved 2026-10-10 and
implemented 2026-10-10/11 ([exit.md](exit.md)); the audit text below records the
state *before* P9 and is not updated (e.g. the NVML mismatch of §8 has since
been resolved). §1–§10 record the repository as found and are not
changed by later design work. Design: [design.md](design.md). Decisions and open questions:
[decisions.md](decisions.md). Requirement status:
[traceability.md](traceability.md). Data and benchmark preparation:
[data-preparation.md](data-preparation.md).

Scope: §P9 of [the phase plan](../../v2-phase-plan.md), B.1–B.5, the
crawler2 ↔ fingerprinter boundary (ADR-011, P1 contracts, P8), and the
fingerprinter repository (`~/anti_piracy/fingerprinter`, V1). Read-only,
except one CPU benchmark run with the existing harness (§7). No service was
started or stopped, no keyspace or stream was written, and nothing was
crawled.

## 1. Repository state at the start

| | crawler2 | fingerprinter |
|---|---|---|
| Branch | `main`, ahead of `origin/main` by 29 (not pushed) | `main`, level with `origin/main` |
| Head | `2e397c1 bench(p8): synthetic dedup exit gate, p8 exit and closure` | `904af14 new flag --runtime` |
| Working tree | clean | clean (ignored only: `env/`, `__pycache__/`, `.pytest_cache/`) |
| Tests | P8 closure: 774 passed / 156 skipped (`make check`), 126 media integration | 408 `def test_` functions in `tests/` (not run in this audit) |
| Python / deps | 3.12, uv, `./env` | 3.12, `env/`; `requirements.txt`: redis, requests, torch, torchvision, transformers, Pillow, numpy (unpinned). **No Scylla driver, no S3/MinIO client, no `antipiracy-contracts`, no pydantic** |
| Container image | `localhost/crawler2-app:dev` | none (no Dockerfile) |

The V1 crawler (`~/anti_piracy/crawler`) has unrelated uncommitted changes;
it is out of scope and was not touched.

## 2. What the plan asks of P9

From §P9: the job model changes from `(candidate, target)` to
`encode(media)`; a representation registry keyed by
`(content_identity, media_version, model_name, model_version,
sampling_config_hash)` in the fingerprinter keyspace; vectors in MinIO, index
in P10; model upgrade = backfill, old representations kept until retired; the
13D multi-host fix (target media and artifacts in MinIO, build-on-miss under a
Redis lock, no host-local authoritative cache); the RTX 2050 GPU plan
(fp16 `facebook/dinov2-base`, adaptive batching, one GPU process per host
with a Redis lease, CPU decode threads feeding a bounded queue, CPU
fallback, fp16/fp32 ≥ 0.999 cosine and identical decisions). Tests: the S1/S2
reuse scenario (plan input `gpt-disscussed-plan.md` §4: S1 encodes A–E; S2
finds C–G; C, D, E must not be re-encoded), fp16/fp32, OOM recovery, GPU
throughput + VRAM, two-host GPU/CPU identical representations. Exit gate:
reuse scenario passes; GPU ≥ 5× CPU throughput (**target to be confirmed at
design review**); no OOM crash in a 1,000-video soak.

## 3. Frozen boundary (crawler2 side)

### 3.1 Contracts (P1, `contracts/src/antipiracy_contracts/`)

| Fact | Evidence |
|---|---|
| `RepresentationSpec` = `name`, `version`, `config_digest` (a `ContentDigest`, "digest of the full encoder configuration (sampling, preprocessing, precision, ...)"); opaque to crawler2 | `models/representations.py` |
| `RepresentationId.for_content(content_id, spec.name, spec.version, spec.config_digest)`; `Representation` validates that its ID is derived exactly so | `ids.py:236`, `models/representations.py` |
| `RepresentationId.for_target(target_id, target_version, name, version, config)` exists; `MatchResult` carries both `representation_id` and `target_representation_id` | `ids.py:242`, `models/matching.py:44` |
| `Representation` has `content_id` (required), `segment_count`, `media_duration_seconds`; vectors are not in the contract | `models/representations.py` |
| `EncodeRequested`: `content` (ContentKey), `source` (Media), `referer`, `required_spec` (absent = "the fingerprinter's current default spec"), `priority` 0–100. Docstring: "the registry picks one and may re-request with another after a failure" | `events/fingerprinting.py` |
| `EncodeFailed`: `request_id`, `content_id`, `failure` ∈ {source_unreachable, source_forbidden, unsupported_format, too_large, decode_error, timeout, internal_error}, `retryable`, `attempt ≥ 1`, optional `spec`, `detail` | same |
| Catalog: `encode.requested` producer MEDIA_REGISTRY → ENCODER, idempotency `(content.content_id, required_spec)`; `representation.ready` ENCODER → MEDIA_REGISTRY, MATCHER, key `representation.representation_id`; `encode.failed` ENCODER → MEDIA_REGISTRY, key `(request_id, attempt)`, "a later representation.ready supersedes any failure"; `target.registered/retired` TARGET_MANAGER → CRAWL_INTELLIGENCE, MATCHER; **`match.found` MATCHER → FEEDBACK, EVIDENCE_COLLECTOR** (not the media registry) | `events/catalog.py` |
| `TargetId` is an allocated ID (`tgt_…`, UUIDv7); `TargetVersion` is an int ≥ 1 | `ids.py:277`, `models/targets.py` |
| P1 catalog for the fingerprinter keyspace: F1 representation by `(content_id, spec)` ("strong enough to avoid duplicate GPU work (LWT or a lease in Redis)"), F2 all representations of a spec by `(spec, hash bucket)` for backfill/index rebuild, F3 target versions, F4 target representations `(target_id, version, spec)`, F5 matches, F6 ANN index (derived) | `docs/phases/p01-contracts/access-patterns.md` §5 |

### 3.2 P8 media registry (`crawler2/media/`, complete, frozen)

| Fact | Evidence |
|---|---|
| Level 3 = `full-bytes/v1`: SHA-256 of the served **identity** bytes (`Accept-Encoding: identity`, raw read, no content decoding); `ContentId.of("full-bytes/v1", digest)` | `crawler2/media/hashing.py:1-22`, P8-D5 |
| One logical `encode.requested` per `(ContentId, 'default')`: LWT claim in `encode_claims`, stored envelope, sweeper re-emits the same `event_id` | `crawler2/media/encode.py:1-26`, P8 design §7.1 |
| `required_spec` is always absent; `spec_key(None) = 'default'`, otherwise `name/version/config_digest` | `crawler2/storage/scylla/projections.py:43-50` |
| Suppression: the `default` row is `requested`, **or any spec row is `ready`** | `crawler2/media/encode.py:210-222` |
| After `encode.failed`, nothing is re-requested (P8-D7); the `default` claim is write-once, so crawler2 **never** re-requests a content (neither after failure nor after a model change) | `encode.py:201-208`, P8-D7 |
| Projection state per `(content_id, spec_key)`: READY if a representation doc exists, else FAILED if `failed_attempt` set, else REQUESTED. `encode.failed` lands on `spec_key(f.spec)`; `representation.ready` lands on its concrete spec | `projections.py:106-127`, `:175-208` |
| `representation.ready` / `encode.failed` are consumed by group `media-registry` (`MediaSettings.consumer_group`), created at ID `0` with `MKSTREAM` | `settings.py:423`, `storage/events/consumer.py:118-123` |
| Stream key = `events:` + `<type>.v<major>` "shared with the fingerprinter" | `settings.py:110`, `publisher.py:77-79` |
| P8 handoffs to P9: re-request semantics (P8-D7/F-3), default spec identity, acquisition from any media of a ContentId, whether the fingerprinter's publisher applies ADR-022 to the `media-registry` group | `docs/phases/p08-media-registry/exit.md` §7 |

### 3.3 Event transport (ADR-022)

The relay publishes every outbox row; for a stream **with consumer groups**
the publisher never trims unacknowledged entries and refuses new ones at
`stream_max_bytes` (64 MiB) with `StreamBackpressureError`; the relay then
**ends the whole cycle** (`relay.py:140-175`), so one blocked stream holds back
every event type. Streams without groups keep ≤ `stream_maxlen` (100,000)
entries / 64 MiB, oldest dropped. Recovery of history: `crawler2-storage relay
--replay-from ISO --replay-to ISO` (`storage/cli.py`, `relay.py:218-225`)
re-publishes outbox rows (retention 14 days).

## 4. Fingerprinter repository — current architecture

### 4.1 Data flow today (V1 job model)

```
V1 crawler bridge ──XADD──► fingerprint:jobs:stream:<priority>  (group fingerprinter-workers)
                              │  Job = (job_id, media_evidence_id, media_url, media_type,
                              │         source_domain, target_id, target_version, techniques, …)
                              ▼
worker/main.py → Worker (worker/fingerprint_worker.py: claim, lease 30 s, retry ZSET, ack)
   → worker/matching_handler.py:221 handler(job)
        acquire candidate (acquisition/acquirer.py → temp file)
        embed candidate: engine.embed_video_segments(artifact)        ◄─ every job
        _resolve_target_segments (matching_handler.py:320):
            registry.get_or_build_segment_embedding(...)              ◄─ cache + Redis lock
        match_segments(...) → Result → fingerprint:result:* / results stream
        artifact.cleanup()   (candidate bytes and vectors discarded)
```

### 4.2 Findings

| # | Finding | Evidence |
|---|---|---|
| A-1 | **Candidates are re-acquired and re-embedded for every (candidate, target) job; candidate representations are never persisted.** The plan's D11 defect is confirmed in code | `matching_handler.py:241` (acquire), `:265` (embed), `finally: artifact.cleanup()` |
| A-2 | Target segment embeddings are persisted (JSON, one file per `(target_id, target_version, content_sha256, spec)`) and reused across jobs | `target/segment_cache.py`, `target/registry.py:500-571` |
| A-3 | Encoding and matching are coupled in one handler; the target build runs inside a candidate job (build-on-miss) | `matching_handler.py:286-292`, `:334-345` |
| A-4 | **Target records are authoritative in Redis** (`fingerprint:target:{id}:{version}` hashes, content index, write-only embedding metadata). Under B.4 Redis is coordination only | `target/registry.py`, 13D audit §2 |
| A-5 | **Default storage is host-local** (`TARGET_CACHE_PATH=./target_cache`). The 13D "fix" (`SharedFilesystemEmbeddingCache`, `SharedTargetMediaStore`) is a *shared filesystem directory* behind the opt-in `SHARED_ARTIFACT_STORE_PATH`; it requires an NFS-like mount that does not exist here. It is **not MinIO** and was validated only as "single-process simulated multi-host" | `worker/main.py:370-395`, `target/shared_storage.py:1-50`, 13D impl doc status line |
| A-6 | Build-on-miss lock = `SET NX PX` + CAS release; **no renewal, no fencing token, no ownership check before the write**. TTL 600 s; losers poll 600 s then raise `TimeoutError` → `TransientFailure` | `target/lock.py`, `registry.py:60-62`, `:548-571` |
| A-7 | A build longer than the lock TTL lets a second host build the same target; writes are atomic replace (`os.replace`) of the same key, so duplicates waste work and are last-writer-wins, but do not corrupt | `registry.py:551-559`, `segment_cache.py` (`_atomic_write`) |
| A-8 | Worker-path target builds use the 300 s ffmpeg timeout. The only real target (`Blast.mp4`, 2 h 21 min HEVC 1920×800) **failed both recorded jobs at 306.9 s / 313.1 s** (ffmpeg timeout); only the operator CLI build is unbounded | `embedding/frames.py:33`, `target-eager-build-audit.md` A.3 |
| A-9 | The V1 job contract, stream-per-priority and result model are superseded by ADR-011 (not ported) | ADR-011 Consequences |

### 4.3 Embedding engine (`embedding/dinov2_engine.py`)

| # | Finding | Evidence |
|---|---|---|
| E-1 | Model `facebook/dinov2-base` pinned to revision `f9e44c814b77203eaa57a6bdbbd535f21ede1415`; `local_files_only=True`; weights present in `~/.cache/huggingface/hub` | `dinov2_engine.py:44-45`, `:114-117` |
| E-2 | **Batch size 1**: one forward pass per frame, and `torch.cuda.empty_cache()` after every frame on CUDA | `:330-348` |
| E-3 | **FP32 only**; no dtype/autocast option | whole module |
| E-4 | Device `auto` silently falls back to CPU when CUDA is unavailable; `cuda` raises `DeviceUnavailableError` | `:135-145` |
| E-5 | CUDA OOM (`torch.cuda.OutOfMemoryError` ⊂ `RuntimeError`) is wrapped as `InferenceError`; the candidate path turns it into `Result(PROCESSING_FAILURE)`, the target path into `TransientFailure`. No batch reduction, no recovery | `:336`, `matching_handler.py:266-276`, `:366-372` |
| E-6 | Output: CLS token `last_hidden_state[:, 0]`, L2-normalized (`normalize=True`), float64 → Python floats in JSON. Coarse vector = mean of segments, re-normalized | `:340-356`, `:303-312` |
| E-7 | Frames are extracted by one ffmpeg subprocess to PNG files in a temp dir, then loaded one at a time; decode and inference are sequential in one thread | `embedding/frames.py`, `:270-290` |
| E-8 | `model_version` is the revision hash (immutable); `model_id` is the repo name | `:88` |

### 4.4 Sampling and identity (`embedding/config.py`, `embedding/frames.py`, `target/versioning.py`)

- Segment sampling: `ffmpeg -vf fps=1/segment_duration_s` over the whole file,
  no frame cap, one frame per segment; default `segment_duration_s = 5.0`
  (documented as a **provisional, unvalidated heuristic**). Deterministic for
  fixed bytes, ffmpeg build and config.
- Segment times are **assumed** (`start = index × d`, `end = start + d`), not
  read from frame PTS; the last segment's end overstates duration; a non-zero
  stream start time or VFR is not reflected (`dinov2_engine.py:281-299`).
- `EmbeddingSpec` = model_id, model_version, embedding_schema_version,
  preprocessing_config, sampling_config; `spec_key()` = SHA-256 of sorted JSON
  **truncated to 16 hex chars** (`versioning.py:49-60`).

**Hidden identity inputs (not in `EmbeddingSpec`), each able to make
"identical" representations differ:**

| # | Input | Status |
|---|---|---|
| H-1 | **Preprocessing implementation.** `PreprocessingConfig` is recorded but **never applied**: the HF processor does the work. Under transformers 5.15.1 `AutoImageProcessor` loads `BitImageProcessor` with the **`TorchvisionBackend`** (verified this audit), i.e. torchvision tensor resize, not PIL. Backend and torchvision version are not recorded | verified (one `from_pretrained` call, local cache) |
| H-2 | `normalize` (L2) flag and pooling (CLS vs pooler/mean of patches) | implied by `embedding_schema_version` only |
| H-3 | Decoder and pixel conversion: ffmpeg version (6.1.1), YUV→RGB matrix/range for untagged streams, swscale defaults, autorotate | not recorded |
| H-4 | fps-filter rounding and start-time handling | not recorded |
| H-5 | Compute device and dtype (fp32 CPU vs CUDA; fp16 later) | not recorded |
| H-6 | torch/transformers/torchvision/Pillow/numpy versions (unpinned in `requirements.txt`) | recorded only in benchmark JSON |

### 4.5 Acquisition (`acquisition/acquirer.py`)

| # | Finding | Evidence |
|---|---|---|
| Q-1 | `requests` with default `Accept-Encoding` and `iter_content` (**content-decoded**). Its `checksum_sha256` is therefore not guaranteed equal to `full-bytes/v1` for content-encoded responses | `acquirer.py:194-197`, `:238-258` |
| Q-2 | `max_bytes` default **100 MiB** (`MEDIA_MAX_BYTES`); P8 hashes up to 8 GiB. A full-length film requested by P8 would fail as too large | `:46`, `worker/main.py:212` |
| Q-3 | No `Referer` header support (the contract carries `referer`) | `:197` |
| Q-4 | SSRF guard and manual redirects exist (ported to crawler2 by P8) | `acquisition/ssrf_guard.py` |

## 5. Multi-host safety (B.4) — current state

| State | Where | Authoritative? | Shared across hosts? | After worker/host death |
|---|---|---|---|---|
| Target records, content index | Redis | yes (violates B.4 #1 intent: Redis = coordination) | yes | survives if Redis persists (AOF); no Scylla copy |
| Target reference media | local path, or shared dir (opt-in) | yes | **no** by default | lost with the host |
| Target segment vectors | local JSON, or shared dir (opt-in) | yes | **no** by default | lost with the host; rebuilt (duplicate work) |
| Candidate vectors | process memory | — | — | discarded after every job |
| Build lock | Redis `SET NX PX` | for "who builds" | yes | expires after 600 s; no renewal |
| Job leases | Redis stream PEL + state hash | yes | yes | reclaimed after lease expiry (V1 phases 1–4 tested) |

Answers to the audit questions:

- **Two hosts, same target, default config:** host A wins the lock and writes
  to its own disk; host B polls a disk that never sees it, times out after
  600 s and builds again (13D audit §5, still true by default).
- **Redis lock sufficient?** For work avoidance only. It has no lease renewal
  and no fencing; correctness today rests on deterministic keys plus atomic
  replace, not on the lock.
- **Retries duplicate records?** Target entries: no (same key, replace).
  Candidate work: every retry re-acquires and re-embeds by design.
- **Model upgrade:** a different `spec_key` misses the cache and rebuilds on
  the next job; old files stay forever (no retirement); no backfill.
- **Partial failure:** temp file + `os.replace`; an orphan temp file can stay
  after a crash; no orphan GC.

**Verdict: the fingerprinter is still not multi-host safe in its default
configuration, and the opt-in fix does not meet the V2 plan (MinIO, Scylla,
no Redis authority).**

## 6. Existing tests and benchmarks

- Tests: unit and simulated-multi-host tests for lock, target build-on-miss,
  shared storage, crash safety, worker lifecycle, acquisition/SSRF
  (`tests/test_target_*.py`, `test_shared_target_storage.py`,
  `test_crash_recovery.py`, `test_acquisition_ssrf.py`); `tests/media_test_server.py`
  (local fixture server). No CUDA test, no OOM test, no fp16 test, no
  real-Scylla/MinIO test.
- Benchmarks (`benchmarks/`): `bench_embedding` (CPU-forced), `bench_matching`,
  `bench_integration_overhead`, `bench_pipeline`; fixtures `bench_15s.mp4`,
  `bench_60s.mp4` = ffmpeg `testsrc2`, **320×240, 24 fps, H.264** synthetic.
  `docs/benchmarks.md`: "No GPU benchmark numbers exist anywhere in this
  repository".
- `benchmark/results/sep6_fingerprinter.json` (V1 run, 6.4 h): configured
  `embedding_device=cuda`, so CUDA initialised on this host on 2026-09-06;
  2,399 claims, 31 completed, 2,364 acquisition failures; candidate-embedding
  p50 119 ms on small media. Not a GPU benchmark (batch 1, no VRAM data).

## 7. CPU baseline (measured 2026-10-10)

Run: `cd ~/anti_piracy/fingerprinter && env/bin/python -m benchmarks.bench_embedding`
(existing harness, unchanged; result
`benchmarks/results/bench_embedding_20261010T174709_79bb9b73.json`, left
uncommitted in the fingerprinter repo).

| Item | Value |
|---|---|
| Machine | Intel i5-11400H (6 cores / 12 threads), 15.4 GiB RAM, USB 5400-rpm HDD; Linux 7.0.0-38 |
| Software | Python 3.12.3, torch 2.13.0+cu130, transformers 5.15.1, Pillow 12.3.0, numpy 2.5.2, ffmpeg 6.1.1-3ubuntu5 |
| Model | `facebook/dinov2-base` @ `f9e44c81…`, FP32, CPU (harness forces `device="cpu"`), batch 1 |
| Input | `bench_60s.mp4`: 60 s, 320×240, 24 fps, H.264, synthetic `testsrc2` |
| Sampling | `SegmentSamplingConfig(segment_duration_s=5.0)` → 12 segments |
| Included | ffmpeg decode + PNG frame extraction, preprocessing, inference, aggregation. **Excluded**: download, serialization, storage |
| Warm-up | engine constructed; first call reported separately (2.01 s); 4 steady repetitions |
| **Result (torch default 6 threads)** | **median 1.80 s per minute of video** (min 1.78, max 1.83, n = 4); 6.66 segments/s; peak RSS 1.07 GiB |
| 1 thread (15 s clip, 2.5 s segments) | median 3.23 s per 15 s → 1.88 segments/s |
| Cold start | first engine construction 9.3 s (model load from HDD); first inference 22.6 s incl. cold caches |
| Tail latency | not meaningful at n = 4 (harness warns) |
| Agreement | matches the 2026-08-12 run on the same host (1.72 s/min; 7.06 seg/s) |

**Limits:** this is an inference-dominated, synthetic, low-resolution figure.
It is *not* a baseline for real media: decode cost grows with resolution and
codec (the real 1920×800 HEVC film needed > 300 s just to decode, A-8). The
real-corpus CPU baseline is **not yet measured** (procedure in
[data-preparation.md](data-preparation.md) §5).

## 8. GPU and environment

- `torch.cuda.is_available()` is **True** in the fingerprinter venv today
  (it was False on 2026-08-12).
- `nvidia-smi` fails: **"Failed to initialize NVML: Driver/library version
  mismatch"** — kernel module 610.57.04, user-space NVML 615.71. A reboot
  (or matching driver packages) is needed before any VRAM measurement via
  NVML; CUDA kernels were **not** exercised in this audit.
- No NVIDIA container runtime in Docker (`runc` only): GPU work must run as a
  host process; containers are CPU-only.
- Running stack: `crawler2-{app,redis,scylla,minio}-1` healthy. Disk: 353 GB
  free on `/dev/sda2` (USB HDD).

## 9. Data sufficiency (summary; details in data-preparation.md §1)

| Need | State |
|---|---|
| Repeatable benchmark videos | available but insufficient (2 synthetic 320×240 clips) |
| Real media | 1 file: `~/Videos/Blast.mp4` (8,495 s, HEVC 1920×800, 24 fps, 1.7 GiB); not for git |
| fp16/fp32 fixed clip set | missing; can be generated once real sources exist |
| Labelled match set | **missing** (no labels in either repo) |
| Identical media at multiple URLs | can be generated (P8 fixture-server pattern) |
| Different-content controls | available (P8 corpus) |
| Varied duration/resolution/codec/fps | can be generated (ffmpeg has libx264, libx265, libvpx-vp9, libaom/libsvtav1) |
| 1,000-video soak corpus | missing; constructible from ≥ 5–10 open-licence films |
| Targets | 1 (V1 Redis `fingerprint:target:blast:v1`); insufficient |
| RTX 2050 | present; driver mismatch blocks NVML |
| CPU-only environment | available (`CUDA_VISIBLE_DEVICES=` or any container) |
| Two hosts (GPU + CPU) | simulated only (host process + container); no second machine (plan Part F #2) |

## 10. Risks found

| # | Risk | Where handled |
|---|---|---|
| R-1 | **A fingerprinter consumer group on `events:encode.requested.v1` can stall the crawler2 relay** (ADR-022: a group 64 MiB behind blocks the whole relay cycle) — a B.5 #1 violation if the fingerprinter is down for long | design §6, P9-D6, P9-D27 |
| R-2 | crawler2 never re-requests (write-once `default` claim; any ready spec suppresses) → model upgrades and retries must be fingerprinter-driven | design §11, §6.11 (P9-D8, P9-D15) |
| R-3 | An `encode.failed` with a concrete `spec` would land on a row other than `default`, leaving crawler2's projection "requested" forever | design §6.11 (P9-D5: echo `required_spec`) |
| R-4 | Acquisition hashes decoded bytes and caps at 100 MiB → digest mismatches and `too_large` for real films | design §5 (P9-D10, P9-D24) |
| R-5 | Hidden identity inputs H-1…H-6 → "same spec" representations that differ beyond tolerance across hosts or library upgrades | design §3.2–§3.6 (P9-D4, P9-D26) |
| R-6 | Real-media decode dominates CPU time; the GPU cannot speed up CPU decode → end-to-end GPU ≥ 5× CPU is doubtful for long high-resolution media | design §13 (P9-D17) |
| R-7 | Driver/NVML mismatch; laptop GPU shared with the display (variable free VRAM) | data-preparation §3 |
| R-8 | Labelled set and soak corpus missing → fp16 decision gate and soak gate blocked | data-preparation §2 |
| R-9 | No Scylla/MinIO/contracts dependencies or image in the fingerprinter repo → dependency and packaging changes needed at implementation | design §19 (P9-D12) |
| R-10 | `segment_duration_s = 5.0` is unvalidated; changing it later is a new spec and a full backfill | decision P9-D4 |

## 11. Design-review follow-up (2026-10-10, revision 2)

The first design draft was reviewed before approval. The review found nine
blocking issues in the **design** (not new code findings; §1–§10 are
unchanged and no code has changed). Each is resolved in the design text and
has tests in design §15. Status per issue: [traceability.md](traceability.md) §2.

| Issue | Problem in draft 1 | Corrected in | Decisions |
|---|---|---|---|
| A | Gap detection compared the first retained stream ID with "cursor + 1"; stream IDs are not contiguous; replay/accept-gap underspecified | design §6 (ordinal rule from `entries-added − length`, gap state machine, replay with new IDs, explicit `accept-gap`, init/reinit) | P9-D6, P9-D27 |
| B | A crash after the representation LWT but before index/event writes left undiscoverable work | design §8 (intents before facts, single commit point, follow-up flags, reconciler, crash table, sweeper order) | P9-D13, P9-D20 |
| C | `pending_events` had a 14-day TTL | design §8.1, §9.2 (no TTL; removal only after ack on the fact row) | P9-D21 |
| D | Target events could be published before any consumer group existed and then be trimmed | design §9.1, §10 (stream registry, holds, `register-consumer`, `republish`) | P9-D22 |
| E | Due index had a 30-day TTL | design §7.3 (no TTL, write order, full-scan repair) | P9-D23 |
| F | One source shape for internet URLs and internal MinIO objects | design §5 (typed `external_http` / `internal_object`) | P9-D24, P9-D10 |
| G | "Exit on single-item OOM" contradicted "no OOM crash in the soak" | design §12.4, §13.3 (failure classes, controlled exits, observable soak gate) | P9-D25, P9-D16 |
| H | Runtime recorded only as provenance; colour policy left open | design §3.2–§3.6 (runtime profiles in the digest, fail-closed compatibility, chosen colour policy, real PTS, determinism levels) | P9-D26, P9-D4 |
| I | The 5× gate and the synthetic baseline were not scoped precisely | design §13 (separate metrics; 5× on the inference stage; end-to-end reported) | P9-D17, P9-D3 |

**Unresolved (at revision 2):** the gate scope (P9-D17, Q2) and approval of
P9-D1…P9-D27 and PC-3. **Operator decisions of 2026-10-10** then approved
PC-3, P9-D2, P9-D3 (precision is part of the identity; FP16 a distinct gated
spec), P9-D17, P9-D18 and the colour policy in principle (decisions.md §1);
the other decisions await final design approval. Data preparation (Q3) and
the GPU driver (Q4) block validation, not the design.

**Environmental limitations (unchanged):** NVML driver/library mismatch
(GPU unvalidated); no labelled set, precision set or soak corpus; one
physical machine (two-host = simulated; real multi-machine in P14); USB HDD
storage.

**Readiness:** the design is ready for the operator's implementation
review. It is not approved, and P9 is not implemented.

**Final review (2026-10-10):** a bounded review of the remaining PROPOSED
decisions found five specification gaps, each traced to a failure sequence and
corrected in the design text: (1) the durable attempt lease `lease_until` was
set once and never renewed, so any attempt longer than one lease would be
requeued and encoded again (design §7.2); (2) no rule said when `attempts`
increments, so released attempts could exhaust a request's budget and a
process-killing item could loop forever (§7.2); (3) a redelivered
`encode.requested` could be read as reopening a retryable FAILED request
(§6.10); (4) a target registered after its representation already existed
was never bound, so `target.registered` was never emitted (§10, §8.4);
(5) the runtime fingerprint mixed CUDA components into one profile, so a CPU
encoder could never match it, or would have to ignore components (§3.5).
No architecture, approved decision or frozen contract changed.

**Readiness review (2026-10-10):** four further gaps, each traced and
corrected in the design: (6) the reference upload named the final key
`ref/<sha256>` before the hash was known; it is now hash-locally, stage,
verify, promote by server-side copy, with intent-based crash recovery (design
§10); (7) `encode.failed.request_id` must name an `encode.requested` event,
which target-only requests do not have, and resetting `claims` on reopen
would repeat an `(request_id, attempt)` key that crawler2 would drop as a
duplicate (§7.2, §7.4); (8) the `timeout` failure had no wall-clock parameter
(§7.2); (9) the soak's zero-orphan criterion could not be observed with the
24 h grace (§13.3). No architecture or approved decision changed.
