# P9 Design — media-centric encoder, representation registry, target manager

Status: **APPROVED (operator final design approval, 2026-10-10) — revision 2
with the final-review and readiness-review corrections.** The operator
approved PC-3 and P9-D1…P9-D27 with all recommendations, trade-offs and
qualifications of the final readiness review ([decisions.md](decisions.md)
§0). Approval authorizes implementation; it is not evidence of
implementation or validation. Implementation status per requirement:
[traceability.md](traceability.md); what was built and measured, and every
exit gate: [exit.md](exit.md) and [validation.md](validation.md). Audit:
[audit.md](audit.md). Data: [data-preparation.md](data-preparation.md).
Where the implementation differs from or narrows this text, §22 records it.
**Amended 2026-10-11** by the operator decisions OF-1…OF-3 (decisions §0a):
§3.2a (spec `dinov2-seg/2`, the FP32 default for new deployments), §3.7,
§7.4, §12.3, §12.6, §13.3.

This document is the source of truth for the approved P9 architecture. Every
statement below is **APPROVED design** (originally PROPOSED) unless it is marked:

- **EXISTING** — behaviour implemented today (crawler2 P1–P8 or the V1
  fingerprinter), as found by the audit;
- **FROZEN** — an accepted contract or ADR that P9 must not change (P1 events
  and IDs, P8 behaviour, ADR-011/013/014/022/023/024);
- **ENV** — an environmental limitation of the dev host.

Code lands in the **fingerprinter repository** (new package, §19). crawler2
gets cross-service tests only (§16). No P1 contract, P8 behaviour or accepted
ADR is changed. No ADR is accepted by this document; ADRs required after
approval are listed in decisions.md §5.

## 1. Scope

**In P9:** intake of `encode.requested`; representation registry (Scylla,
authoritative) with artifacts in MinIO; encoders (GPU and CPU); spec and
runtime-profile registry; durable request, commit and event-publication
state with reconcilers; target manager on the same representation
infrastructure; backfill and retirement; publication of
`representation.ready`, `encode.failed`, `target.registered`,
`target.retired`; operator CLIs; metrics; cross-service tests; benchmarks.

