# ADR-027 — Representation specs pin a runtime profile; encoders outside an allowed profile refuse to produce

Status: Accepted (2026-10-10, P9 final design approval, decisions P9-D2,
P9-D3, P9-D26).

## Context

DINOv2 vectors depend on more than model weights: the ffmpeg build (decode,
colour conversion), Pillow resampling, torch/transformers kernels and the
precision. A library upgrade can silently change vectors that share one
`RepresentationId`.

## Decision

1. The canonical spec document (its SHA-256 is the P1 `config_digest`) names
   its origin **runtime profile** (`rt-1`) and includes the precision, so FP16
   and FP32 are distinct specs and identities; `media_version` and locators
   are provenance only (PC-3).
2. A profile lists common components (ffmpeg build hash and library versions,
   Python, torch, transformers, numpy, Pillow, model file hashes) and device
   variants (`cpu`; `cuda` with CUDA runtime and cuDNN, admitted after the CUDA
   FP32 golden check). An encoder may produce a spec only if its fingerprint
   matches an allowed profile (`spec_runtime_compat`); otherwise it refuses
   (fail closed). The NVIDIA driver is provenance plus a startup golden check.
3. A new profile is admitted to an existing spec only by an operator action
   after the runtime-compatibility gate; otherwise a new spec + backfill.
4. An FP16 spec is `gated` (not claimable, never default) until the precision
   and labelled-decision gates pass; passing never merges identities.

## Consequences

- Upgrades cost a gate run or a backfill; silent drift is impossible.
- A CPU container with a different ffmpeg/torch build is refused for `rt-1`
  (the simulated two-host test therefore uses two host processes).
- Implementation: `spec.py`, `runtime.py`, `store.runtime_check`,
  `specs.py`; tests TI-3, TI-5, TI-6, TI-8, TP-4…TP-6.
