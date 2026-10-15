# P9 Data sufficiency and benchmark preparation

Status (updated 2026-10-12): **the data sets now exist** — S-SRC complete
(7 films, ≥ 6 required), C-PREC, L-PAIRS and SOAK-1000 built from it with
fixed seeded commands and manifests (§7, last rows); the ≥ 5 real-media
targets of §2.5 were not built (no gate depends on them; target tests run on
synthetic media). Results: [validation.md](validation.md) §7.
2026-10-12 (operator decision OF-5): the SOAK-1000 corrupt-media set was
corrected — every expected-failure item is now verified against the
production decoder at build time, `c-04-h264.mp4` was replaced, new
manifest `ed3bfd8d…` (§7 last row; validation §8.2).
Earlier status (2026-10-11, after implementation, kept as history): data-sufficiency gate
still NOT PASSED. C-PREC, L-PAIRS and SOAK-1000 did not exist; the
licensed source set S-SRC was incomplete (§7). What was produced: a seeded
**synthetic** corpus for harness validation and synthetic benchmarks (§7), and
the benchmark/soak tooling in `fingerprinter/benchmarks/p9-encoder/`. The
NVML mismatch is resolved (GPU usable, §3). No threshold is lowered for lack
of data; every gate needing these sets is reported NOT MEASURED in
[exit.md](exit.md). Gate definitions: [design.md](design.md) §13; test IDs:
design §15.

## 1. What exists today

| Need | Assets found | Verdict |
|---|---|---|
| Synthetic videos | `fingerprinter/benchmarks/fixtures/bench_15s.mp4`, `bench_60s.mp4` (ffmpeg `testsrc2`, 320×240, 24 fps, H.264, committed); `tests/fixtures/tiny_video.mp4`; P8 corpus generator (`crawler2/benchmarks/p8-media/dedup_bench.py`) | **available**; sufficient for functional tests only |
| Real media | `~/Videos/Blast.mp4`: 8,495.6 s, HEVC 1920×800, 24 fps, untagged colour, 1.7 GiB | **available, local-only** (§6); one title is insufficient |
| Measured | synthetic CPU embedding throughput 1.80 s per minute of video (audit §7) | the only measurement; **not** a real-media or end-to-end baseline |
| Precision clip set C-PREC | — | **missing** |
| Labelled set L-PAIRS | — (none in either repo) | **missing** |
| Identical media at several URLs | fixture HTTP server pattern (P8) | **can be generated** |
| Different-content controls | P8 corpus | **available and sufficient** (distinct ContentId ⇒ distinct RepresentationId) |
| Varied codecs/resolutions/fps | ffmpeg 6.1.1: libx264, libx265, libvpx-vp9, libaom-av1, libsvtav1 | **can be generated** from real sources |
| SOAK-1000 | — | **missing** |
| Targets | V1 `blast:v1` (Redis, local file) | insufficient (need ≥ 5) |
| GPU | RTX 2050 present; `torch.cuda.is_available()` True; NVML mismatch (kernel 610.57.04 vs library 615.71) at audit time | **resolved by 2026-10-10 evening**: `nvidia-smi` reports driver 615.71.09, CUDA 13.0; GPU tests and GPU benchmarks ran |
| CPU-only environment | `CUDA_VISIBLE_DEVICES=` or any Docker container (runc only) | **available** |
| Two hosts | one machine; crawler2 stack running; no fingerprinter image yet | **simulated only** (P9-D18); real machines **deferred to P14** |

## 2. Preparation plan (acquisition permitted by the operator, 2026-10-10, Q3; not started)

Proposed targets, not completed work. All media live in
`~/anti_piracy/datasets/p9/` (outside every git repository). Only scripts,
manifests and labels are committed, in `fingerprinter/benchmarks/p9-encoder/`.
Downloaded films and large media are **never** committed.

### 2.1 Sources (S-SRC)

- ≥ 6 openly licensed films with distinct content (candidates: Blender
  Foundation open movies — Big Buck Bunny, Sintel, Tears of Steel, Elephants
  Dream, Cosmos Laundromat, Spring). Each is downloaded **only after** its
  source and licence are verified.
- Manifest `sources.json` per file: title, source URL, licence name and URL,
  attribution text, SHA-256, size, acquisition date, ffprobe summary.
- `Blast.mp4` may be used **locally only** (§6; operator decision 2026-10-10).