**Not in P9:** matching, the vector index, thresholds, level-4/5 equivalence,
`match.found` (P10); consumers of target events (P7 follow-up/P10);
crawler2-side re-requests (P8-D7, FROZEN); NVDEC; real multi-machine
validation (P14, plan Part F #2).

**V1 code (EXISTING):** the V1 job model, `worker/matching_handler.py`, the
Redis target registry and the filesystem caches are not extended; they stay
unwired from V2 until P10. Reused as code: `acquisition/ssrf_guard.py`; the
model-loading pieces of `embedding/` refactored behind the new pipeline;
`matching/matcher.py` only as a frozen offline test oracle (§13.3).

## 2. Architecture overview

```
crawler2 (FROZEN, unchanged)                 fingerprinter (PROPOSED)
media registry ─ outbox ─ relay ─XADD──► events:encode.requested.v1   (no consumer group)
                                              │ XRANGE from durable cursor
                                              ▼
                      intake reader (single owner: Scylla lease; ordinal gap check §6)
                                              │ LWT upsert (idempotent)
                                              ▼
 Scylla `fingerprinter` ── encode_requests (authoritative) ── encode_due (index, no TTL)
                                              │ scheduler → Redis fp:q:encode (cache)
                                              ▼
 encoder process per host (GPU lease or CPU): acquire (typed source §5) → decode → preprocess
   → bounded queue → batched inference → artifact → MinIO (attempt-unique key)
   → intent (Scylla) → representations IF NOT EXISTS  ← AUTHORITATIVE COMMIT POINT (§8)
                                              │ follow-ups (idempotent, reconciled from intents)
                                              ▼
 representations_by_spec, request READY, target binding, event ack
                                              │ publisher (stream registry, holds §9)
                                              ▼
 events:representation.ready.v1 / encode.failed.v1  (group media-registry, FROZEN P8)
 events:target.registered.v1 / target.retired.v1    (HELD until consumers registered)
```

Every fingerprinter role runs identically on one or N hosts; only
configuration differs (`host_id`, roles `intake`, `scheduler`, `encoder`,
`publisher`, `reconciler`, `target-manager`, `device`). Durable state is in
Scylla and MinIO; Redis holds only leases and a rebuildable queue; local disk
holds only per-attempt scratch (§4.4).

## 3. Representation identity

### 3.1 Identity rule (FROZEN derivation; interpretation PC-3, APPROVED)

`RepresentationId = RepresentationId.for_content(content_id, spec.name,
spec.version, spec.config_digest)` (P1 `ids.py:236`, FROZEN).

- `content_id` = P8 `ContentId` = `full-bytes/v1` SHA-256 of the full
  **served identity bytes** (no content decoding), FROZEN (P8-D5).
- `spec.name` = representation family (`dinov2-seg`); `spec.version` =
  pipeline version (`1`); `spec.config_digest` = `sha256:` of the full
  canonical spec document (§3.2), never truncated.
- **`media_version` is not an identity component** (PC-3). P8's media
  version means "the bytes at a locator changed", which already yields a new
  `ContentId`. Same bytes ⇒ same `ContentId` ⇒ same representation under a
  spec, whatever URL, page, version or time they were seen at. Different
  bytes ⇒ different `ContentId` ⇒ different representation. Media version,
  `MediaId`, locator and validators are **provenance** on the request and the
  artifact manifest only.
- Source type (external URL or internal object, §5) is provenance only.
- **Precision is part of the identity:** `inference.compute_dtype` is in the
  canonical spec document, so an FP32 spec and an FP16 spec always have
  different `config_digest`s and therefore different `RepresentationId`s
  for the same content (P9-D3, APPROVED).

### 3.2 Canonical spec document `dinov2-seg/1` (the base of `dinov2-seg/2`, §3.2a)

Canonical JSON: UTF-8, keys sorted, no insignificant whitespace, numbers in
shortest round-trip form, arrays in given order. `config_digest` = SHA-256
of those bytes. Every input that can materially change the output is in it:

```json
{
  "schema": "fingerprinter/spec/v1",
  "family": "dinov2-seg", "pipeline_version": 1,
  "runtime_profile": "rt-1",
  "model": {"id": "facebook/dinov2-base", "revision": "f9e44c814b77203eaa57a6bdbbd535f21ede1415",
            "loader": "transformers.AutoModel", "attn_implementation": "sdpa",
            "output": "cls", "dim": 768},
  "decode": {"tool": "ffmpeg", "stream": "first-video", "seek": "none", "autorotate": true,
             "genpts": true, "fps_mode": "passthrough", "copyts": true,
             "pix_fmt": "rgb24", "sws_flags": "bicubic+accurate_rnd+full_chroma_int",
             "colour_policy": "tags-else-swscale-default/rt-1", "tone_mapping": "none", "scale": "none"},
  "sampling": {"segment_duration_s": 5.0, "origin": "first_decoded_pts",
               "frame_rule": "first_frame_with_index_gt_previous", "empty_segment": "absent"},
  "preprocess": {"impl": "fingerprinter/preprocess/v1", "library": "Pillow",
                 "resize_shortest_edge": 256, "other_edge": "round_half_up",
                 "resample": "BICUBIC", "crop": 224, "crop_offset": "floor",
                 "rescale": "1/255", "mean": [0.485,0.456,0.406], "std": [0.229,0.224,0.225],
                 "layout": "NCHW", "dtype": "float32"},
  "inference": {"compute_dtype": "float32"},
  "postprocess": {"normalize": "l2", "accumulate_dtype": "float32",
                  "coarse": "mean_then_l2"},
  "artifact": {"format": "fp-seg-npz/v1", "vector_dtype": "float32"}
}
```

The **default spec is FP32** (`inference.compute_dtype = float32`) on both
CPU and CUDA until the precision gate passes (§3.7, P9-D3). Since OF-1
(2026-10-11) the FP32 default for new deployments is `dinov2-seg/2`, which is
this document plus `decode.truncation` (§3.2a); `dinov2-seg/1` remains
available with its original identity and behaviour.

### 3.2a Spec `dinov2-seg/2` — truncated input is a decode error (operator decision OF-1, 2026-10-11)

`dinov2-seg/1` accepts a decode that ffmpeg finishes with exit 0, even when the
demuxer reported that the input ended inside a container element or sample:
it represents the decodable prefix. The operator decided (decisions §0a) that
**explicitly detected truncation is `decode_error`**. That changes which
inputs yield a representation, i.e. the validity semantics of the output, so
it is a **new spec version**, not a change to `dinov2-seg/1`:

```json
"pipeline_version": 2,
"decode": { …all dinov2-seg/1 fields unchanged…, "truncation": "explicit-indicators/v1" }
```

Everything else (model, decode flags, sampling, preprocessing, inference,
postprocessing, artifact) is identical; for complete media both specs produce
the same frames and vectors (tested), but under different
`RepresentationId`s (`version` and `config_digest` are identity inputs, §3.1).
`decode.truncation` is optional in the schema and **omitted from the canonical
form when unset**, so `dinov2-seg/1` keeps its digest
(`sha256:ddfc71c1…`, FP16 `sha256:4dadb2a0…`; pinned by a test) and its
behaviour. Built-ins: `dinov2-seg/2` (FP32, `sha256:a4df3d19…`) and
`dinov2-seg/2-fp16` (gated, `sha256:a765d7d2…`). `dinov2-seg/2` is the spec a
new deployment adds and makes default; `dinov2-seg/1` stays available.

Detection (`explicit-indicators/v1`, §7.4) and its limits:

| Indicator | Emitted by (pinned `rt-1` ffmpeg 6.1.1) | Matched line |
|---|---|---|
| `matroska_file_ended_prematurely` | matroska/webm demuxer: EOF inside an element | `[matroska,webm @ 0x…] File ended prematurely` |
| `mov_partial_file` | mov/mp4 demuxer: a sample's bytes lie beyond the end of the input | `[mov,mp4,m4a,3gp,3g2,mj2 @ 0x…] stream N, offset 0x…: partial file` |

Lines are matched whole and anchored at the demuxer's log prefix, so metadata
text (e.g. a title "File ended prematurely") never matches. Not truncation
evidence, and therefore not failing a decode: corrupt-packet reports
(`Packet corrupt`, `corrupt input packet`), decoder concealment, deprecated
pixel-format and other warnings. **Not detectable** by this policy (the decode
is accepted, as under `dinov2-seg/1`): a cut exactly on an element/cluster
boundary or within the last bytes of a Matroska file, MPEG-TS and FLV
truncation (no explicit report), an MP4 whose cut falls inside the last
sample only (reported as a corrupt packet, not a partial file), and any
indicator text that a future ffmpeg changes (a runtime-profile change, §3.5,
re-runs these tests). A truncated MP4 with the `moov` atom at the end already
fails at probe (`unsupported_format`, unchanged).

**Migration and coexistence.** No representation is rewritten or relabelled.
Existing `dinov2-seg/1` rows stay valid under their own identity; crawler2's
suppression accepts either (FROZEN "any ready", §11.3). Moving to
`dinov2-seg/2` follows §11.1: `spec add --builtin dinov2-seg/2` (FP32 →
`active`) → `spec set-default` → optionally a **bounded, operator-started**
backfill `--from-spec <v1> --to-spec <v2>` (§11.2, rate-limited, pausable;
nothing starts automatically) → `deprecate`/`retire` v1 only after the
retirement guards allow it (§11.4). A backfill re-acquires sources (D11): a
v1 representation of a truncated prefix becomes a `decode_error` under v2
(reported in the backfill report; the v1 row is kept until v1 is retired).
P9 has no production deployment, so no data migration is needed now.

### 3.3 Decode, sampling, timestamps, colour (APPROVED policy, P9-D4)

- **Decode:** one sequential full decode of the first video stream from the
  start (no seeking), `-fflags +genpts -copyts -fps_mode passthrough`,
  autorotate on; output `rawvideo rgb24` through a pipe (no PNG files).
- **Timeline origin:** `t0` = PTS of the first decoded frame (ffmpeg
  `select` variable `start_t`). All segment times are relative to `t0`.
- **Frame selection:** segment `k` covers `[k·d, (k+1)·d)` with `d = 5.0 s`.
  The representative frame of segment `k` is the **first decoded frame in
  presentation order whose `floor((t − t0)/d)` is greater than that of the
  previously selected frame**:
  `select='isnan(prev_selected_t)+gte(floor((t-start_t)/5),floor((prev_selected_t-start_t)/5)+1)'`,
  followed by `showinfo` to emit each selected frame's integer `pts`,
  `time_base` and `pts_time`.
- **Timestamps stored:** for each segment: `segment_index k`, the selected
  frame's exact `pts` (int64) and `time_base` (rational), `pts_s` (float64),
  `window_start_s = k·d`, `window_end_s = (k+1)·d`. **Never** `index × 5`
  alone: `k` is derived from the frame's real PTS.
- **Irregular timelines:** VFR and timeline holes produce absent segments
  (no frame selected for that `k`); absent `k` are not filled or
  interpolated, so segment indices may be non-contiguous. Selected PTS must
  be strictly increasing; otherwise the attempt fails `decode_error`
  (`detail=non_monotonic_pts`). A stream with no usable timestamps after
  `genpts` fails `decode_error`. A stream whose duration is 0 or with no
  decodable frame fails `decode_error`.
- **Precision:** segment classification uses ffmpeg's double arithmetic
  inside the pinned runtime profile; it is bitwise reproducible under `rt-1`
  and verified by golden fixtures with frames placed exactly on and ±1 tick
  around window boundaries.
- **Colour (resolves review issue H's open choice):** YUV→RGB by swscale with
  explicit `sws_flags=bicubic+accurate_rnd+full_chroma_int`, input matrix and
  range taken from stream tags; **untagged streams use swscale's default
  interpretation as built in the pinned `rt-1` ffmpeg**, and that behaviour is
  pinned by golden fixtures (untagged SD, untagged HD, tagged BT.601 limited,
  tagged BT.709 limited, full-range/JPEG-range, 10-bit). Transfer
  characteristics are **not** tone-mapped (HDR PQ/HLG is converted as-is);
  this is recorded and tested, not hidden. BT.709 is not forced.

### 3.4 Preprocessing `fingerprinter/preprocess/v1`

Explicit code, not `AutoImageProcessor` (whose backend is
`TorchvisionBackend` under transformers 5.15.1, audit H-1): rgb24 uint8 HWC →
Pillow `Image` → resize so the shortest edge is 256, other edge
`round_half_up(long × 256 / short)`, `Image.BICUBIC` → centre crop 224 with
offsets `floor((h − 224)/2)`, `floor((w − 224)/2)` → float32 ÷ 255 →
normalize mean/std → NCHW. Runs on CPU decode threads. Tested against HF
`BitImageProcessor` outputs on fixtures for **tolerance** (documented
difference), and against committed golden tensors for **equality** (§3.6).

### 3.5 Runtime profiles and compatibility (fail closed, P9-D26)

A **runtime profile** is a named, immutable document stored in
`runtime_profiles`: exact versions of every component that can change output
— ffmpeg (version string and libavcodec/libavformat/libswscale versions and
the build configuration hash from `ffmpeg -buildconf`), Python, torch (and
CUDA runtime/cuDNN versions for CUDA execution), transformers, numpy,
Pillow, and the model files' SHA-256. `rt-1` is captured from the lock file
and the host at the start of implementation (current host values: ffmpeg
6.1.1-3ubuntu5, Python 3.12.3, torch 2.13.0+cu130, transformers 5.15.1,
numpy 2.5.2, Pillow 12.3.0 — ENV, audit §7; torchvision is not used by
`preprocess/v1`).

- A profile has **common components** (ffmpeg build, Python, torch, numpy,
  Pillow, transformers, model file hashes) and **device variants**: `cpu`
  (no further components) and `cuda` (CUDA runtime and cuDNN versions as
  reported by torch). An encoder's fingerprint matches a profile only if its
  common components are all equal **and** its device's variant is listed in
  the profile with equal components. The NVIDIA driver version is provenance
  only (not in the profile); a driver change is caught by the startup golden
  check (§3.6, CUDA fp32 vs CPU golden ≥ 0.999), which refuses the device on
  failure. `rt-1` lists both `cpu` and `cuda` variants; the `cuda` variant is
  admitted only after the CUDA fp32 golden check passes on the RTX 2050
  (blocked until the NVML mismatch is fixed). Adding a variant later is a
  profile change and follows the upgrade path below.
- The spec document names its **origin profile** (`runtime_profile`), so the
  profile is part of `config_digest`.
- `spec_runtime_compat (spec_digest, profile)` lists the profiles allowed to
  **produce** representations of a spec. Initially only the origin profile.
- **Fail closed:** at start the encoder computes its actual runtime
  fingerprint; if it does not equal an allowed profile for a spec, the
  encoder refuses to claim work of that spec (`runtime_incompatible`, alarm,
  metric). It never produces "best effort" output under another profile.
- **Serving** (reading) a committed representation never requires a matching
  runtime; artifacts are plain arrays.
- **Upgrade path:** a new library set becomes profile `rt-2`. It may be added
  to `spec_runtime_compat` of an existing spec only by an explicit operator
  action after the **runtime compatibility gate** passes on the frozen golden
  set and a C-PREC subset: identical segment count, indices and PTS; decoded
  RGB of selected frames bitwise equal; per-segment cosine ≥ 0.9999 vs the
  origin profile's artifacts (CPU fp32). The approval is recorded (who, when,
  evidence file). If the gate fails, the upgrade needs a new spec version and
  a backfill (§11). Representations are never silently reused across an
  unapproved runtime change.
- Each artifact also records its actual runtime fingerprint (provenance).

### 3.6 Determinism levels

| Stage | Guarantee under an allowed profile | Test |
|---|---|---|
| Decode, selected-frame RGB, segment indices, PTS | **bitwise** identical | golden RGB SHA-256 and PTS lists |
| Preprocessed tensors (CPU, Pillow) | **bitwise** identical (target); if a platform shows ULP-level differences, the criterion is max abs diff ≤ 1e-6 and the deviation is recorded | golden tensor hashes |
| Embeddings, CPU fp32 | numerically equivalent: cosine ≥ 0.9999 per segment vs golden | golden vectors |
| Embeddings, CUDA fp32 | numerically equivalent: cosine ≥ 0.999 per segment vs CPU golden | golden vectors |
| Embeddings, CUDA fp16 | a **different spec and identity**; never compared for identity with FP32 artifacts, only evaluated against them by the precision gate | §3.7, §13.3 |

GPU kernels are not bitwise deterministic; two hosts may commit numerically
different (equivalent) vectors for the same `RepresentationId`. The first
committed artifact is the representation (§8).

### 3.7 Precision policy (P9-D3, APPROVED 2026-10-10)

1. Precision is part of representation identity (`inference.compute_dtype`
   in the spec digest).
2. FP32 is the default spec on CPU and CUDA: `dinov2-seg/2`
   (`compute_dtype = float32`) for new deployments since OF-1 (§3.2a);
   `dinov2-seg/1` FP32 remains available with its original identity and
   behaviour.
3. FP16 is a **distinct spec** per version (`dinov2-seg/2-fp16` or
   `dinov2-seg/1-fp16`: the same document with
   `inference.compute_dtype = float16` ⇒ its own `config_digest` and
   `RepresentationId`s), always added as `gated`. FP16 and FP32 representations of the same content
   coexist as separate rows and objects.
4. The FP16 spec may be added with `spec add`, but it may be **enabled as an
   operational option** (encoders claim its work, backfill to it) **or made
   the default** (`spec set-default`) only after the precision gate (§13.3)
   and the labelled-decision gate have passed for it. `spec add` records the
   spec as `status = gated` until then; the encoder refuses to claim work of
   a `gated` spec.
5. Passing the gate **does not merge** the identities: FP32 representations
   are never relabelled as FP16 (or vice versa), and switching the default
   to FP16 creates new FP16 representations through backfill (§11); FP32
   ones stay until retired.

No FP16 evidence exists; 0.999 is the approved threshold, not a measured
result. The FP16 spec is GPU-only: CPU encoders run FP32 and never claim
FP16-spec work (they would fail the spec's runtime/device requirement).

### 3.8 Artifact format `fp-seg-npz/v1`

Uncompressed NumPy `.npz`: `segment_index` int32 `[N]`, `pts` int64 `[N]`,
`time_base` int64 `[2]`, `pts_s` float64 `[N]`, `window_start_s`,
`window_end_s` float64 `[N]`, `vectors` float32 `[N, 768]` (L2-normalized),
`coarse` float32 `[768]`, and `manifest.json` (representation_id, content_id,
spec digest, runtime fingerprint, device, compute dtype, batch sizes used,
worker identity, attempt_id, stream info, duration). 2 h at 5 s ≈ 4.4 MiB.
Object SHA-256 and size are stored on the authoritative row and verified on
every read.

## 4. Storage ownership

### 4.1 Scylla keyspace `fingerprinter` (tablets disabled for LWT; RF by config)

Own migrations and migrator in the fingerprinter repo. No TTL on any table
that holds the only copy of active work, unpublished events or authoritative
facts.

| Table | Key | Role | TTL |
|---|---|---|---|
| `specs` | `spec_digest` | spec document, name, version, status `gated`/`active`/`deprecated`/`retired`, gate evidence reference | none |
| `spec_roles` | `role` | `default` → spec digest; `epoch` | none |
| `runtime_profiles` | `profile` | profile document | none |
| `spec_runtime_compat` | PK `spec_digest`, CK `profile` | runtime profiles allowed to produce the spec; approval record (precision is not here: it is in the spec digest) | none |
| `encode_requests` | PK `content_id`, CK `spec_key` | **authoritative request state** (§7) | none |
| `encode_due` | PK `(due_day, shard)`, CK `due_at, content_id, spec_key` | discoverability index of non-terminal requests (§7.3) | **none** |
| `encode_due_days` | PK `due_day` | registry of due partitions that may hold entries | none |
| `representations` | PK `content_id`, CK `spec_digest` | **authoritative representation** (§8); `artifact_attempt_id`, artifact URI/sha256/size, stored ready envelope, `ready_event_published_at`, follow-up flags | none |
| `representations_by_spec` | PK `(spec_digest, bucket)`, CK `content_id` | secondary index (backfill, retirement, P10 rebuild) | none |
| `intents` | PK `(day, shard)`, CK `intent_id` | durable work intents written **before** facts: representation commits, failures, target events (§8.1) | **none** |
| `intent_days` | PK `day` | registry of intent partitions with open intents | none |
| `targets`, `targets_by_content` | PK `target_id` CK `version`; PK `content_id` CK `target_id, version` | target facts; reverse lookup for binding | none |
| `target_representations` | PK `target_id`, CK `version, spec_digest` | target ↔ representation binding | none |
| `stream_registry` | PK `stream` | required consumer groups, hold flag, registration audit (§9) | none |
| `intake_state` | PK `reader` | lease, epoch, cursor, ordinal, mode (§6) | none |
| `intake_gaps` | PK `reader`, CK `gap_id` | gap records and their audit trail (§6.5) | none |
| `backfill_jobs`, `backfill_progress` | PK `job_id`; PK `job_id` CK `bucket` | backfill state (§11) | none |
| `encoder_hosts` | PK `host_id`, CK `device, spec_digest, compute_dtype` | calibrated and stable batch sizes, OOM counters | none |
| `encode_attempt_log` | PK `(content_id, spec_key)`, CK `attempt_id` | observability history of attempts | 90 d (**safe**: not used for scheduling or recovery) |

### 4.2 MinIO (fingerprinter-owned buckets)

| Bucket | Key | Content | Deletion |
|---|---|---|---|
| `fingerprinter-representations` | `rep/<spec_hex>/<content_id>/<attempt_id>.npz` | artifacts, attempt-unique keys | only by the orphan sweeper (§8.5) or retirement (§11.4); **no bucket lifecycle rule** |
| `fingerprinter-targets` | `ref/<sha256_hex>`; `staging/<upload_id>` | target reference media, content-addressed after verified promotion (§10); staging objects during upload | `ref/`: only by purge or an unreferenced-orphan rule (§10); `staging/`: after promotion or by the sweeper |

Candidate media bytes are **not** stored (P9-D11). Note (EXISTING): P8 does
not store media in MinIO either; it hashes and discards bytes.

### 4.3 Redis (`fp:` namespace; coordination only, all rebuildable)

`fp:q:encode` (ZSET, rebuilt from `encode_due`), `fp:lease:req:<content>:<spec_key>`
(work lease, avoidance only), `fp:gpu:<host_id>` (GPU lease, §12.1),
`fp:lease:target:<target_id>:<version>`. Redis time (`TIME`) is the clock for
every lease, including those stored in Scylla.

### 4.4 Local disk

`scratch_dir/<attempt_id>/` holds the acquired bytes and nothing else (frames
are piped). Deleted when the attempt ends; at encoder start every
`scratch_dir` entry whose attempt is not owned by this process is deleted.
Never authoritative. Disk budget: `scratch_max_bytes`; the encoder does not
claim work whose declared size exceeds the free budget.

## 5. Typed sources (P9-D24)

`encode_requests.sources` is a list of tagged values; the tag decides the
acquisition path. Sources are provenance; none is part of the identity.

| | `external_http` | `internal_object` |
|---|---|---|
| Origin | intake from `encode.requested` (`source: Media`, `referer`) — the **only** type intake creates | target manager only; **never** accepted from an event |
| Required fields | `media` (P1 `Media` JSON: media_id, locator, kind), `first_event_id`, `added_at` | `bucket`, `key`, `size`, `sha256`, `added_by` (target_id/version) |
| Optional | `referer` (P1 `UrlRef`) | — |
| Validation | absolute `http`/`https` locator; P1 `Media` decodes | `bucket` ∈ allowlist {`fingerprinter-targets`}; `key` matches `^ref/[0-9a-f]{64}$` and equals `ref/<sha256>`; size ≤ 8 GiB |
| Authorization | none (public web); SSRF guard on the first request and **every redirect hop** (ported V1 guard; private/loopback/link-local/reserved refused) | the fingerprinter's own MinIO credentials; only the allowlisted bucket/prefix; no URL is ever fetched |
| Acquisition | streamed GET, `Accept-Encoding: identity`, raw bytes (no content decoding); a non-identity `Content-Encoding` is refused; redirects by hand (≤ 5); `Referer` per `strict-origin-when-cross-origin` (full page URL same-origin, origin only cross-origin, none on https→http); timeouts `acquire.timeout_s` 3600 / `read_timeout_s` 60 | MinIO SDK `GetObject` stream |
| Size | declared length checked first, then counted; **max 8 GiB** (= P8 `hash_max_bytes`); larger → `too_large` | same 8 GiB bound |
| ContentId check | streaming SHA-256 must equal the request's `content.digest`; mismatch → this source is unusable (`content_mismatch`) | same check (detects corruption) |
| Missing / inaccessible | 404/410 → non-retryable for this source; 401/403/451/SSRF → `source_forbidden`; network/5xx → retryable | `NoSuchKey` → non-retryable `internal_error` (`detail=reference_missing`), target build fails; storage unavailable → retryable |
| Retry | transient classes, backoff, other sources first | transient storage errors only |

The 100 MiB V1 limit and V1's content-decoded hashing (EXISTING, audit Q-1/Q-2)
are **not** used by P9.

## 6. Intake of `encode.requested` and gap recovery (P9-D6)

### 6.1 Why no consumer group (FROZEN constraints)

A consumer group on `events:encode.requested.v1` would let a stopped
fingerprinter hold back crawler2's whole relay cycle at the 64 MiB stream
budget (ADR-022, `relay.py:140-175`), violating B.5 #1. The fingerprinter may
not read crawler2's outbox (B.5 #2). So the intake reads the stream
**without a group**; ungrouped streams are trimmed by crawler2's publisher to
≈ `stream_maxlen` (100,000) entries / 64 MiB, oldest first, via
`XADD … MAXLEN ~` and `XTRIM` — never `XDEL` (EXISTING, `publisher.py`).
The intake must therefore detect trimming of unread entries exactly.

### 6.2 Ownership lease

`intake_state(reader)` columns: `owner`, `epoch`, `lease_until`,
`cursor_id` (Redis stream ID string or null), `consumed_ordinal` (bigint),
`mode` (`RUNNING`/`HALTED`), `halt_reason`, `initialized_at`, `init_mode`.

- **Acquire:** read the row (SERIAL); if `lease_until < now_redis` (or no
  owner): `UPDATE … SET owner = me, epoch = e + 1, lease_until = now + ttl
  IF epoch = e`. TTL 60 s (proposed).
- **Renew:** every 20 s, `UPDATE … SET lease_until = now + ttl IF owner = me
  AND epoch = e`. A failed renewal stops the reader immediately (it may have
  been superseded).
- Every cursor write is conditioned on `epoch = e AND cursor_id = <old>`
  (compare-and-set), so a stale owner can never move the cursor. A stale
  owner may still upsert requests it already read; upserts are idempotent
  (§6.10), so this is harmless.

### 6.3 Stream IDs and ordinals

Redis stream IDs are `<ms>-<seq>` and **not contiguous**. They are compared
only as the integer pair `(ms, seq)` (lexicographic on the pair), never as
strings and never by "+1" arithmetic on the full ID. "Next after `c`" is
expressed only as the exclusive range start `(c` in `XRANGE`.

Gap detection uses **counts**, not IDs. Redis ≥ 7.0 (deployed 7.4.2, ENV)
reports in `XINFO STREAM`: `entries-added` `A` (total XADDs ever, monotonic,
unaffected by trimming), `length` `L`, `first-entry`, `last-generated-id`,
`max-deleted-entry-id`. With trimming only from the head and no `XDEL`, the
number of entries ever removed is `T = A − L`, and they are exactly the
`T` oldest entries. The reader keeps `consumed_ordinal` `o` = the number of
stream entries (ever added) up to and including its cursor.

### 6.4 Read cycle and gap rule

Each cycle runs one `MULTI … EXEC` (atomic snapshot): `XINFO STREAM s` and
`XRANGE s (cursor + COUNT n` (or `XRANGE s - + COUNT n` when
`cursor_id` is null and `o = 0`).

1. **Stream missing** (`XINFO` error "no such key"): if `o = 0` → nothing to
   do. If `o > 0` → the stream was deleted or Redis lost data →
   `HALTED(stream_reset)` + gap record (§6.5) with `T` unknown.
2. **Stream reset:** `A < o`, or `last-generated-id < cursor_id` →
   `HALTED(stream_reset)` + gap record.
3. **Deletion after the cursor:** `max-deleted-entry-id > cursor_id` (an
   `XDEL` tombstone, or a trim, after the cursor) → gap record if rule 4 has
   not already opened one for this cycle (conservative: with `XDEL`, `T`
   over-counts, so detection may raise a false alarm but never misses a
   loss). Trims of already-read entries cannot trigger it, because they only
   remove IDs ≤ `cursor_id`.
4. **Trim gap:** if `T > o`, then `T − o` entries after the cursor were
   trimmed before being read → **gap record** with `missing_count = T − o`,
   `after_cursor = cursor_id`, `first_retained = first-entry`,
   `window_hint = [time(cursor_id) − replay_margin, time(first-entry)]`.
   Then the reader sets `o := T` (positions now accounted for by the gap
   record) and **continues** reading from the first retained entry. It does
   not halt: halting an ungrouped reader would let crawler2 trim more entries
   and enlarge the loss. The gap stays OPEN and alarmed (§6.5).
   If `T ≤ o`, every trimmed entry was already consumed: **no gap**, even if
   `first-entry` is many IDs after `cursor_id` (normal non-contiguous IDs).
5. **Normal:** apply the returned entries in order (§6.10). After all are
   durable, CAS-advance `cursor_id` to the last applied ID and `o += count`.
6. **Empty range:** nothing returned and `T ≤ o` → idle, poll again
   (`poll_interval_s` 1 s).

Invalid entries (undecodable, wrong type, ownership violation) are counted,
logged with their stream ID, recorded in `intake_invalid` (audit), and the
cursor moves past them — they are never retried forever and never silently
dropped.

### 6.5 Gap records and state machine

`intake_gaps(reader, gap_id)`: `detected_at`, `kind` (`trim`,
`tombstone`, `stream_reset`, `bootstrap`), `after_cursor`, `first_retained`,
`missing_count` (null if unknown), `window_hint`, `state`, and an append-only
`audit` list (time, actor, action, parameters, result).

| From | Event | To |
|---|---|---|
| OPEN, REPLAY_INCOMPLETE | operator `gap replay-start` | REPLAY_REQUESTED |
| REPLAY_REQUESTED | operator `gap replay-done` | REPLAY_OBSERVING |
| REPLAY_OBSERVING | reader cursor ≥ `replay_end_id`, no new gap overlapping `(replay_start_id, replay_end_id]` | CLOSED_REPLAYED |
| REPLAY_OBSERVING | new gap detected inside that range | REPLAY_INCOMPLETE |
| OPEN, REPLAY_INCOMPLETE | operator `gap mark-unreplayable` (window outside outbox retention) | REPLAY_UNAVAILABLE |
| OPEN, REPLAY_INCOMPLETE, REPLAY_UNAVAILABLE | operator `accept-gap` | CLOSED_ACCEPTED |

No transition is automatic except REPLAY_OBSERVING → CLOSED_REPLAYED /
REPLAY_INCOMPLETE, which follow from observed stream state.

- Gap records never close by themselves. While any gap is not CLOSED_*,
  `fingerprinter-intake status` exits non-zero, `fingerprinter_intake_open_gaps`
  > 0 and an alarm fires.
- The reader's own mode stays `RUNNING` during OPEN gaps (continuing is
  required, §6.4.4). `HALTED` is only for `stream_reset` (ordinals invalid)
  and for an uninitialized reader (§6.8); it ends only by an operator
  `intake reinit` (§6.8) recorded in the gap audit.

### 6.6 Lossless repair by replay (preferred)

crawler2's relay can re-publish outbox rows (EXISTING, FROZEN:
`crawler2-storage relay --replay-from ISO --replay-to ISO`; republishes every
outbox row enqueued in the window, all event types, **with new stream IDs
appended at the tail**; outbox retention 14 days, ADR-013).

