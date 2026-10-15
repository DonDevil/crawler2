# P9 Decisions, risks and open questions

Audit: [audit.md](audit.md). Design (source of truth): [design.md](design.md).
Traceability: [traceability.md](traceability.md).

Status vocabulary (as P8): **FOLLOWS** (restates frozen semantics),
**PROPOSED** (awaits the operator at the B.1 review), **APPROVED**,
**DEFERRED**. Approving the design is not a pass of any gate.

**Status 2026-10-10.** Revision 2 revised P9-D3, D4, D6, D7, D10, D13, D14,
D16, D17 and added P9-D20…P9-D27 to resolve review issues A–I (map: audit.md
§11). **Operator decisions (2026-10-10, design approved in principle):**
APPROVED — PC-3, P9-D2, P9-D3 (precision policy as given by the operator),
P9-D17, P9-D18; P9-D4's colour policy approved in principle; data
acquisition (Q3), driver fix before GPU validation (Q4) and simulated
two-host (Q5) answered. All other decisions remain **PROPOSED** pending
final design approval. Nothing is implemented and no implementation or
validation gate has passed.

**Final review (2026-10-10):** five specification gaps were corrected in the
design without changing the architecture or any approved decision: durable
attempt-lease renewal (D13), attempt accounting with a poison-item bound
(D9), reopen only on a new `event_id` (D8), target binding on reuse (D14),
runtime-profile device variants (D26). These decisions stay PROPOSED.
A readiness review (same day) corrected four more specification gaps:
target upload to a content-addressed key (D14), `encode.failed` only for
requests with a real `encode.requested` event and monotonic `attempt` across
reopen episodes (D9, D8), the attempt wall-clock limit (D9), and an
observable soak orphan criterion (D25). Still PROPOSED.

## 0. Final design approval (operator, 2026-10-10)

The operator approved the P9 design and **all** recommendations, trade-offs
and limitations of the final design readiness review, including the review's
clarifications and qualifications: PC-3 and P9-D1…P9-D27 (D2, D3, D17, D18
were already approved; D4's colour policy, approved in principle earlier,
is approved with the rest of D4). Implementation of P9 within the approved
scope is authorized; frozen contracts, later phases, commits and pushes are
not. Approval is a governance state: it is **not** evidence that anything is
implemented, tested or measured. Implementation and gate status are tracked
in [traceability.md](traceability.md) and [exit.md](exit.md).

The four trade-offs that were still open before final approval, as approved:

| Decision | Approved trade-off | Consequence that stays documented |
|---|---|---|
| D6/D27 intake without a consumer group | continue the group-less reader; detect and record gaps; loss of unrecoverable requests is visible (gap record, metric, non-zero `status`) and closes only by an explicit, audited operator `accept-gap`; an accepted gap is never reported as recovered | an accepted gap can **permanently** lose `encode.requested` events until a future crawler2 mechanism re-issues them (P8-D7 FROZEN: crawler2 never re-requests) |
| D11 candidate-media retention | candidate bytes are not kept as a general archive; backfill re-acquires; a vanished or changed source is reported per content in the backfill report and is never counted as processed | later reprocessing/backfill coverage is bounded by source availability; the old representation is kept |
| D12 dependencies | the approved dependency set, installed into the existing fingerprinter environment with every pre-existing package held at its installed version (constraints file), and a pinned lock file; versions recorded in runtime profile `rt-1` | any deviation must be justified, documented and tested |
| D25 observable soak gate | controlled exits (75/70/73) are allowed only when handled and reported; the gate requires **zero uncontrolled terminations**, every eligible request recovered to a terminal recorded state, and all controlled exits, recoveries and outstanding work recorded | the soak gate is not reported as passed unless every criterion of design §13.3 passes on the real corpus |

## 0a. Operator decisions on the open findings (operator, 2026-10-11)

The operator reviewed the implementation and validation report and decided
the three open findings of design §22. The decisions authorize the corrective
implementation only (no P9 rewrite, no scope change, no later-phase work, no
commits or pushes). Like §0, a decision is governance, not evidence: status of
implementation, tests and measurements is in [traceability.md](traceability.md),
[validation.md](validation.md) and [exit.md](exit.md).