### 2.2 C-PREC (precision set)

≈ 60 clips × ≈ 15 min (≈ 10,800 segments at 5 s), fixed offsets, balanced over
{360p, 720p, 1080p} × {H.264, HEVC, VP9} × {24, 30, 60 fps}, plus hard cases
(dark scene, flat colour, high motion, letterbox) and the colour/timestamp
golden variants of design §3.3. Every derivation is a fixed ffmpeg command
recorded with its output SHA-256 (`c-prec.json`).

### 2.3 L-PAIRS (labelled match set, labels by construction)

Targets = source films. ≥ 150 positives (excerpts 30 s–10 min, re-encodes,
360p downscale, 10 % crop, letter/pillarbox) and ≥ 300 negatives (excerpts of
other sources; synthetic clips). Label = provenance (`l-pairs.csv`: target,
clip, label, transform). Oracle in P9: frozen V1
`matching.matcher.match_segments` with today's `MatcherConfig`.

### 2.4 SOAK-1000

1,000 videos derived from S-SRC with a seeded distribution: 60 % 10 s–3 min,
30 % 3–20 min, 9 % 20–60 min, 1 % full length; 240p–1080p;
H.264/HEVC/VP9/AV1; 24/25/30/60 fps; ≥ 20 VFR; MP4/MKV/WebM; plus 20
corrupt/truncated and 10 audio-only files (expected failure classes).
Every expected-failure item must be verified at build time: the production
decoder (probe + decode, approved spec and truncation policy, pinned ffmpeg)
must fail it non-retryably with its labelled class, or the corpus is not
accepted (OF-5, decisions §5b).
≈ 30–60 GB, ≈ 120–200 h of video; served by a local fixture HTTP server.
Run in resumable windows (laptop power/network constraint).

### 2.5 Targets

≥ 5 targets from S-SRC, one with a second version, one retired, one whose
reference bytes equal a crawled clip (reuse test TR-4).

## 3. Environment preparation

| Step | Check | Owner |
|---|---|---|
| Fix NVML mismatch (reboot or driver alignment) — **required before GPU validation (operator decision 2026-10-10); not done** | `nvidia-smi` lists the RTX 2050 | operator |
| Confirm CUDA | `env/bin/python -c "import torch; print(torch.cuda.get_device_name(0), torch.cuda.mem_get_info())"` | engineer |
| Record free VRAM with the desktop running | `mem_get_info()` | engineer |
| Capture runtime profile `rt-1` | `fingerprinter-runtime fingerprint` (to be implemented) → lock file + ffmpeg buildconf hash | engineer |
| CPU-only runs | `--device cpu` / container | engineer |

## 4. Which tests need what

| Test group (design §15) | Synthetic data | Licensed real media | Working GPU | Large corpus |
|---|---|---|---|---|
| TI identity, TG intake/gaps, TC commit, TE events, TD due index, TS sources | **sufficient** | — | — | — |
| TR-1…TR-6 reuse/backfill (S1/S2 exit gate G-1) | **sufficient** | — | — | — |
| TI-6 golden fixtures (colour/timestamps) | partly (synthetic tags/VFR) | for realistic colour cases | — | — |
| TX-1…TX-5, TX-9 lifecycle/queue | sufficient (CPU parts) | — | for GPU parts | — |
| TX-6…TX-8 OOM/device classes (G-4) | sufficient inputs | — | **required** | — |
| TP precision (G-2, G-3) | — | **required** (C-PREC, L-PAIRS) | **required** | — |
| B-CPU real baseline (G-6) | — | **required** | — | — |
| B-GPU-a/b throughput (G-5) | — | **required** | **required** | — |
| B-SOAK (G-8) | — | **required** | **required** for the GPU run | **required** (SOAK-1000) |
| TH simulated two-host (G-7) | sufficient | — | for TH-8 | — |
| Real multi-machine | — | — | — | **deferred to P14** |

## 5. Benchmark procedures (to implement in `fingerprinter/benchmarks/p9-encoder/`)

Every result JSON records CPU, RAM, GPU, driver, CUDA, runtime profile, spec
digest, batch size, threads, corpus manifest SHA-256; warm-up item
discarded; cold start separate; ≥ 3 repetitions; n ≥ 30 per percentile;
median and p95; actual device recorded (a CPU run is never labelled GPU).