1. `fingerprinter-intake gap show <gap_id>` prints the window hint and whether
   it is inside crawler2's retention (`now − crawler2_outbox_retention_s`,
   configured to mirror ADR-013; the fingerprinter cannot read crawler2 data
   to check it). The default `replay_margin` is 24 h before the cursor entry's
   time, because outbox rows may be published long after they were enqueued
   (backpressure); the operator may widen the window (replay is idempotent).
2. `fingerprinter-intake gap replay-start <gap_id> --from T1 --to T2`
   records the window and `replay_start_id = last-generated-id` → REPLAY_REQUESTED.
3. Operator runs the crawler2 command with the same window.
4. `fingerprinter-intake gap replay-done <gap_id>` records
   `replay_end_id = last-generated-id` → REPLAY_OBSERVING.
5. The reader consumes the replayed entries like any others (they are after
   the cursor; duplicates of already-seen `event_id`s are no-ops, §6.10).
   When `cursor_id ≥ replay_end_id` with **no new gap detected whose missing
   positions overlap `(replay_start_id, replay_end_id]`**, the gap becomes
   CLOSED_REPLAYED. If such a gap is detected (replayed entries were trimmed
   before being read), the original gap becomes REPLAY_INCOMPLETE and a new
   replay is required — operator-driven, so there is no automatic replay loop.

**Limit (stated, not hidden):** the fingerprinter cannot enumerate the event
IDs it missed; the repair is lossless if the replay window covers the
enqueue times of the missing outbox rows. The recorded window is the
evidence. Replay re-publishes all crawler2 event types in the window; their
consumers are idempotent by contract (FROZEN), but the replay costs load.

### 6.7 `accept-gap` (explicit, never automatic)

`fingerprinter-intake accept-gap <gap_id> --reason "…" --operator <name>`:

- Allowed from OPEN, REPLAY_INCOMPLETE or REPLAY_UNAVAILABLE.
- Acknowledges that up to `missing_count` physical stream entries (unknown
  `event_id`s, possibly several copies of one event) after `after_cursor`
  were never read and may contain `encode.requested` events that are lost.
- Consequence (stated): affected contents stay "requested" in crawler2's
  projection and are not encoded, because crawler2 never re-requests (P8-D7,
  FROZEN), until a future crawler2 re-request design or an operator-supplied
  re-request exists.
- Cursor changes: **none** (the reader already continued, §6.4.4).
- Persists an audit entry (operator, reason, time, counts, window, replay
  attempts) and sets CLOSED_ACCEPTED.

### 6.8 Initialization and re-initialization

The reader refuses to start without an initialized `intake_state` row
(`HALTED(uninitialized)`).

- `fingerprinter-intake init --from-start`: `cursor_id = null`, `o = 0`. If
  the stream already trimmed entries (`T > 0`), the first cycle opens a
  `bootstrap` gap for them (replay or accept).
- `fingerprinter-intake init --from-now --reason …`: `cursor_id =
  last-generated-id`, `o = A`; records a `bootstrap` gap CLOSED_ACCEPTED with
  `missing_count = A` (history explicitly not consumed).
- `fingerprinter-intake reinit --after-reset --reason …` (from
  `HALTED(stream_reset)`): sets `cursor_id = null`, `o = 0` against the new
  stream and keeps the reset gap OPEN for replay/accept.

### 6.9 Crash and restart cases

| Crash point | Recovery |
|---|---|
| after `XRANGE`, before any upsert | cursor unchanged → entries re-read |
| after some upserts, before cursor CAS | entries re-read; upserts are idempotent (§6.10) |
| after cursor CAS | nothing to redo |
| lease lost mid-cycle | CAS fails → stop; new owner re-reads from the durable cursor |
| Redis restart (AOF) | stream persists; if data was lost, rule §6.4.1/2 halts |

### 6.10 Request upsert, duplicates, delays

Key `(content_id, spec_key)`; `spec_key = 'default'` when `required_spec` is
absent (crawler2 always sends `default` today, FROZEN), else the digest.
Every upsert first records the incoming `event_id` in the row's
`request_event_ids` set (idempotent).

| Existing row | Incoming | Action |
|---|---|---|
| none | any | `INSERT … IF NOT EXISTS`: PENDING, `resolved_spec` = current default (for `default`), `sources = [external_http]`, priority; due entry (§7.3). If a READY representation of `resolved_spec` exists → request READY + intent for a re-published `representation.ready` (same `representation_id`, new `event_id`), no encode (S1/S2 reuse) |
| PENDING / IN_PROGRESS | same `event_id` | no-op |
| PENDING / IN_PROGRESS | new `event_id` | add the source if its locator is new; priority = max |
| READY | any new `event_id` | intent: re-publish `representation.ready` (crawler2 dedupes on `representation_id`) |
| FAILED | a **new** `event_id` (never a redelivery of a seen one) that either carries an untried locator, or arrives after the last failure was retryable and its backoff elapsed | reopen → PENDING as a new episode (`failures` reset, claim bound re-based, §7.2) (P9-D8) |
| FAILED | otherwise | intent: re-publish the stored `encode.failed` envelope (same `event_id`) |

### 6.11 Re-request semantics (P9-D8) and default spec (P9-D5)

- Same idempotency key = same work (catalog, FROZEN); a new `event_id` adds
  a source; the fingerprinter never encodes a key twice. crawler2 does not
  send re-requests (FROZEN P8). After FAILED, recovery is the operator command
  `fingerprinter-encode retry` (§18).
- `required_spec` absent = the fingerprinter's default **at intake**,
  resolved once and stored as `resolved_spec`. `representation.ready` carries
  the concrete spec. `encode.failed.spec` **echoes the request's
  `required_spec`** (absent for `default`), so crawler2's `default`
  projection row shows FAILED.

## 7. Request lifecycle, due index, retries (P9-D9, P9-D23)

### 7.1 Authoritative state

`encode_requests` is authoritative. States: `PENDING` (with `next_attempt_at`),
`IN_PROGRESS` (with `attempt_id`, `lease_until`), `READY`, `FAILED`.
Transitions out of IN_PROGRESS are `IF state = 'IN_PROGRESS' AND attempt_id =
?` — the `attempt_id` (UUIDv7, new per attempt) is the **fencing token** for
request state. A stale worker cannot move a request it no longer owns.

### 7.2 Claim

Scheduler pops `fp:q:encode` (Lua: `ZPOPMIN` + `SET fp:lease:req:… attempt_id
PX lease_ms`), then `UPDATE encode_requests SET state='IN_PROGRESS',
attempt_id=?, lease_until=?, claims=n+1 IF state='PENDING' AND
next_attempt_at <= ? AND claims = n` (`lease_until = now + attempt_lease_s`).
The Redis lease avoids duplicate work only; correctness is the LWT.

**Attempt lease renewal (Scylla).** While an attempt runs, its worker renews
the durable lease every `attempt_lease_renew_s` (proposed 120 s;
`attempt_lease_s` proposed 600 s): write the new due entry (§7.3 write
order), `UPDATE … SET lease_until = now + attempt_lease_s IF state =
'IN_PROGRESS' AND attempt_id = ?`, then delete the old due entry. A failed
renewal (condition false) means the attempt was superseded: the worker stops
the attempt, deletes its uploaded object if the authoritative row does not
reference it, and makes no further request transition. If it had already
reached the representation LWT, that commit stands (§8.2). Without renewal,
any attempt longer than one lease (e.g. a 2 h film) would be requeued and
encoded again by another worker. The Redis work lease (`lease_ms`) is renewed
independently every `lease_ms/3`.

**Attempt accounting.** `claims` counts every claim (incremented in the claim
LWT). `failures` counts only attempts that ended with a failure attributed to
the request (any cause in §7.4, including a single-item GPU OOM). Attempts
released by the encoder (SIGTERM, GPU-lease loss, controlled exits releasing
*other* in-flight items, device errors) and attempts abandoned by lease
expiry do **not** increment `failures`. A request becomes FAILED when
`failures ≥ max_attempts` (4) or when `claims ≥ max_claims` (proposed 10;
`internal_error`, `retryable=true`, `detail=claim_budget_exhausted`). The
second bound stops a poison item that repeatedly kills or stalls encoders
without an attributed failure from looping forever. A reopen (§6.10, new
`event_id`) or the operator `encode retry` starts a new **episode**: it
resets `failures` and sets `claims_base = claims`; the claim bound applies to
`claims − claims_base`. `claims` itself never decreases, so every
`encode.failed` of a request has a distinct `attempt` (§7.4).

**Attempt wall-clock limit.** An attempt running longer than
`encoder.attempt_timeout_s` (proposed 4 h; measured value set in validation)
is stopped by its worker and counted as a `timeout` failure (retryable).
`orphan_grace_s` must exceed it.

### 7.3 Due index (no TTL)

- Every non-terminal request has ≥ 1 entry in `encode_due`, keyed by its due
  time: PENDING → `next_attempt_at`; IN_PROGRESS → `lease_until`.
- **Write order:** the new due entry (and its `encode_due_days` row) is written
  **before** the transition that sets that due time; the old entry is deleted
  **after** the transition succeeds. A crash leaves an extra entry, never a
  missing one. Stale entries are harmless: the scheduler re-reads the
  authoritative row and deletes entries that do not match it.
- Terminal transitions (READY/FAILED) delete the due entry after success.
- The scheduler reads `encode_due_days` ≤ today, scans those partitions for
  `due_at ≤ now`, re-validates, enqueues in Redis; an empty past day is removed
  from `encode_due_days` only after a scan finds it empty.
- **No TTL** on `encode_due` or `encode_due_days`: a request delayed by backoff
  or IN_PROGRESS for any duration stays discoverable.
- **Repair:** the reconciler (§8.4) runs a full token-range scan of
  `encode_requests` (default daily, rate-limited) and recreates the due entry
  of every non-terminal row that lacks a matching one
  (`fingerprinter_due_index_repaired_total`). Index loss is thus detected and
  repaired; Redis loss is repaired by rebuilding `fp:q:encode` from
  `encode_due`.
- Terminal rows are retained (they are the idempotency memory for redelivery
  and re-requests). Cleanup of old terminal rows is out of P9 (no TTL).

### 7.4 Retries and `encode.failed`

Retryable causes retry with backoff `10 min × 3ᵏ` (k = `failures`),
`max_attempts` 4 attributed failures and `max_claims` 10 claims (§7.2),
untried sources first. At FAILED one `encode.failed` is published via an intent (§8.1)
with `request_id` = the request's first `encode.requested` `event_id`
(`request_event_ids` minimum by arrival), `attempt` = `claims` (≥ 1,
monotonic, §7.2), `failure` = last cause, `retryable` = whether a later
re-request could succeed. **A request with no `encode.requested` event**
(target-only or backfill-only) publishes **no** `encode.failed` — the
contract's `request_id` must name a real request event; such failures are
visible on the target (`BUILD_FAILED`) or in the backfill report.
`representation.ready` is published for every committed representation
(its meaning does not depend on a request event). A later success publishes
`representation.ready`, which supersedes the failure (catalog, FROZEN).

| Cause | `failure` | `retryable` |
|---|---|---|
| DNS/connect/5xx/reset | `source_unreachable` | true |
| 401/403/451, SSRF refusal | `source_forbidden` | false |
| 404/410 | `source_unreachable` | false |
| SHA-256 ≠ requested digest (`detail=content_mismatch`) | `source_unreachable` | false |
| non-identity `Content-Encoding`, unsupported container/codec, no video stream | `unsupported_format` | false |
| > 8 GiB | `too_large` | false |
| ffmpeg error, zero frames, non-monotonic PTS; with `dinov2-seg/2`, an explicit demuxer truncation report even when ffmpeg exits 0 (§3.2a, `detail = truncated_input: <indicator>`) | `decode_error` | false |
| `encoder.attempt_timeout_s` exceeded (§7.2) | `timeout` | true |
| single-item GPU OOM, device failure, runtime incompatible, storage unavailable, `reference_missing` (non-retryable) | `internal_error` | true except `reference_missing` |

No new enum value is introduced (P1 FROZEN).

## 8. Commit protocol and reconciliation (P9-D13, P9-D20)

### 8.1 Intents (durable work written before facts)

An **intent** row in `intents(day, shard, intent_id)` records work that must
be completed once a fact exists: `kind` (`rep_commit`, `request_failed`,
`target_event`, `target_register`, `republish`), `fact_key` (e.g. `content_id, spec_digest`),
`attempt_id`, `event_id`, `artifact_uri`, `created_at`, `holder`. Intents are
written **before** the fact's LWT; if the intent write fails, the fact is not
attempted. Intents have **no TTL**; one is deleted only after its work is
complete **and** acknowledged on the fact row. `intent_days` lists partitions
that may hold intents (written before the first intent of a day; removed only
after a scan finds the partition empty).

### 8.2 Representation commit — steps and the commit point

1. Encode to scratch; upload `rep/<spec>/<content>/<attempt_id>.npz`; verify
   size and SHA-256 by `StatObject`/read-back.
2. Write intent `rep_commit(content_id, spec_digest, attempt_id, event_id,
   artifact_uri)`.
3. **`INSERT INTO representations … IF NOT EXISTS`** with the full row:
   `representation_id`, `artifact_attempt_id`, URI, sha256, size,
   segment_count, duration, runtime fingerprint, device, compute dtype,
   the request key `(content_id, spec_key)` that triggered it, the stored
   `representation.ready` envelope (pre-allocated `event_id`), follow-up flags
   all false, `ready_event_published_at = null`.
   **The authoritative commit point is the applied result of this LWT.** A
   representation exists and is READY iff this row exists; nothing else
   (object, index, event, request state) proves commitment.
   - LWT timeout (unknown outcome): resolve by a SERIAL read before any other
     step; the intent guarantees a reconciler will resolve it if the worker dies.