| Finding | Approved decision | Constraints given with it |
|---|---|---|
| **OF-1** truncated media | Explicitly detected truncated media must fail with `decode_error`, even when ffmpeg exits 0 after decoding the available prefix | distinguish explicit truncation indicators from benign warnings; keep the existing `decode_error` cases (ffmpeg error exit, zero frames, non-monotonic PTS); a **new representation spec version** for the changed acceptance behaviour — the existing spec's identity and behaviour are not changed silently and its `RepresentationId` is not reused; document coexistence and the approved backfill path, no automatic unbounded backfill; do not claim that every truncation is detectable |
| **OF-2** GPU batch calibration | Replace `B_start = B_max/2` by the batch with the **best repeatable measured throughput** among the eligible batch sizes that fit in GPU memory | measure in the actual environment (no hard-coded B = 8); robust to single anomalous measurements, variability recorded; bounded cost; record batch, throughput, VRAM and runtime/hardware profile; keep OOM halving, typed failure classes and controlled-exit semantics |
| **OF-3** G-5 GPU throughput gate | **Unchanged:** GPU FP32 inference ≥ 5× CPU FP32 inference on the same host and the same preprocessed inputs (C-PREC) | threshold and methodology fixed before measuring; synthetic results never substitute C-PREC; FP16 never satisfies the FP32 gate while gated; a valid C-PREC result < 5× is FAIL and investigated; end-to-end reported separately from inference-only |

Implementation of these decisions: design §3.2a (spec `dinov2-seg/2`),
§7.4 (truncation indicators), §12.3 (throughput calibration) and §22.

## 1. Decisions

Columns: decision; reason and trade-off; depends on; validation (test IDs
from design §15); status.