| ID | Measures | Procedure |
|---|---|---|
| B-DEC | decode throughput | ffmpeg stage alone (select + rgb24 pipe) per resolution/codec on C-PREC; frames/s, segments/s |
| B-CPU | end-to-end CPU | full pipeline, CPU fallback, all cores, C-PREC from a local server; stage times; s per minute of video |
| B-CAL | batch calibration | GPU, B = 1…256; peak VRAM, throughput per B |
| B-GPU-a | **inference stage** (5× gate) | same preprocessed tensors from RAM: GPU (FP32 default) vs CPU fallback inference on all cores; frames/s ratio |
| B-GPU-b | end-to-end GPU (reported, no threshold) | full pipeline; s per minute of video; GPU idle and decode-blocked time |
| B-IO | storage/network overhead | upload + commit + publication per attempt |
| B-PREC | precision | design §13.3 |
| B-OOM | OOM classes | forced batch OOM and single-item OOM (reserved VRAM); design §12.4 behaviours |
| B-START | startup | cold/warm process start → first vector |
| B-SOAK | soak gate | SOAK-1000, observable criteria of design §13.3 |
| B-KILL | crash injection on the real stack | design §15.3 boundaries with `kill -9`, Redis restart, Scylla pause |
| B-2HOST | simulated two-host | design §15.11 |

## 6. Copyright and local-only material

- `~/Videos/Blast.mp4` is **local-only test material**: never committed,
  never uploaded to any external service, never transmitted off the host.
  Storing a copy in the **local** MinIO of the dev stack (target reference)
  is local use; such data must not be exported or shared.
- Openly licensed films: download only after licence verification; record
  the manifest fields of §2.1; keep outside git.
- Derived clips inherit their source's licence/restriction; manifests record
  the parent SHA-256.

## 7. What was actually prepared (2026-10-10/11)