4. If applied (winner), perform follow-ups, each idempotent and each recorded
   by setting its flag on the row:
   a. `representations_by_spec` insert → `f_index = true`;
   b. request → READY (`IF attempt_id = ?`; if that fails, the request is
      READY-able anyway and the reconciler converges it) → `f_request = true`;
   c. target bindings for targets in `targets_by_content(content_id)` whose
      version awaits this spec (§10) → `f_targets = true`;
   d. publication (§9): XADD the stored envelope, then set
      `ready_event_published_at`.
   Then delete the intent.
5. If not applied (row exists, loser): the loser's object stays unreferenced;
   the loser deletes it only if `row.artifact_attempt_id ≠ attempt_id`, sets
   its request READY, writes a `republish` intent, deletes its own intent.

First committer wins: one authoritative row per `(content_id, spec_digest)`;
its `artifact_attempt_id` can never be replaced (no UPDATE of artifact
columns exists in the code paths). Equivalent outputs from other attempts are
discarded.

### 8.3 Crash at each boundary

| Crash | State left | Recovery |
|---|---|---|
| before upload | attempt IN_PROGRESS, no object | lease expiry → due entry → requeue; scratch cleaned at restart |
| during upload | partial/absent object (MinIO multipart not completed, or completed without intent) | requeue; incomplete multipart aborted by sweeper; a completed object without intent → orphan after grace |
| after upload, before intent | object, no intent, no row | orphan after grace (§8.5); requeue |
| after intent, before row | intent, object, no row | reconciler: SERIAL read; no row and attempt dead (request lease expired / attempt superseded) → delete object, delete intent; request requeued by due index |
| after row, before index | row (flags false), intent | reconciler completes 4a–4d from the row |
| after index, before pending publication | row (`f_index`), intent | reconciler completes 4b–4d |
| after XADD, before `ready_event_published_at` | row, intent, event in stream | reconciler re-publishes the same `event_id` (duplicate allowed by contract), sets ack, deletes intent |
| after ack, before intent delete | row acked, intent | reconciler sees all flags/ack → deletes intent |

### 8.4 Reconciler (any host, leased per partition, idempotent)

- **Intent pass** (every 60 s): for every intent older than `intent_grace_s`
  (120 s) in `intent_days` partitions: SERIAL-read the fact; if the fact
  matches the intent (`artifact_attempt_id`/`event_id`) → complete missing
  follow-ups and publication (subject to holds, §9); if the fact is absent and
  the attempt is dead → clean up (object, intent); if the fact belongs to
  another attempt → the intent's object is an orphan; delete the intent.
  Everything needed comes from the authoritative row (content_id,
  spec_digest, request key, stored envelope, artifact URI) plus
  `targets_by_content`.
- **Full pass** (daily, rate-limited, token-range scan) — defence in depth if
  an intent were ever lost: `representations` rows with a false flag or a null
  ack and no open intent → write a new intent; `encode_requests` non-terminal
  rows without a due entry → recreate it (§7.3); IN_PROGRESS rows whose
  representation is READY → READY; PENDING `target_representations` whose
  representation is READY → READY + `target.registered` intent (§10).
- Metrics: `reconciler_repairs_total{kind}`, `intents_open`, `intent_oldest_age_seconds`.

### 8.5 MinIO orphan sweeper

Runs after the reconciler's intent pass and never deletes an object that
the reconciler could still need:

1. List `rep/…` objects, `fingerprinter-targets/staging/…` objects and
   incomplete multipart uploads older than `orphan_grace_s` (24 h, >
   `encoder.attempt_timeout_s`, the longest allowed attempt). Staging objects
   are deleted after the reconciler has resolved their `target_register`
   intent (§10); steps 2–3 apply to `rep/` objects.
2. For each object `<attempt_id>`: if the authoritative row exists and
   `artifact_attempt_id = attempt_id` → **keep** (referenced). If an intent
   with that `attempt_id` is still open → **skip** (reconciliation first).
   If the request row's `attempt_id = attempt_id` and IN_PROGRESS with a
   valid lease → **skip** (active). Otherwise → delete.
3. A missing secondary index never makes a referenced object deletable
   (step 2 checks only the authoritative row).

## 9. Event publication (P9-D7, P9-D21, P9-D22)

### 9.1 Stream registry and holds

`stream_registry(stream)`: `required_groups` (set), `hold_until_registered`
(bool), registration audit. Seeded at migration (PROPOSED, P9):

| Stream | Required groups | Hold | Reason |
|---|---|---|---|
| `events:representation.ready.v1` | `media-registry` | yes | FROZEN P8 consumer (created by crawler2 `ensure_group` at ID 0) |
| `events:encode.failed.v1` | `media-registry` | yes | same |
| `events:target.registered.v1` | none registered yet | **yes** | catalog consumers (CRAWL_INTELLIGENCE, MATCHER) do not exist yet; no crawler2 consumer exists today (EXISTING) |
| `events:target.retired.v1` | none registered yet | **yes** | same |

Rule: the publisher publishes a stream's events only if
`hold_until_registered = false` **or** every group in `required_groups`
exists (`XINFO GROUPS`) and the set is non-empty. Otherwise events stay
**held**: their intents and stored envelopes remain in Scylla (durable, no
TTL). **Nothing is ever published to a stream before its required consumers
exist**, so Redis trimming cannot destroy the only copy of an event a future
consumer needs.