| # | Decision | Reason / trade-off | Depends on | Validation | Status |
|---|---|---|---|---|---|
| P9-D1 | **Scope:** new V2 package in the fingerprinter repo (intake, registry, encoders, targets, specs/profiles, backfill, publisher, reconciler, CLIs). V1 job model, `worker/matching_handler.py`, Redis target registry and filesystem caches are not extended and stay unwired until P10. No matching, index or `match.found` in P9 | plan §P9/§P10; keeps V1 runnable as reference; trade-off: two code paths in one repo until P10 | plan | review | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D2 | **Identity = P1 `RepresentationId.for_content(content_id, spec)`**, `config_digest` = full SHA-256 of the canonical spec document (design §3.2), which includes the precision (`inference.compute_dtype`). `media_version`, source locator and source type are provenance only (PC-3) | same bytes reuse one representation per spec (S1/S2, P8 dedup); different bytes or a different precision always give a different identity | P1 FROZEN, P8-D5, PC-3 | TI-1…TI-4, TI-7, TP-5, TR-1 | **APPROVED** (operator, 2026-10-10) |
| P9-D3 | **Precision policy:** (1) precision is part of representation identity; (2) FP32 is the default; (3) FP16 and FP32 are distinct specs with distinct `RepresentationId`s; (4) an FP16 spec stays `gated` (not claimable, not default) until it passes the precision gate (alignment exact, per-segment cosine ≥ 0.999 vs CPU FP32) and the labelled-decision gate (identical decisions with the frozen V1 matcher); only then may the operator enable it as an option or default; (5) passing the gates does not merge the identities — FP32 representations are never relabelled; switching to FP16 means new FP16 representations via backfill. The FP16 spec is GPU-only | no FP16 evidence exists; a passed gate shows equivalence for decisions, not identical vectors; trade-off: switching to FP16 later costs a backfill | D2, D26, data (C-PREC, L-PAIRS), GPU | TP-1…TP-6 | **APPROVED** (operator, 2026-10-10); gates not run |
| P9-D4 | **Spec `dinov2-seg/1`** (design §3.2–§3.4): sequential full decode, no seek, `genpts`, `copyts`, passthrough; frame = first decoded frame entering each new 5 s window relative to the first PTS; real PTS stored (int64 + time_base); absent windows stay absent; non-monotonic PTS → `decode_error`. **Colour policy (chosen):** stream tags, else swscale default of the pinned `rt-1` ffmpeg, explicit `sws_flags=bicubic+accurate_rnd+full_chroma_int`, no tone mapping, pinned by golden fixtures (BT.709 not forced). Explicit Pillow preprocessing `preprocess/v1`; CLS; L2 in fp32; `fp-seg-npz/v1` float32. `segment_duration_s = 5.0` kept (unvalidated; P10 may replace it via a new spec + backfill). V1 cache entries not imported | closes hidden inputs H-1…H-4; smallest reproducible colour policy; trade-off: HDR looks washed out (documented), full decode is slow for long HD media | D26 | TI-3, TI-6 | **APPROVED** (colour policy in principle earlier on 2026-10-10; whole decision at final approval, 2026-10-10); truncation handling **amended by OF-1** (§0a, 2026-10-11): new spec `dinov2-seg/2` |
| P9-D5 | **Default spec semantics:** absent `required_spec` = fingerprinter default at intake, resolved once and stored; `representation.ready` carries the concrete spec; `encode.failed.spec` echoes the request's `required_spec` so crawler2's `default` row shows FAILED | keeps crawler2's FROZEN "any ready suppresses" rule correct | P8 FROZEN | TR-1, integration "failure lands on default" | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D6 | **Intake without a consumer group** on `events:encode.requested.v1`; one leased reader (Scylla `intake_state`, epoch LWT, CAS cursor). Gap handling per **P9-D27** | a group could stall crawler2's whole relay (ADR-022) → B.5 #1; trade-off: losses possible if crawler2 trims unread entries, handled by D27 | ADR-022 FROZEN | TG-1…TG-4 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D7 | **Publisher** applies ADR-022's rules (consumer-aware trim, byte budget) to the fingerprinter's own streams, publishes stored envelopes (pre-allocated `event_id`), acknowledges on the fact row; holds per **P9-D22**; retention per **P9-D21** | P8 handoff (exit §7); a lagging crawler2 group delays, never loses | D20–D22 | TE-1…TE-7 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D8 | **Re-request semantics:** same key = same work; a new `event_id` adds a source; FAILED is reopened only by a **new** `event_id` with an untried locator or arriving after an elapsed retryable failure (redeliveries never reopen; a reopen starts a new episode: `failures` reset, claim bound re-based, `claims` monotonic); re-publishes READY/FAILED. crawler2 sends no re-requests (P8 FROZEN); recovery after failure = `fingerprinter-encode retry` | resolves the P8-D7 conflict without contract change | P8-D7 | design §6.10 cases in TG/TR tests | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D9 | **Retry policy:** backoff `10 min × 3ᵏ`, `max_attempts` 4 attributed failures, untried sources first; attempt accounting design §7.2: `claims` counted at claim, `failures` only for attributed failures (released/abandoned attempts do not count); FAILED also at `claims ≥ max_claims` (10, `claim_budget_exhausted`, bounds poison items); attempt wall-clock limit `attempt_timeout_s` (proposed 4 h) → `timeout`; **one** `encode.failed` per request episode at FAILED (via intent, `request_id` = first `encode.requested` event id, `attempt` = monotonic `claims`), and **none** for requests without an `encode.requested` event (target-only/backfill-only), mapping design §7.4 | bounded work; one failure event per request | D20 | TS-*, TX-7, TX-10, TX-11, TE-8 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D10 | **Acquisition = P8 semantics** for external sources: identity bytes, raw read, no content decoding, Referer (`strict-origin-when-cross-origin`), SSRF guard on every hop, 8 GiB, SHA-256 = requested digest, mismatch → `source_unreachable`/non-retryable/`content_mismatch` (no new enum). Typed sources per **P9-D24**. V1's 100 MiB limit and decoded hashing are not used | ContentId must match P8 bytes exactly | P8-D5 FROZEN, D24 | TS-1…TS-6 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D11 | **Candidate media bytes are not retained**; target reference media are (MinIO). Backfill re-acquires candidates; vanished sources keep the old representation and are reported | bounded storage on the USB HDD; trade-off: backfill coverage gaps | D15 | TR-5 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D12 | **Storage ownership:** keyspace `fingerprinter` (tables design §4.1, own migrator, tablets off), buckets `fingerprinter-representations` (no lifecycle rule) and `fingerprinter-targets`, Redis `fp:` coordination only, local disk scratch only. Dependencies added at implementation: scylla-driver, minio, pydantic, prometheus-client, antipiracy-contracts (pinned), pytest-cov, lock file for `rt-1` | B.4/B.5; plan ownership | ADR-002/003/014 | TH-7, review | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D13 | **First committer wins by Scylla, not Redis:** attempt-unique object keys; `representations … IF NOT EXISTS` = the authoritative commit point; request transitions fenced by `attempt_id`; durable attempt lease `lease_until` (600 s) renewed every 120 s `IF attempt_id = ?` with due-entry rewrite, a failed renewal stops the attempt; Redis leases only avoid duplicate work; no UPDATE path for artifact columns. Commit follow-ups and recovery per **P9-D20** | Redis locks have no fencing (audit A-6); stale winners are valid results; trade-off: duplicate GPU work possible on lease expiry | D20 | TC-9…TC-11, TD-7, TD-8, TH-3 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D14 | **Target manager** in Scylla + MinIO; reference media are hashed locally, staged at `staging/<upload_id>`, re-verified, promoted by server-side copy to the content-addressed `ref/<sha256>` (design §10; crash recovery by a `target_register` intent and the sweeper; purge only when unreferenced), get a `full-bytes/v1` ContentId, an `internal_object` source and the same encode path; existing representations of the same ContentId reused, with the target manager binding READY itself in that case (race-free: `targets_by_content` written before its SERIAL read; reconciler backstop); binding `RepresentationId.for_target(…)`; `target.registered`/`retired` are durable facts **held** until consumers register (P9-D22); idempotent re-registration; `blast:v1` not migrated | one representation infrastructure; no target events lost before P7/P10 consumers exist | D22, D24 | TR-4, TR-7, TE-3/TE-4 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D15 | **Spec lifecycle and backfill:** `spec add` → bounded, checkpointed, idempotent backfill (priority 10, `max_in_flight`, pause/resume/cancel, yields to live work) → explicit `set-default` → `retire` only when unused (P10 guard) after grace; never destructive | coexistence of specs; controlled re-encoding | D11, D23 | TR-5, TR-6 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D16 | **Encoder process:** one GPU owner per host (`fp:gpu:<host_id>`, PX 30 s, renew 10 s, CAS — proposed values); lease loss → stop claiming, release attempts, exit 75; SIGTERM → drain and release; separate claim/decode/inference concurrency; bounded queue; calibration `B_start = B_max/2`; halve on batch OOM with the same items; failure classes per **P9-D25**; no runtime device switch; CPU fallback is FP32 and never reported as GPU | 4 GB VRAM; laptop GPU shared with the display | D25, ENV (NVML) | TX-1…TX-9 | **APPROVED** (operator final approval, 2026-10-10); calibration rule **amended by OF-2** (§0a, 2026-10-11): `throughput/v1` replaces `B_start = B_max/2` |
| P9-D17 | **GPU gate:** GPU inference-stage throughput ≥ 5× CPU inference-stage throughput, same host, same preprocessed inputs, CPU fallback on all cores; measured for the FP32 default spec (an FP16 spec counts only once enabled under D3, and is reported as such); **end-to-end speedup is measured and reported separately with no threshold** until a valid real-media benchmark exists | CPU decode dominates long HD media (audit R-6) | D3, ENV (NVML), data | B-GPU-a, B-GPU-b (design §13) | **APPROVED** (operator, 2026-10-10); not measured |
| P9-D18 | **Two-host validation in P9 = simulated** on one machine (host GPU process + CPU container); real multi-machine validation stays in P14 | B.4 #8; plan Part F #2 | — | TH-1…TH-8 | **APPROVED** (operator, 2026-10-10) |
| P9-D19 | **crawler2 adapter = cross-service tests only** (P8 requester and `RepresentationLoop` already exist); no `match.found` consumer (catalog: P11/P12) | no P8 change | P8 FROZEN | TR-1…TR-3 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D20 | **Intents and reconciliation (issue B):** a durable intent (no TTL) is written before every fact LWT; the representation LWT is the only commit point; follow-ups (index, request, target binding, publication) are idempotent, flagged on the authoritative row and completed by the worker or by the reconciler from the row; a daily full scan repairs anything an intent could miss; the orphan sweeper runs after reconciliation and checks only the authoritative row, open intents and active attempts | crash at any boundary recoverable (design §8.3); trade-off: one extra write per commit and a reconciler process | D13, D21 | TC-1…TC-14 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D21 | **Durable event retention (issue C):** no TTL on intents or unpublished events; removal only after the ack is stored on the fact row; stored envelopes kept on fact rows for republish; backlog alerts 15 min / 1 h | unpublished work cannot expire | D20 | TE-1, TE-2 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D22 | **Stream registry and holds (issue D):** each fingerprinter stream has `required_groups` and a hold flag; nothing is published before required consumers exist; representation streams require `media-registry`; target streams are held until a future consumer creates its group and the operator runs `register-consumer`; `republish` bootstraps late consumers from Scylla facts; XADD ≠ downstream processing | Redis trimming can never remove the only copy of an event; no consumer is invented | D7, D14 | TE-3…TE-7 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D23 | **Due index without TTL (issue E):** `encode_requests` authoritative; `encode_due` + `encode_due_days` without TTL; new entry before transition, old after; terminal entries removed; daily full-scan repair; Redis queue rebuilt from it. TTL only on `encode_attempt_log` (observability) | active/delayed work always discoverable | D20 | TD-1…TD-6 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D24 | **Typed sources (issue F):** `external_http` (from events only; HTTP path, SSRF, identity bytes, 8 GiB) vs `internal_object` (target manager only; allowlisted bucket `fingerprinter-targets`, key `ref/<sha256>`, MinIO SDK read, never HTTP); both verified against ContentId; sources are provenance | an internal object must not go through internet acquisition, and an event must not be able to reference internal storage | D10, D14 | TS-1…TS-9 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D25 | **Failure classes and soak gate (issue G):** batch OOM recovered; single-item OOM → retryable attempt + controlled exit 75 + restart; device error → exit 70, no CPU switch; crash and OOM-killer = uncontrolled. Soak gate observable (design §13.3): zero uncontrolled terminations, zero lost/stuck requests, correct retryable states, leases released/expired, bounded RSS/VRAM, zero orphans/open intents after drain (checked with a recorded, shortened soak `orphan_grace_s`), recovered/single-item OOMs and controlled exits reported | removes the contradiction between "exit on OOM" and "no OOM crash"; does not claim every input fits in VRAM | D16 | TX-6…TX-8, soak | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D26 | **Runtime profiles, fail closed (issue H):** the origin profile `rt-1` (common components: exact ffmpeg build, Python, torch, transformers, numpy, Pillow, model file hashes; device variants `cpu` and `cuda` (CUDA runtime/cuDNN); NVIDIA driver = provenance + startup golden check) is in the spec digest; an encoder matches only if the common components and its device variant match; the `cuda` variant is admitted after the CUDA fp32 golden check; `spec_runtime_compat` lists profiles allowed to produce a spec; an encoder whose fingerprint is not allowed refuses the spec; a new profile is admitted only by an explicit approval after the runtime compatibility gate (exact indices/PTS, bitwise RGB, cosine ≥ 0.9999); otherwise a new spec + backfill. Determinism levels design §3.6 | runtime changes cannot silently alter a spec; trade-off: upgrades require a gate run | D4 | TI-5, TI-6, TI-8 | **APPROVED** (operator final approval, 2026-10-10) |
| P9-D27 | **Intake gap protocol (issue A):** stream IDs compared as `(ms, seq)` pairs only; gap detection by counts (`entries-added − length > consumed_ordinal`, plus `max-deleted-entry-id > cursor`), never by ID arithmetic; the reader records a gap and continues (halting would enlarge the loss); gap records `OPEN → REPLAY_REQUESTED → REPLAY_OBSERVING → CLOSED_REPLAYED`, or `REPLAY_INCOMPLETE`/`REPLAY_UNAVAILABLE`, or `CLOSED_ACCEPTED` only by explicit `accept-gap` with audit; repair by crawler2's existing `relay --replay-from/--replay-to` (new IDs at the tail); `HALTED` only for stream reset or an uninitialized cursor; explicit `init --from-start\|--from-now` | exact detection on non-contiguous IDs; lossless when the replay window covers the missing rows; trade-off: the fingerprinter cannot enumerate missed event IDs — the window is the evidence; replay re-publishes all crawler2 event types | D6, ADR-013/022 FROZEN, Redis ≥ 7.0 | TG-5…TG-14 | **APPROVED** (operator final approval, 2026-10-10) |