| Set | Location (outside git) | State | Usable for |
|---|---|---|---|
| synthetic-mini (seed 7): 60 ffmpeg test-source videos (H.264/HEVC/VP9/AV1; MP4/MKV/WebM; 240p–720p; 24–60 fps; 6 VFR), 4 corrupt/truncated, 2 audio-only; 1.1 GB | `~/anti_piracy/datasets/p9/synthetic-mini/` (`manifest.json` with SHA-256 per file; generator `benchmarks/p9-encoder/make_synthetic_corpus.py`) | complete | soak-harness validation, synthetic B-GPU-a; **not** a gate input |
| S-SRC (licensed films) | `~/anti_piracy/datasets/p9/s-src/` (`manifest.json`: source URL, licence, licence page, attribution, bytes) | **INCOMPLETE**: licences verified on the official pages for 4 films (Big Buck Bunny CC BY 3.0, Sintel CC BY 3.0, Tears of Steel CC BY 3.0, Elephants Dream CC BY 2.5); Cosmos Laundromat and Spring pages state no licence → not downloaded; downloads of Sintel/ED/BBB from download.blender.org failed repeatedly on 2026-10-10 (connection resets/timeouts). **2026-10-11:** one bounded resume attempt completed **Sintel** (1,180,090,590 B, sha256 `97f1dbc6…`) and **Elephants Dream** (152,496,338 B, sha256 `6fc295e0…`) — byte counts equal the server's length, ffprobe opens both; the publisher gives no checksum. Big Buck Bunny still partial; Tears of Steel has no file in the listing | superseded by the next row |
| S-SRC (2026-10-11, later) | same; `manifest.json` `status: COMPLETE` | **COMPLETE: 7 films**, 3,795.7 s in total. Added from Wikimedia Commons (byte count = publisher size, SHA-1 = Commons `imageinfo` SHA-1): Tears of Steel (4K VP8, CC BY 3.0), Spring (CC BY 4.0), Sprite Fright (official page: "Creative Commons Attribution 1.0"; Commons: CC BY 4.0 — attribution-only either way, discrepancy recorded), Coffee Run (CC BY 4.0), HERO (CC BY per the official YouTube upload and Commons; Blender Studio's film page states no licence — the weakest evidence of the set). Six films have licence evidence on an official Blender page, so the ≥ 6 requirement holds without HERO. Big Buck Bunny not used (download incomplete). SHA-256 of all 7 files re-verified on 2026-10-11 (`sha256sum -c`, 7/7 OK) | input of the reel and the derived gate corpora below |
| reel (2026-10-11) | `~/anti_piracy/datasets/p9/reel/` (`manifest.json`, sha256 `50de67d9…`) | complete: the 7 films normalized (1920×800, 24 fps, H.264 CRF 16, padded) and concatenated, 3,790.9 s; generator `benchmarks/p9-encoder/make_real_corpora.py reel` | source of every derived clip (clip windows recorded as reel offsets + film ids) |
| C-PREC, L-PAIRS, SOAK-1000, ≥ 5 targets | — | (as of the morning of 2026-10-11) not built — superseded by the next three rows | — |
| C-PREC (built 2026-10-11) | `~/anti_piracy/datasets/p9/c-prec/` (`manifest.json` sha256 `af5826bc…`, seed 9) | **complete**: 63 clips, 14.26 h, 5.7 GB — 54 balanced ({360p, 720p, 1080p} × {H.264, HEVC, VP9} × {24, 30, 60} fps, 2 each, 15 min at seeded reel offsets) + 9 hard/golden cases (5 min: dark, flat colour, high motion, letterbox, pillarbox, BT.601, full range, VFR, PTS offset); every output with its ffmpeg command, SHA-256, probe and contained films | G-2 (TP-1/TP-2), G-5, G-6, B-GPU-b (validation §7) |
| L-PAIRS (built 2026-10-11) | `~/anti_piracy/datasets/p9/l-pairs/` (`manifest.json` `ab3d7711…`, `l-pairs.csv` `ff43168c…`, seed 150) | **complete**: 7 targets (S-SRC originals, symlinks) + 150 film excerpts (31–562 s, 6.82 h; transforms re-encode / 360p / 10 % crop / letterbox / pillarbox; H.264/HEVC/VP9) + 30 synthetic clips; **480 pairs: 150 positives, 330 negatives** (each excerpt vs its own film = positive, vs two other films = negatives; synthetic vs a film = negative). Labels = provenance only; 0 label/provenance mismatches | G-2 (TP-1/TP-2), G-3 (TP-3) |
| SOAK-1000 (built 2026-10-12; **superseded by the next row**, used by SOAK-1000 run 2; manifest kept in `runs/soak-1000-run2/corpus-as-run/`) | `~/anti_piracy/datasets/p9/soak-1000/` (`manifest.json` `210052fe…`, seed 1000) | **complete**, but one of its 30 expected failures (`c-04-h264.mp4`) decodes READY under the approved policy (OF-5): 1,000 videos, 136.8 h, 43 GB (600 × 10 s–3 min, 300 × 3–20 min, 90 × 20–60 min, 10 full films; 240p–1080p; H.264 259 / AV1 251 / VP9 246 / HEVC 244; 24/25/30/60 fps + 24 VFR; MKV/MP4/WebM) + 20 corrupt/truncated (12 truncated to ⅓ or 90 %, 8 with garbled headers) + 10 audio-only, each with its expected outcome; non-VFR videos are keyframe-aligned stream copies of 20 seeded variant reels | G-8 (validation §7) |
| SOAK-1000 corrected (2026-10-12, OF-5) | `~/anti_piracy/datasets/p9/soak-1000/` (`manifest.json` **`ed3bfd8d…`**, seed 1000; build log `runs/build-soak-1000-of5.log`) | **complete and verified**: the same 1,029 items byte-for-byte (same composition, 136.8 h); `corrupt/c-04-h264.mp4` replaced by `corrupt/c-04-h264-moovless.mp4` (same window, moov-at-end MP4 truncated to ⅓, sha256 `e404b18e…`); **all 30 expected failures verified** against `fingerprinter.media` (`dinov2-seg/2`, explicit-indicators/v1, ffmpeg 6.1.1-3ubuntu5): 11 → `decode_error` (truncated_input), 19 → `unsupported_format`; outcome recorded per item (`verified`) | G-8 (validation §8) |
| `~/Videos/Blast.mp4` | — | not used, copied or hashed in P9 | — |

To complete the data gates: finish S-SRC (≥ 6 verified licences; resume the
downloads), derive C-PREC/L-PAIRS/SOAK-1000 per §2 with fixed commands and
manifests, then run `precision.py`, `bench_inference.py`, `bench_e2e.py` and
`soak.py` (commands: [runbook](../../architecture/fingerprinter-encoder.md) §7).