- **Registration:** a future consumer (P7 follow-up, P10 matcher) first
  creates its group (at ID `0`, like crawler2's `ensure_group`), then the
  operator runs `fingerprinter-publisher register-consumer --stream … --group …`,
  which verifies that the group exists and records it. The publisher then
  releases held events oldest first.
- **Bootstrap/recovery for later consumers:** target facts are authoritative
  in Scylla, so `fingerprinter-publisher republish --stream
  target.registered.v1 [--since …]` re-emits the stored envelopes (same
  `event_id`s) of every current target version — consumers dedupe on the
  catalog key. A consumer registered long after a target was published can
  therefore always be brought up to date.
- **Deployment order is irrelevant:** a fingerprinter started before crawler2's
  `media-registry` group exists holds `representation.ready`/`encode.failed`
  until it appears (alarm after `hold_alarm_s`, 1 h).
- A registered group that disappears (consumer decommissioned) makes the
  stream held again (alarm); the operator updates the registration.

### 9.2 Publication, acknowledgement, retention

- **Created:** the intent at fact time (§8.1). Eligible when the fact is
  committed and the stream is not held.
- **Publish:** XADD the stored envelope under the ADR-022 rules applied by the
  fingerprinter's own publisher to its own streams: consumer-aware `XTRIM
  MINID` (never past an unacknowledged entry of any group), byte budget
  `stream_max_bytes` (64 MiB) with backpressure.
- **Ack:** after XADD succeeds, set the fact row's `*_event_published_at`
  (`ready_event_published_at`, `failed_event_published_at`,
  `registered_event_published_at`, `retired_event_published_at`), then delete
  the intent.
- **Duplicates:** a crash between XADD and ack re-publishes the same
  `event_id` — at-least-once, consumers dedupe (FROZEN contract).
- **Publication ≠ processing:** the ack means "appended to the stream";
  downstream processing is the consumer group's acknowledgement, which the
  publisher only uses for trimming (never trims unacknowledged entries). The
  fingerprinter never treats an XADD as processed.
- **Retention:** intents are removed only after ack; held events are never
  removed; there is no TTL. Old acknowledged envelopes stay on the fact rows
  (needed for republish).
- **Consumer offline:** its group's pending entries are retained; when the
  stream reaches its byte budget, publication of that stream waits
  (backpressure); facts stay durable; crawling is unaffected (these are
  fingerprinter streams).
- **Alerts:** `events_unpublished{stream}` (excluding held),
  `event_oldest_unpublished_age_seconds` warn 15 min / critical 1 h;
  `events_held{stream}` and `stream_hold_age_seconds` (info until a registration
  is expected, alarm for representation streams after 1 h);
  `publisher_backpressure_total`.

## 10. Target manager (P9-D14)

- **Register** (`fingerprinter-target register --title … --kind … --media <path>
  [--alias …] [--release-year …] [--target-id …]`): new `TargetId` (P1
  `AllocatedId`) or next version (`version = max + 1`, LWT `IF NOT EXISTS` on
  `(target_id, version)`). Retrying a registration with the same
  `--target-id` and identical bytes is idempotent (same ContentId, the
  existing version is returned); different bytes create a new version.
- **Reference upload (content-addressed, two passes; the final key is known
  before it is written):**
  1. **Hash locally.** The CLI reads the local file once and computes
     SHA-256 `D` and size `n`; `n > 8 GiB` → refused. No extra local copy.
  2. **Intent.** Write intent `target_register(upload_id, D, target_id?,
     staging key)` (§8.1) before any object is written.
  3. **Reuse check.** If `ref/<D>` exists with size `n` and user metadata
     `sha256 = D` (set only by step 5), skip to step 6.
  4. **Stage.** Upload the file to `fingerprinter-targets/staging/<upload_id>`
     (multipart) while computing SHA-256 again over the bytes sent. If it
     differs from `D` (the file changed between passes) → delete the staging
     object, close the intent, fail the registration.
  5. **Promote.** Server-side copy `staging/<upload_id>` → `ref/<D>` with
     metadata `sha256 = D` (multipart copy/compose for objects > 5 GiB, the
     S3 single-copy limit), `StatObject` to verify size, then delete the
     staging object. Concurrent registrations of the same bytes may both
     promote: they write identical verified bytes to the same key, so the
     last write is harmless (S3 PUT/copy replaces atomically for readers).
  6. **Facts.** `targets_by_content`, then the `targets` row (LWT), then the
     request upsert and binding (below); then the intent is deleted.
  - **Crash recovery:** the reconciler resolves `target_register` intents
    older than the grace: staging object present → delete it; target row
    present → done; `ref/<D>` without any `targets_by_content` row for
    `ContentId(D)` and no target row → delete `ref/<D>` after
    `orphan_grace_s`. The orphan sweeper also deletes `staging/` objects and
    incomplete multipart uploads older than `orphan_grace_s`.
  - **Purge** deletes `ref/<D>` only when no target version (any state other
    than purged) references `ContentId(D)` in `targets_by_content`.
- **Representation:** upsert `encode_requests(content_id, 'default')` with an
  `internal_object` source and priority 90. If a READY representation of the
  resolved spec already exists (the same bytes were crawled or registered
  before), it is **reused** — no encode.
- **Binding:** `target_representations(target_id, version, spec_digest)`
  is written PENDING (after `targets_by_content`), then the target manager
  reads `representations(content_id, spec_digest)` (SERIAL). If the
  representation already exists (reuse — no commit will follow), the target
  manager sets the binding READY itself and writes the `target.registered`
  intent. Otherwise the commit follow-up 4c (§8.2) binds it. Because
  `targets_by_content` is written before the read, any commit either
  happened before the read (seen by the target manager) or reads
  `targets_by_content` afterwards (follow-up 4c) — no binding is missed. The
  reconciler full pass (§8.4) also binds any PENDING binding whose
  representation is READY. READY binding = `RepresentationId.for_target(target_id,
  version, spec…)` bound to the content `representation_id`.
- **Default spec for a target** = the current `spec_roles['default']` at
  registration; on a later default change, backfill (§11) creates the new
  spec's representation and binding; the old binding stays.
- **Events:** `target.registered` (intent at the moment the default binding is
  READY) and `target.retired` are durable facts on the target row and are
  **held** until consumers are registered (§9.1). Pending state is visible via
  `fingerprinter-target show`.
- **Failure:** the target version stays `REGISTERING`/`BUILD_FAILED` with the
  request failure; `encode retry` re-runs it; no event until READY.
- **Retire:** state RETIRED + held `target.retired`; representations kept;
  reference media deleted only by explicit `purge`.
- V1 target `blast:v1` (EXISTING, Redis) is not migrated; it may be
  re-registered locally (local-only file, data-preparation §6).

## 11. Specs, backfill, retirement (P9-D15)

### 11.1 Lifecycle

`spec add <document>` (validates canonical form, origin profile exists;
status `active`, or `gated` for a spec that requires a gate first, such as an
FP16 spec, §3.7) → for a gated spec, `spec enable <digest> --evidence <file>`
only after its gates passed → `backfill create` → `spec set-default <digest>`
(LWT on `spec_roles`; refused for a `gated` spec; affects new `default`
requests only) → `spec deprecate` → `spec retire`. Old and new
representations coexist (different `spec_digest` rows and objects).

### 11.2 Backfill (`fingerprinter-backfill create|run|pause|resume|cancel|status`)

- **Eligible content:** contents with a READY representation of `from_spec`,
  enumerated from `representations_by_spec(from_spec, bucket)` in bucket order
  (the reconciler guarantees the index is complete); optional filters
  (targets only, since date, sample). Missing-representation check against
  `representations(content_id, to_spec)`.
- **Work:** upsert `encode_requests(content_id, to_spec_digest)` at
  `backfill_priority` (10) with the original sources (external) or the target
  reference (internal).
- **Bounds:** `max_in_flight` (200), `max_per_hour`, optional `budget`; pauses
  while non-backfill work above `queue_high_water` is waiting, so live work
  keeps priority on GPU and CPU encoders alike.
- **Progress:** per-bucket cursor in `backfill_progress`, job state by LWT on
  `epoch`; pause/resume/cancel are job-state LWTs; a crashed runner resumes
  from the last committed bucket cursor; re-running a bucket is idempotent.
- **Retries/errors:** the encode retry policy (§7.4); per-class failure
  counts and a list of contents whose sources vanished (old representation
  kept) in `backfill status --json`.
- Never destructive: no row or object of `from_spec` is touched.

### 11.3 Coexistence

Requests after `set-default` use the new spec; older contents keep only the
old spec until backfilled. crawler2's suppression is satisfied by either
(FROZEN rule "any ready"). P10 switches its index when coverage suffices.

### 11.4 Retirement

`spec retire` is refused while the spec is default, read by a running
backfill, or listed in `retire_guard` (P10 registers index specs). It marks
rows RETIRED and deletes objects after `retire_grace_s` (7 d). No event (none
exists in the contract).

## 12. Encoder process (P9-D16, P9-D25)

### 12.1 GPU ownership

- One GPU-owning process per host: `fingerprinter-encoder run --device cuda`
  acquires `fp:gpu:<host_id>` (`SET NX PX 30000`, renewed every 10 s by CAS;
  proposed parameters, to be validated). If held → exit code 73
  (`gpu_lease_held`) before loading the model.
- **Lease loss** (renewal fails or CAS mismatch): stop claiming at once; abort
  in-flight GPU batches; release each in-flight attempt explicitly
  (`IN_PROGRESS → PENDING`, `IF attempt_id = ?`, backoff 0) where Scylla is
  reachable, otherwise leave them to lease expiry; exit 75 (controlled). The
  process never uses the GPU without the lease.
- **Safe shutdown (SIGTERM):** stop claiming, finish batches in flight up to
  `shutdown_grace_s` (60 s), commit finished attempts, release the rest,
  release the GPU lease, exit 0.
- Multiple GPU owners on one GPU are not permitted (not designed or tested).

### 12.2 Pipeline and bounded memory

```
claim threads (C=2) → acquire to scratch (typed source, §5)
decode threads (D=3) → ffmpeg select/showinfo → rgb24 pipe → preprocess/v1 (CPU)
      → bounded queue (Q = 4·B items; producers block when full)
inference thread (1, owns the device) → batches ≤ B across videos → CLS → fp32 → L2
assembler → per-attempt arrays indexed by segment_index → artifact → commit (§8)
```

Decode concurrency is independent of inference. On CPU,
`torch.set_num_threads(inference_threads)` with `D·ffmpeg_threads +
inference_threads ≤ cores`. Memory bounds: queue ≤ Q × 602 KiB; per-attempt
vector arrays ≤ N × 3 KiB; `max_rss_bytes` watchdog (proposed 4 GiB) stops
claiming and triggers a controlled exit if exceeded.

### 12.3 Calibration and batch OOM recovery

- Calibration on first start per (host, device, spec, dtype), method
  **`throughput/v1`** (operator decision OF-2, 2026-10-11; replaces
  `B_start = B_max/2`):
  1. *eligibility* — B = 1, 2, 4, … ≤ `max_batch` (256); each B gets one
     warm-up batch on the device; an OOM makes it and every larger B
     ineligible (not tried); an OOM during timing also makes that B
     ineligible;
  2. *measurement* — each eligible B embeds `max(calibration_frames (64), B)`
     frames `calibration_reps` (3) times with the device synchronized; same
     fixed synthetic preprocessed inputs for every B;
  3. *confirmation* — the two candidates with the highest median are measured
     `calibration_reps` more times and decided on all samples;
  4. *selection* — highest median frames/s; medians within
     `calibration_tolerance` (5 %) of it count as equal and the smallest of
     them is chosen. The largest fitting B is never chosen for fitting alone.
  The report (every candidate's samples, median, min/max, spread, peak
  reserved VRAM; selection and reason; host, device, spec, runtime
  fingerprint, GPU name/memory/capability, torch/CUDA) is stored in
  `encoder_hosts.calibration` with `b_max` (largest fitting) and `b_start`
  (selected). Rows without a `throughput/v1` report are recalibrated. Cost is
  bounded (≤ 9 candidates × 4 passes + 6 confirmation passes). Failure: B = 1
  does not fit → controlled exit 75; device error → exit 70; GPU lease
  released in both. Stable size = B after `stable_after` (500) OOM-free
  batches. No automatic increase (operator `recalibrate`).
- **Recoverable batch OOM (B > 1):** catch `torch.cuda.OutOfMemoryError`
  around the forward pass; drop the batch's device tensors;
  `torch.cuda.empty_cache()`; halve B; retry **the same in-memory items**.
  Items leave the batch only when their vectors are stored at
  `(attempt_id, segment_index)` — no loss, no duplicate. Counted
  `encoder_oom_recovered_total`; not a failure.

### 12.4 Failure classes (resolves review issue G)

| Class | Detection | Required behaviour | Counts as uncontrolled crash? |
|---|---|---|---|
| 1. Recoverable batch OOM | `OutOfMemoryError` at B > 1 | §12.3 | no |
| 2. Single-item OOM | `OutOfMemoryError` at B = 1 | the item's attempt → PENDING with backoff, `last_failure = internal_error/gpu_oom_single`; after `max_attempts` → FAILED `internal_error` retryable=true; encoder stops claiming, releases all in-flight attempts explicitly, releases the GPU lease, exits **75 (controlled)**; the supervisor restarts it (fresh CUDA context, recalibration). Repeated within 10 min → alarm, supervisor backs off | no (controlled exit, reported) |
| 3. Process crash | signal, unhandled exception, segfault | none possible in-process; work leases expire → requeue; scratch cleaned at restart; GPU lease expires ≤ 30 s | **yes** |
| 4. GPU/device failure | CUDA error other than OOM (illegal address, launch failure, device lost/Xid) | stop claiming, release attempts (retryable `internal_error/device_error`), release lease, exit **70**; restart only after a device health check passes; no silent switch to CPU | no if handled; yes if the process dies |
| 5. Host memory exhaustion | RSS watchdog / kernel OOM killer | watchdog: controlled exit 75 before the limit; an OOM-killer kill is class 3 | killer: **yes** |

Nothing in P9 claims that every input can be encoded regardless of GPU
memory; an item that cannot be processed ends FAILED with a retryable class.

### 12.5 CPU fallback and startup

- `--device cpu`: same pipeline, FP32, batch `cpu_batch` (8), thread budget
  per §12.2. CPU artifacts share the identity with CUDA **FP32** artifacts
  (same spec, allowed profile, tolerance §3.6). A CPU encoder never claims
  FP16-spec work (§3.7).
- `--device cuda` with CUDA unavailable, NVML/driver error at start, model
  revision missing from the local cache, golden check failing, or runtime not
  allowed (§3.5) → exit non-zero with a typed error. `--device auto` resolves
  once at start, exports `encoder_device`, never switches at runtime.
- A CPU encoder never reports results under a GPU benchmark label; benchmark
  JSON records the actual device (§13).
- ENV: the dev host currently has an NVML mismatch (kernel 610.57.04 vs
  library 615.71); GPU paths are untestable until it is fixed.

### 12.6 Configuration (`FINGERPRINTER_*`, pydantic)

`host_id`, roles; `scylla.*`, `redis.*`, `minio.*`; `encoder.device` (`auto`),
`encoder.max_batch` (256), `encoder.cpu_batch` (8),
`encoder.calibration_frames` (64), `encoder.calibration_reps` (3, ≥ 3),
`encoder.calibration_tolerance` (0.05) (§12.3), `encoder.queue_items`
(4·B), `encoder.decode_threads` (3), `encoder.claim_threads` (2),
`encoder.inference_threads`, `encoder.lease_ms` (60 000),
`encoder.gpu_lease_ms` (30 000), `encoder.gpu_lease_renew_ms` (10 000),
`encoder.max_attempts` (4), `encoder.max_claims` (10), `encoder.attempt_timeout_s`
(14 400), `encoder.attempt_lease_s`
(600), `encoder.attempt_lease_renew_s` (120), `encoder.max_rss_bytes`,
`encoder.shutdown_grace_s`;
`acquire.max_bytes` (8 GiB), `acquire.timeout_s` (3600),
`acquire.read_timeout_s` (60), `acquire.max_redirects` (5); `scratch_dir`,
`scratch_max_bytes`; `intake.lease_ttl_s` (60), `intake.replay_margin_s`
(86 400), `intake.crawler2_outbox_retention_s` (1 209 600);
`publisher.stream_max_bytes` (64 MiB), `publisher.hold_alarm_s` (3600);
`reconciler.intent_grace_s` (120), `orphan_grace_s` (86 400),
`retire_grace_s` (604 800); `backfill.*`. All values are proposals; measured
values replace batch/thread defaults in validation.

### 12.7 Observability (`fingerprinter_` prefix)

Encoder: `encoder_device`, `encoder_batch_size`, `encoder_batch_calibrated_max`,
`encoder_queue_depth`, `encoder_gpu_idle_seconds_total`,
`encoder_decode_blocked_seconds_total`, `encoder_frames_total{device}`,
`encoder_oom_recovered_total`, `encoder_oom_single_total`,
`encoder_controlled_exits_total{reason}`, `encoder_vram_bytes{kind}`,
`encoder_host_rss_bytes`, `encode_stage_seconds{stage=acquire|decode|preprocess|infer|upload|commit}`.
Work: `encodes_total{outcome}`, `representations_committed_total`,
`requests{state}`, `due_index_repaired_total`. Intake: `intake_lag_entries`,
`intake_open_gaps`, `intake_invalid_total`. Events: §9.2. Reconciler/sweeper:
§8.4, `orphans_deleted_total`. Runtime: `runtime_incompatible_total`,
`golden_check_total{result}`. Logs carry worker identity, attempt and
content IDs, never full media URLs.

## 13. Performance and precision measurement (P9-D17)

### 13.1 What exists (EXISTING)

Only a synthetic CPU figure: **1.80 s per minute of video** on
`bench_60s.mp4` (ffmpeg `testsrc2`, 320×240, 24 fps, H.264), V1 engine, FP32,
batch 1, 6 torch threads, i5-11400H; includes decode + PNG extraction +
inference; excludes download and storage (audit §7). It is **not** a
real-media or end-to-end baseline. A real 2 h 21 min HEVC 1920×800 film took
> 300 s just to decode (V1 timeout). No GPU measurement exists; GPU validation
is blocked by the NVML mismatch (ENV).

### 13.2 Metrics measured separately (benchmarks B-*, data-preparation §5)

| Metric | Definition |
|---|---|
| Decode throughput | decoded frames/s and selected segments/s of the ffmpeg stage alone, per resolution/codec |
| Preprocess throughput | frames/s of `preprocess/v1` on D threads |
| Inference throughput | frames/s from preprocessed tensors in RAM to L2-normalized vectors on the host |
| End-to-end CPU | s per minute of video and segments/s, acquisition from a local fixture server through commit, CPU device |
| End-to-end GPU | same with the GPU encoder |
| Storage/network overhead | upload + Scylla commit + publication time per attempt |
| Memory/batch | peak RSS, `max_memory_allocated`/reserved VRAM, calibrated `B_max`, stable B, OOM counts |

Methodology: hardware and runtime fingerprint recorded (CPU, RAM, GPU,
driver, CUDA, profile, spec digest, B, threads, corpus manifest SHA-256);
1 warm-up item discarded; cold start reported separately; ≥ 3 repetitions per
workload and n ≥ 30 per reported percentile (else the small-sample warning);
median and p95; CPU baseline = CPU fallback using all cores on the same host;
same inputs for both devices.

### 13.3 Gates (none measured)

- **G-GPU (exit gate, APPROVED P9-D17):** GPU inference-stage throughput
  ≥ **5×** CPU inference-stage throughput on the same host using the same
  preprocessed inputs (C-PREC frames held in RAM), CPU fallback on all cores.
  Measured for the FP32 default spec (GPU FP32 vs CPU FP32). An FP16 spec's
  throughput is measured and reported separately; it can be used to meet the
  gate only if that FP16 spec has passed the precision gates and been enabled
  (§3.7), and the report names the spec measured. Unverified until GPU tests
  run on a working driver/runtime. **Threshold and methodology confirmed
  unchanged by operator decision OF-3 (2026-10-11):** synthetic inputs never
  substitute C-PREC; a valid C-PREC result below 5× is FAIL and is
  investigated, never answered by changing the threshold or method.
- **End-to-end speedup:** measured and **reported separately**; no threshold
  until a valid real-corpus benchmark exists (CPU decode dominates long
  high-resolution media, audit R-6; an estimate of 1.5–3× is unmeasured).
- **Precision gate** (§3.7): on C-PREC and L-PAIRS (data-preparation §2):
  segment alignment exact (same indices and PTS by construction — the same
  decoded tensors are fed to every mode); per-segment cosine ≥ 0.999 vs CPU
  FP32; identical match/no-match decisions and matched-segment alignment from
  the frozen V1 `matching.matcher.match_segments` with today's
  `MatcherConfig` (operator interpretation OF-4, 2026-10-12, decisions §5b:
  decisions identical for every pair; alignment identical for every pair
  classified as a match; a non-match's partial alignment is diagnostic only).
  Pass → the FP16 spec may be enabled/made default by the
  operator (still a separate identity); fail → it stays `gated` (§3.7).
- **Soak gate** (1,000 videos, SOAK-1000), observable:
  1. zero uncontrolled process terminations (class 3 or killer, §12.4);
  2. zero lost requests: every request created ends READY or FAILED with a
     recorded class; zero non-terminal requests left after drain + 2 × lease;
  3. recovered batch OOMs, single-item OOMs and controlled exits are reported
     with counts; every controlled exit is followed by a restart and the
     remaining eligible work completes;
  4. single-item-OOM items end in the correct retryable state;
  5. GPU and work leases released or expired correctly (no attempt held by a
     dead process after lease expiry);
  6. RSS and VRAM bounded (no upward trend over the run; peak below limits);
  7. zero unreferenced objects and zero open intents after drain, checked
     after a sweeper run with the soak's configured `orphan_grace_s`
     (shortened for the run, e.g. 15 min, and recorded — the production 24 h
     default would otherwise make this unobservable within a run window);
  8. expected failures for the 30 corrupt/audio-only files, no crash.

## 14. P9/P10 boundary

P10 consumes `representation.ready` (after registering its `matcher` group,
§9.1), F1/F2/F4 tables and artifacts (§3.8), and target events after
registering. P10 owns the index (rebuilt from `representations_by_spec`),
candidate retrieval, verification, thresholds, `match.found`, equivalence and
any change of `segment_duration_s` (a new spec + P9 backfill). P9 never
compares representations at runtime.

## 15. Test and acceptance matrix (APPROVED matrix; per-test execution status in traceability.md and validation.md)

Unit tests run per commit; integration tests use real Scylla/Redis/MinIO from
crawler2's compose stack with throwaway keyspaces/buckets; crash injection uses
named hook points (as P8). "Two-host" means **simulated** (§15.11).

### 15.1 Identity (TI)
| ID | Test | Pass condition |
|---|---|---|
| TI-1 | same ContentId + spec, two attempts | equal RepresentationId; one authoritative row |
| TI-2 | different ContentIds | different RepresentationIds and rows |
| TI-3 | change model revision / any preprocess, decode, sampling, runtime-profile field | new `config_digest`; canonicalization ignores key order/whitespace |
| TI-4 | same bytes at two locators / new media version | same RepresentationId; both locators in provenance |
| TI-5 | encoder runtime fingerprint not in `spec_runtime_compat` | refuses to claim; metric + alarm; no artifact |
| TI-6 | golden fixtures (PTS on/±1 tick of boundaries, VFR, holes, rotation, colour tags) | RGB hashes, indices, PTS bitwise equal; vectors per §3.6 |
| TI-7 | repeated processing of the same bytes | no second authoritative row; loser object removed |
| TI-8 | CPU encoder and CUDA encoder with equal common components; one with a differing common component; CUDA variant not admitted | first two allowed for the FP32 spec; differing common component refused; CUDA refused until admitted |

### 15.2 Intake and gaps (TG)
| ID | Test | Pass condition |
|---|---|---|
| TG-1 | normal intake | every entry upserted once; cursor/ordinal advance |
| TG-2 | duplicate delivery (same event_id; P8 physical copies) | no new work |
| TG-3 | crash before upsert / after upsert before cursor CAS | re-read; one request row |
| TG-4 | lease expiry, second reader takes over; stale CAS | stale cursor write rejected; no skip |
| TG-5 | trim of unread entries (`T > o`) | gap OPEN with exact `missing_count`; reader continues |
| TG-6 | trim of read entries only, non-contiguous IDs (`T ≤ o`) | no gap |
| TG-7 | empty stream; missing stream with `o = 0` / `o > 0` | idle / `HALTED(stream_reset)` |
| TG-8 | uninitialized cursor | `HALTED(uninitialized)` until `init` |
| TG-9 | replay with new IDs | missing requests ingested; duplicates no-ops; CLOSED_REPLAYED after `cursor ≥ replay_end_id` |
| TG-10 | replay entries trimmed before read; replay interrupted and restarted | REPLAY_INCOMPLETE; second replay closes; no automatic loop |
| TG-11 | window outside outbox retention | REPLAY_UNAVAILABLE; only `accept-gap` closes |
| TG-12 | `accept-gap` | audit row (operator, reason, counts, window); state CLOSED_ACCEPTED; no cursor change; never automatic |
| TG-13 | `XDEL` after cursor (synthetic) | conservative gap record |
| TG-14 | status/metrics with open gaps | non-zero status, `intake_open_gaps > 0` |

### 15.3 Commit and reconciliation (TC)
| ID | Crash/Case | Pass condition |
|---|---|---|
| TC-1…TC-8 | each boundary of §8.3 | the recovery column holds; exactly one authoritative row; follow-ups and ack complete; no lost request |
| TC-9 | LWT timeout with unknown outcome | SERIAL resolution; no second row; no premature object deletion |
| TC-10 | two workers commit simultaneously | one winner; loser cannot change `artifact_attempt_id`; loser object deleted |
| TC-11 | stale worker after lease loss | can win only an uncontested insert; cannot move the request |
| TC-12 | sweeper vs referenced/active/intent-held objects | never deleted; abandoned objects deleted after grace |
| TC-13 | missing index with referenced object | object kept; index repaired |
| TC-14 | full pass with an intent removed by hand | row repaired from authoritative data |

### 15.4 Events and consumers (TE)
| ID | Test | Pass condition |
|---|---|---|
| TE-1 | publisher down for a simulated > 14 days (clock injection) | all events published afterwards; nothing expired |
| TE-2 | crash after XADD before ack | same `event_id` re-published; consumer dedupes |
| TE-3 | target event before any consumer | held; stream not written |
| TE-4 | consumer group created + `register-consumer` | held events released oldest first; `republish` replays current targets |
| TE-5 | `media-registry` group missing at start | representation events held, alarm; released when the group appears |
| TE-6 | consumer offline, byte budget reached | backpressure; no trimming of unacknowledged entries; facts durable |
| TE-7 | XADD success not treated as processing | no state depends on downstream ack |
| TE-8 | target-only request fails; crawl request for the same content fails after a reopen | no `encode.failed` for the target-only request; the reopened request's second `encode.failed` has a larger `attempt` (distinct idempotency key) and is applied by crawler2 |

### 15.5 Due index (TD)
| ID | Test | Pass condition |
|---|---|---|
| TD-1 | request PENDING/IN_PROGRESS > 30 days (clock injection) | still scheduled |
| TD-2 | retry backoff spanning > 30 days | discoverable at due time |
| TD-3 | due entry deleted by hand | full pass recreates it |
| TD-4 | crash between new-entry write and transition | extra entry, cleaned; never missing |
| TD-5 | terminal cleanup | removes only terminal entries |
| TD-6 | Redis flush | queue rebuilt from `encode_due` |
| TD-7 | attempt running longer than several `attempt_lease_s` (slow decode fixture) | lease renewed; no requeue; exactly one claim |
| TD-8 | renewal fails (attempt superseded by a forced requeue) | worker stops; no request transition; unreferenced object deleted; at most one authoritative row |

### 15.6 Sources (TS)
| ID | Test | Pass condition |
|---|---|---|
| TS-1 | external URL | identity bytes hashed; ContentId verified |
| TS-2 | redirect to another public host | followed; SSRF checked; Referer per policy |
| TS-3 | redirect to private/loopback/link-local | refused `source_forbidden` |
| TS-4 | `Content-Encoding: gzip` | refused `unsupported_format`; never hashed decoded |
| TS-5 | digest mismatch | `content_mismatch`; next source |
| TS-6 | size exactly 8 GiB (sparse fixture) / 8 GiB + 1 | accepted / `too_large` |
| TS-7 | internal object (allowlisted) | read via MinIO SDK, never HTTP; verified |
| TS-8 | internal object outside allowlist / in an event | rejected at validation / at intake |
| TS-9 | missing internal object; transient MinIO failure | `reference_missing` non-retryable; transient retried |

### 15.7 GPU/CPU (TX)
| ID | Test | Pass condition |
|---|---|---|
| TX-1 | `--device cuda` without CUDA; model missing | typed non-zero exit |
| TX-2 | CPU fallback encode | identity and tolerance per §3.6 |
| TX-3 | GPU lease acquire/renew; second process | second exits 73 |
| TX-4 | lease lost (Redis paused) | stops claiming; attempts released; exit 75 |
| TX-5 | SIGTERM | finished work committed; rest released; lease released |
| TX-6 | forced batch OOM (4·B_max + reserved VRAM) | B halved; same items retried; artifact equals a clean run within tolerance; no lost/duplicate segments |
| TX-7 | forced single-item OOM | retryable state; controlled exit 75; restart completes remaining work |
| TX-8 | injected device error | exit 70; attempts released; no CPU switch |
| TX-9 | bounded queue under slow GPU / slow decode | RSS ≤ bound; starvation/saturation metrics move |
| TX-11 | attempt exceeding `attempt_timeout_s` (slow fixture, shortened limit) | worker stops it; `timeout` failure, retryable; lease and objects cleaned |
| TX-10 | poison item that kills the encoder on every claim; items released by controlled exits | poison item FAILED `claim_budget_exhausted` after `max_claims`; released items do not consume `failures` and complete later |

### 15.8 Precision (TP) — needs C-PREC, L-PAIRS, working GPU
TP-1 alignment exact; TP-2 per-segment cosine ≥ 0.999; TP-3 identical
decisions and alignment with the V1 oracle (alignment of matched pairs,
OF-4); TP-4 the FP16 spec stays
`gated` (not claimable, not default) until TP-1…3 pass; TP-5 FP16 and FP32
specs have different digests and `RepresentationId`s for the same content,
before and after the gate passes (no merge); TP-6 a CPU encoder never claims
FP16-spec work.

### 15.9 Reuse and backfill (TR)
| ID | Test | Pass condition |
|---|---|---|
| TR-1 | **S1/S2** (exit gate): S1 = A–E, S2 = C–G (C–E at new URLs) | exactly **7** committed encodes (not 10); C, D, E not re-encoded; crawler2 projection shows 7 ready contents |
| TR-2 | S1/S2 + crawler2 relay replay of S1 | 0 new encodes; ready re-published |
| TR-3 | S1/S2 with intake restart mid-S2 | 7 encodes |
| TR-7 | target upload: crash after staging, after promotion before facts; file changed between passes; concurrent same-bytes registrations; object > 5 GiB (multipart copy) | reconciler/sweeper leave exactly `ref/<D>` referenced by one target version, no staging leftovers; changed file refused; purge keeps shared references |
| TR-4 | repeated target registration; target bytes equal C (representation already READY) | no encode; binding set READY by the target manager; `target.registered` intent created and held; registration concurrent with C's commit binds exactly once |
| TR-5 | backfill interrupted, resumed, retried, cancelled | idempotent; bounded in-flight; old reps available throughout |
| TR-6 | retire guards | refused while default/backfill/guarded; objects removed only after grace |

### 15.10 Performance and soak (TB) — see §13 and data-preparation §5.

### 15.11 Simulated two-host (TH)
Two fingerprinter instances with separate `host_id`, scratch and process
spaces (host GPU process + CPU container) on one machine, sharing
Redis/Scylla/MinIO: TH-1 shared durable state; TH-2 duplicate requests
idempotent; TH-3 first-committer-wins on concurrent attempts; TH-4 GPU lease
per host; TH-5 one instance killed → no lost work; TH-6 reconciliation
completes interrupted commits; TH-7 no shared local filesystem needed;
TH-8 GPU-host and CPU-host artifacts of the same content agree (§3.6).
**Simulated only**; real network partitions and machine failures are P14.

## 16. crawler2 integration (P9-D19)

The adapter the plan names already exists (EXISTING, FROZEN P8): requester
and `RepresentationLoop` (`media-registry` group). P9 adds only
cross-service tests in crawler2 (`tests/integration/fingerprinter/`): they run
the fingerprinter (image or host process) against the shared stack, drive
crawler2's P8 pipeline with the fixture web, and assert on crawler2's own
tables and stream contents; fingerprinter counters come from its `/metrics`
or CLI JSON — no import of fingerprinter code, no read of its keyspace. No
`match.found` consumer (catalog consumers are P11/P12).

## 17. Failure matrix (summary)

| Failure | Behaviour | Section |
|---|---|---|
| encoder crash / host loss | lease expiry → due index → requeue; intents reconciled; orphans swept | §7, §8 |
| Redis loss | queue rebuilt from `encode_due`; Redis-held leases lost → requeue; intake unaffected (Scylla lease) | §4.3, §7.3 |
| Scylla unavailable | typed errors; no claims/commits; nothing skipped | — |
| MinIO unavailable | attempt released retryable before commit | §8.2 |
| stream trimmed while intake was behind | gap record, continue, replay/accept | §6 |
| crawler2 group missing / lagging | events held / backpressure; facts durable | §9 |
| runtime drift | refuse to produce | §3.5 |
| OOM classes | §12.4 | §12.4 |
| source changed bytes | `content_mismatch` | §5 |

## 18. Operator procedures (CLIs, fingerprinter repo)

`fingerprinter-intake init|status|gap list|gap show|gap replay-start|gap replay-done|gap mark-unreplayable|accept-gap|reinit`;
`fingerprinter-encode status|retry --content … | --failure … --since …`;
`fingerprinter-spec add|list|enable|set-default|deprecate|retire`;
`fingerprinter-runtime fingerprint|check|approve-compat --spec … --profile … --evidence …`;
`fingerprinter-backfill create|run|pause|resume|cancel|status`;
`fingerprinter-target register|show|list|retire|purge`;
`fingerprinter-publisher status|register-consumer|republish`;
`fingerprinter-reconcile run|full`; `fingerprinter-encoder run|calibrate|recalibrate`.
Every state-changing command writes an audit record and supports `--json`.

## 19. Repository layout, dependencies, deployment

New package `fingerprinter/` (`config`, `storage` (Scylla, MinIO,
migrations), `intake`, `encode` (sources, decode, preprocess, model, batcher,
assembler, artifact), `registry`, `targets`, `backfill`, `publish`,
`reconcile`, `cli`, `observability`); tests `tests/v2/`; benchmarks
`benchmarks/p9-encoder/`. Dependencies to add at implementation (P9-D12, not
now): scylla-driver, minio, pydantic, prometheus-client,
antipiracy-contracts (pinned; the sanctioned shared package, ADR-007),
pytest-cov; a lock file realising `rt-1`. A CPU `Dockerfile` for the
simulated two-host profile; the GPU encoder is a host process (no NVIDIA
container runtime, ENV).

## 20. Increments (after approval)

1. Storage, specs/profiles, intake with gap protocol, intents, publisher with
   holds, reconciler, CPU encoder batch 1; crawler2 loop test on synthetic media.
2. Typed sources, decode/timestamps/colour policy, preprocess/v1, batching,
   commit protocol, sweeper, due index, golden tests; S1/S2 test.
3. GPU lease, calibration, OOM classes, VRAM metrics (needs NVML fix);
   precision and throughput benchmarks (need data).
4. Targets, backfill, retirement.
5. Simulated two-host, crash injection, soak, coverage, validation/exit docs.

## 21. Invariant check (B.4, B.5)

| Invariant | Design |
|---|---|
| B.4 #1 no authoritative local state | Scylla + MinIO; scratch per attempt; Redis rebuildable |
| B.4 #3 one time source | Redis `TIME` for all leases |
| B.4 #4 worker identity | on attempts, artifacts, events |
| B.4 #5 idempotent consumers | §6.10, §8 |
| B.4 #6 per-host roles | roles + device config |
| B.4 #7 keyspace by RF | migrator parameter; tablets disabled |
| B.4 #8 two-host profile | simulated (§15.11); real in P14 |
| B.5 #1 crawler independent | no group on crawler2 streams; P8 unchanged |
| B.5 #2 service-owned data | own keyspace/buckets; events only |
| B.5 #3 derived state rebuildable | queue from `encode_due`; indexes from authoritative rows; P10 index from F2 |
| B.5 #4 no all-to-all | no matching in P9 |
| B.5 #5 contracts | no P1 change; existing enums/fields only |

## 22. Implementation notes, narrowings and deviations (2026-10-10)

The approved design above is unchanged. This section records where the
implementation (fingerprinter repo, package `fingerprinter/`) narrows or
concretizes it, and every point that still needs an operator decision. Status
per requirement: [traceability.md](traceability.md); gates: [exit.md](exit.md).

| # | Topic | Implementation | Relation to the design |
|---|---|---|---|
| N-1 | Dead attempt with an intent but no row (§8.3, §8.4) | the reconciler deletes the intent but leaves the object to the orphan sweeper (`orphan_grace_s` > `attempt_timeout_s`) | **safer narrowing**: a paused stale worker that later wins its uncontested insert can never reference an object the reconciler already deleted |
| N-2 | Publisher role (§2, §9) | publication is done inline by the committing worker and, for held/failed publications, by the reconciler's intent pass; `fingerprinter-publisher` provides `status`, `register-consumer`, `republish` | roles merged; behaviour as designed |
| N-3 | CLIs (§18) | `python -m fingerprinter <tool> <cmd>` (e.g. `python -m fingerprinter intake accept-gap …`) | naming only |
| N-4 | Canonical numbers (§3.2) | the document is validated against a strict schema and re-serialized: a float field given as `5` becomes `5.0`; a float/string where an int is required is refused | concretizes "shortest round-trip form" |
| N-5 | Source failures (§7.2, §7.4) | within one attempt the worker tries every usable source (internal first, then untried external); a non-retryable source failure marks only that source bad and does not count as an attributed failure while another usable source remains | concretizes "untried sources first" |
| N-6 | Re-publication on FAILED (§6.10) | a redelivered (already seen) `event_id` is a no-op in every state; only a **new** `event_id` re-publishes the stored `encode.failed`/`representation.ready` | concretizes "otherwise" |
| N-7 | Target request key (§10) | if the content's `default` request already resolves to an older default spec, the target manager requests its spec explicitly (`spec_key = digest`) so the binding completes without waiting for a backfill | fixes a gap found by test (default changed before registration) |
| N-8 | Simulated two-host (§15.11) | two (or three) separate **host processes** with their own `host_id`, scratch and process space; no CPU container | a container with another ffmpeg/torch build cannot match `rt-1` and is refused (ADR-027); real machines remain P14 |
| N-9 | Redis target lease `fp:lease:target:*` (§4.3) | not used: target facts are protected by LWTs (`targets IF NOT EXISTS`, binding `IF state = 'PENDING'`); concurrent same-bytes and same-`target_id` registrations are tested (TR-7) | avoidance-only lock omitted |
| N-10 | Calibration (§12.3) | `throughput/v1` (operator decision OF-2): measured selection over memory-eligible B, report in `encoder_hosts.calibration` (column added idempotently by `migrate`) | design text §12.3 updated 2026-10-11 |
| N-14 | Truncated input (§3.2a, §7.4) | spec `dinov2-seg/2` with `decode.truncation = explicit-indicators/v1`; `dinov2-seg/1` unchanged | operator decision OF-1; design text added 2026-10-11 |
| N-11 | `encode_attempt_log` (§4.1) | one row per attempt (outcome, host, device, duration; 90-day TTL, never read for scheduling) | as designed |
| N-12 | Metrics (§12.7) | Prometheus counters/gauges; `/metrics` served by long-running `run` commands when `FINGERPRINTER_METRICS_PORT` is set; alert rules are not provided/validated | partial (DoD-5) |
| N-13 | Controlled exits (§12.1, §12.4) | a claim that lands after a controlled exit/drain began is released at once; a per-attempt lock makes worker and main-thread releases idempotent and complete before the process exits | concretizes "release each in-flight attempt" (two races found by tests, validation §4) |
| N-15 | Storage and decoder failure typing (§7.4, §12.4) — 2026-10-12 | Scylla `ReadFailure`/`WriteFailure` are storage-unavailable (an LWT `WriteFailure` is an unknown outcome, like a timeout); an unexpected exception in the claim path is a controlled exit 75 (`claim_crashed`), never a dead worker thread; a decoder/prober ended by a signal is a retryable `internal_error`, and any decode error raised while the encoder drains or stops is a cancellation (released) | defects X-8, X-9 found by SOAK-1000 (validation §7.7); concretizes "typed errors, no silent degradation" and §7.4 (decode_error = a property of the media) |
| N-16 | Truncation limits (§3.2a) — 2026-10-12 | SOAK-1000 found an H.264 MP4 (faststart) cut to ⅓ of its bytes that ffmpeg 6.1.1 reports only as `Packet corrupt` (no `partial file`): accepted, as §3.2a prescribes for corrupt-packet reports. §3.2a's "cut inside the last sample only" understates which MP4 cuts go undetected | operator decision OF-5 (a), 2026-10-12 (decisions §5b): policy kept, this is a documented limit; the SOAK-1000 corrupt set is verified against the decoder at build time; approved text unchanged |

**Findings OF-1…OF-3 — decided by the operator on 2026-10-11 (decisions
§0a) and implemented (N-10, N-14):**

- **OF-1 — truncated media.** Found: a container truncated mid-stream that
  ffmpeg decodes to EOF with exit 0 ("File ended prematurely") yielded a
  representation of the decodable prefix under `dinov2-seg/1`; the soak
  harness reported it as a criterion-8 failure. **Decision:** explicitly
  detected truncation is `decode_error`, as a new spec version → §3.2a.
- **OF-2 — batch calibration.** Found: on the RTX 2050, CUDA FP32 throughput
  is ≈ 40 frames/s at B ≤ 8 but ≈ 27 frames/s at B ≥ 16, so
  `B_start = B_max/2` picked a slower batch. **Decision:** select by best
  repeatable measured throughput among memory-eligible B → §12.3.
- **OF-3 — G-5 margin.** Found: synthetic inference-stage ratio 5.7× at
  B = 8, 4.0× at B = 32. **Decision:** the 5× FP32 gate and its C-PREC
  methodology are unchanged → §13.3.