## 2. Phase-plan correction

| # | Plan text before | Proposed text | Why | Status |
|---|---|---|---|---|
| PC-3 | "Representation registry keyed by `(content_identity, media_version, model_name, model_version, sampling_config_hash)`" | "Representation registry keyed by P1's `RepresentationId` = `(ContentId, spec name, spec version, config_digest)`; `config_digest` is the SHA-256 of the canonical spec document covering model name and immutable revision, runtime profile, decode, timestamps, sampling, colour, preprocessing, normalization, precision and artifact format; the media version and source locator are provenance (new bytes ⇒ new `ContentId`)" | `media_version` in the key would give byte-identical content several representations (breaks P8 dedup, the S1/S2 gate and P1's derivation); the plan tuple omits inputs that change vectors (audit H-1…H-6) | **APPROVED** (operator, 2026-10-10); applied to the plan |

PC-3 was approved on 2026-10-10 and is applied to the plan text.

## 3. Alternatives considered

| Topic | Alternative | Why not |
|---|---|---|
| Intake | consumer group with fast intake | a long fingerprinter outage stalls crawler2's relay (ADR-022) — B.5 #1 |
| Intake | per-stream backpressure in crawler2's relay | changes FROZEN P2/ADR-022 behaviour |
| Gap detection | compare first retained ID with "cursor + 1" | stream IDs are not contiguous (review issue A) |
| Gap reaction | halt the reader on a gap | crawler2 keeps trimming the ungrouped stream; halting enlarges the loss |
| Gap closure | automatic accept after a timeout | silent loss; forbidden |
| Commit | deterministic object key, overwrite allowed | concurrent attempts would write different (equivalent) bytes to one key |
| Commit recovery | index/event writes after the LWT without intents | a crash between them leaves undiscoverable work (review issue B) |
| Event retention | TTL on pending events | unpublished work could expire (review issue C) |
| Target events | publish to an ungrouped stream before consumers exist | trimming can destroy the only copy (review issue D) |
| Due index | TTL on index entries | active work could become undiscoverable (review issue E) |
| Sources | MinIO presigned URLs through the HTTP acquirer | internal storage behind the SSRF guard would be refused or would need an exemption that events could abuse (review issue F) |
| Identity | device (CPU vs CUDA) in every spec | CPU and GPU hosts could never share FP32 representations (precision, unlike device, *is* in the identity: D3) |
| Identity | merge FP16 into the FP32 identity after a passed gate | equivalence of decisions is not identity of vectors; would mix artifacts of different numerics under one ID (operator policy D3) |
| Runtime | record versions as provenance only | does not prevent silent semantic change (review issue H) |
| Colour | force BT.709 for HD | a choice the content does not state; tags + pinned default is smaller and testable |
| Sampling | keyframe-only / per-segment seek | positions depend on the encoder's GOP; little saving at 5 s |
| Media retention | keep candidate bytes | unbounded storage on the USB HDD |
| Batch policy | additive increase after stability | oscillation on a GPU shared with the display |
| Preprocessing | keep `AutoImageProcessor` | its backend changed (TorchvisionBackend) without any recorded signal |

## 4. Open questions for the operator

| # | Question | Status |
|---|---|---|
| Q1 | Approve the remaining PROPOSED decisions (P9-D1, D4 except colour, D5…D16, D19…D27) | **answered 2026-10-10:** all approved with the readiness review's recommendations and qualifications (§0) |
| Q2 | GPU gate scope (P9-D17) | **answered 2026-10-10:** inference stage ≥ 5×; end-to-end reported, no threshold (D17 APPROVED) |
| Q3 | Test data | **answered 2026-10-10:** openly licensed films may be acquired after licence verification with a manifest (source, licence, checksum, date); `~/Videos/Blast.mp4` local-only — never committed, uploaded or transmitted |
| Q4 | NVML mismatch | **answered 2026-10-10:** must be resolved before GPU validation (operator action; not done) |
| Q5 | Two-host | **answered 2026-10-10:** simulated two-host for P9; real multi-machine in P14 (D18 APPROVED) |
| Q6 | Colour policy | **answered 2026-10-10:** accepted in principle (D4) |

## 5. Dependencies and follow-ups

- **P8 / P1 (FROZEN):** `full-bytes/v1` ContentIds, one `default` request per
  content, `media-registry` group, no re-requests, all events/IDs/enums as
  they are.
- **ADR-013 / ADR-022 (FROZEN):** outbox retention 14 d and `relay
  --replay-from`; stream rules avoided by the intake and applied by the
  publisher.
- **Redis ≥ 7.0** (`entries-added`, `max-deleted-entry-id`): deployed 7.4.2.
- **P10:** registers the `matcher` group and `retire_guard` specs; owns
  thresholds, index, `match.found`, equivalence, segment duration changes.
- **P7 follow-up / P10:** first consumers of target events (registration
  releases held events).
- **P11/P12:** `match.found` consumers in crawler2.
- **P14:** real multi-machine validation; optional GPU container runtime.
- **ADRs required after approval — written 2026-10-10 after final approval:**
  [ADR-025](../../adr/ADR-025-groupless-intake-and-gap-protocol.md) group-less
  intake and gap protocol (P9-D6/D27);
  [ADR-026](../../adr/ADR-026-durable-intents-and-held-publication.md) durable
  intents, held publication and stream registry (P9-D13, D20…D22);
  [ADR-027](../../adr/ADR-027-runtime-profiles-fail-closed.md) runtime profiles
  and fail-closed compatibility (P9-D2, D3, D26).

## 5a. Open findings from implementation

OF-1 truncated media, OF-2 batch calibration and OF-3 the G-5 margin were
raised in design §22 and **decided by the operator on 2026-10-11** (§0a).
None is open any more as a decision; their implementation and measurement
status is in [traceability.md](traceability.md) and [exit.md](exit.md).

Findings from the real-media validation (2026-10-12; evidence in
[validation.md](validation.md) §7). Raised as open; **both decided by the
operator on 2026-10-12** (§5b). The table keeps the finding as raised.

| # | Finding | Options |
|---|---|---|
| OF-4 | **G-3 scope.** `precision.py` compares the whole V1-oracle result (decision, both alignment ranges, matched-segment count) for every L-PAIRS pair. FP16 and FP32 agree on every decision (480/480) and on the alignment of all 37 matches, but one *non-match* differs in its best partial alignment (2 vs 1 segments). Design §13.3 says "identical match/no-match decisions and matched-segment alignment"; whether alignment of a non-match counts is not stated | (a) keep the strict reading → G-3 FAIL; (b) define alignment as applying to matched pairs only → G-3 would pass on this run. FP16 stays `gated` either way (G-2 failed on C-PREC) |
| OF-5 | **Undetected MP4 truncation.** SOAK-1000 item `corrupt/c-04-h264.mp4` (H.264 MP4, faststart, stream copy, truncated to ⅓ of its bytes) produces only `Packet corrupt` / NAL errors with ffmpeg exit 0 — no `partial file` line — so `dinov2-seg/2` accepts the prefix (READY), exactly as design §3.2a prescribes for corrupt-packet reports. The corpus generator had labelled it an expected failure; design §3.2a's limits ("an MP4 whose cut falls inside the last sample only") understate the gap: a ⅓ cut can yield the same report | (a) keep `explicit-indicators/v1`; document the wider gap; rebuild the corrupt set with build-time verification that each item is an expected failure under the approved policy, and rerun SOAK-1000; (b) treat corrupt-packet reports at end of input as truncation → a new spec version (`dinov2-seg/3`, like OF-1), then rerun; (c) accept criterion 8 with this item as a documented exception (gate wording would change) |

## 5b. Operator decisions OF-4, OF-5 (2026-10-12)

**OF-4 — option (b), G-3 alignment applies to matched pairs.** Design
§13.3 TP-3 ("identical match/no-match decisions and matched-segment
alignment") is interpreted as: every match/no-match decision must be
identical between the FP16 candidate and CPU FP32, and alignment (both
ranges and the matched-segment count) must be identical for **every pair
classified as a match**. A partial-alignment difference on a pair that both
precisions classify as a non-match is reported as a diagnostic and does not
fail G-3. Labelled data (L-PAIRS) and matcher thresholds (`MatcherConfig`)
are unchanged. Implemented in `benchmarks/p9-encoder/precision.py`
(`tp3_compare`), test `tests/v2/test_precision_tp3.py`. **This does not
ungate FP16:** G-2 failed on C-PREC, so the FP16 spec stays `gated`, is not
the default, and keeps its own representation identity (design §3.7).

**OF-5 — option (a), keep `explicit-indicators/v1`.** The truncation policy,
`dinov2-seg/2`'s identity and behaviour, representation digests, criterion 8
and its thresholds are unchanged. The SOAK-1000 corrupt-media set is
corrected instead: every item labelled as an expected failure is verified at
build time by the production decoder (`fingerprinter.media` probe + decode,
`dinov2-seg/2`, explicit-indicators/v1, on the pinned ffmpeg) and the corpus
is not accepted unless each fails non-retryably with its labelled class.
`corrupt/c-04-h264.mp4` (faststart MP4 cut to ⅓ — decodes READY under the
policy) is replaced by `corrupt/c-04-h264-moovless.mp4` (same source window,
plain moov-at-end MP4 cut to ⅓: an interrupted download whose sample index is
lost). It was not deleted, relabelled as valid, or exempted. SOAK-1000 run 2
stays recorded as a failed run. The gap (an MP4 cut that ffmpeg reports only
as a corrupt packet is accepted) is a documented limit of §3.2a (design §22
N-16); closing it would need a new spec version (option (b)), not chosen.

## 5c. Operator acceptance of P9 (2026-10-15)

The operator accepted P9 as **complete for the approved implementation and
validation scope, with the documented limitations** ([exit.md](exit.md) §5,
§6). These are part of the acceptance, not resolved by it:

- G-2 failed, so FP16 stays `gated`;
- soak criteria 3 and 4 were satisfied only vacuously;
- truncation detection is partial;
- real multi-machine validation is deferred to P14;
- the P8 real-stack tiers were not re-run.

Constraints given with the acceptance:

- P9 is **not** production-deployed and must not be represented as such;
- FP32 (`dinov2-seg/2`) remains the default, and FP16 stays gated;
- the approved design is not reopened.

Acceptance is governance, not new evidence. Every gate result stays as
recorded in [validation.md](validation.md).

## 6. Risk register

| # | Risk | Likelihood / impact | Mitigation |
|---|---|---|---|
| K-1 | end-to-end GPU speedup < 5× (CPU decode) | high / reporting | D17 scopes the gate to inference; parallel decode; NVDEC later as a reviewed optimisation. **Inference-stage FP32 is marginal on synthetic inputs (5.7× at B = 8, 4.0× at B = 32); OF-2 (§0a) calibrates by measured throughput; the 5× threshold is unchanged (OF-3)** |
| K-2 | FP16 fails 0.999 | medium / speed | FP32 default (D3) |
| K-3 | labelled set and soak corpus not prepared | medium / blocks gates | data-preparation §2 |
| K-4 | NVML mismatch; compositor VRAM | medium | Q4 — **mismatch resolved by 2026-10-10** (`nvidia-smi` 615.71.09 works; GPU tests ran); compositor VRAM remains; throughput calibration over memory-eligible B (OF-2, §0a); OOM classes |
| K-5 | intake gap with replay window too narrow | low / loss | default 24 h margin, operator-widened, recorded window, REPLAY_INCOMPLETE detection |
| K-6 | runtime drift | medium | D26 fail-closed |
| K-7 | USB HDD I/O slows soak | high / time | resumable windows; scratch size budget |
| K-8 | source URLs vanish before backfill | medium | report gaps; old reps kept |
| K-9 | laptop power/network interruptions | high | all work resumable from Scylla |
| K-10 | held target events accumulate until P10 | certain / small | durable, bounded by target count; `events_held` metric |
| K-11 | full-scan reconciler cost at scale | low now | rate-limited, daily; intents make it a backstop |
