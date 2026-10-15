# Fingerprinter encoder (P9) — runbook

How to install, configure, run, test, benchmark and recover the P9
fingerprinter: media-centric encoder, representation registry and target
manager. Design (source of truth):
[`phases/p09-fingerprinter-encoder/design.md`](../phases/p09-fingerprinter-encoder/design.md).
Gate status: [`exit.md`](../phases/p09-fingerprinter-encoder/exit.md). ADRs:
[025](../adr/ADR-025-groupless-intake-and-gap-protocol.md),
[026](../adr/ADR-026-durable-intents-and-held-publication.md),
[027](../adr/ADR-027-runtime-profiles-fail-closed.md).

## 1. Where things are

| What | Where |
|---|---|
| Code | fingerprinter repository, package `fingerprinter/` (`~/anti_piracy/fingerprinter`). V1 packages there are not used, except `acquisition.ssrf_guard` (reused as code). |
| Local developer guide | fingerprinter `docs/v2-encoder.md` (environment, dependency files, CLI, tests, benchmarks; this runbook stays the cross-service reference) |
| Tests | fingerprinter `tests/v2/` (unit + real-stack integration + GPU + simulated two-host); crawler2 `tests/integration/fingerprinter/` (cross-service, `scripts/test-fingerprinter-p9.sh`) |
| Benchmarks | fingerprinter `benchmarks/p9-encoder/` (scripts + small result JSONs); datasets and run logs in `~/anti_piracy/datasets/p9/` (never in git) |
| Durable state | Scylla keyspace `fingerprinter` (tablets off), MinIO buckets `fingerprinter-representations`, `fingerprinter-targets`; Redis `fp:` keys (rebuildable); local scratch per attempt |
| Streams | consumes `events:encode.requested.v1` (no group); publishes `representation.ready.v1`, `encode.failed.v1` (held until `media-registry` exists), `target.registered.v1`, `target.retired.v1` (held until a consumer registers) |

## 2. Required services and installation

Shared with crawler2's compose stack: Scylla 6.2 (LWT, vnodes), Redis ≥ 7.0
(`entries-added`/`max-deleted-entry-id`; deployed 7.4.2), MinIO. ffmpeg 6.1.1
(system), Python 3.12, the DINOv2 model in the local HF cache.

```bash
cd ~/anti_piracy/fingerprinter
env/bin/pip freeze > /tmp/before.txt                      # never upgrade existing packages
env/bin/pip install -c /tmp/before.txt -r requirements-p9.txt ~/anti_piracy/crawler2/contracts
. scripts/p9-env.sh   # dev/test: points at crawler2's stack with THROWAWAY names; for a real
                      # deployment export FINGERPRINTER_* yourself (keyspace fingerprinter …)
python -m fingerprinter migrate                            # keyspace, tables, buckets, stream registry
python -m fingerprinter runtime init-profile              # rt-1 from this host (cpu + cuda variants; cpu admitted)
python -m fingerprinter runtime admit-cuda                # CUDA FP32 golden check vs CPU golden (≥ 0.999)
python -m fingerprinter spec add --builtin dinov2-seg/2   # FP32 default spec → prints its digest (design §3.2a)
python -m fingerprinter spec set-default <digest>
python -m fingerprinter intake init --from-start          # or --from-now --reason "…" (records a bootstrap gap)
```

Pinned versions: `requirements-p9.txt` (direct), `requirements-p9.lock` (full
environment = runtime profile `rt-1`): torch 2.13.0+cu130, transformers 5.15.1,
numpy 2.5.2, Pillow 12.3.0, ffmpeg 6.1.1-3ubuntu5, model
`facebook/dinov2-base@f9e44c814b77203eaa57a6bdbbd535f21ede1415`. Changing any of
them changes the runtime fingerprint: encoders then refuse the spec (fail
closed) until a new profile passes the runtime-compatibility gate or a new
spec is backfilled (ADR-027). Do not upgrade them casually.

## 3. Processes (one host or N hosts; only configuration differs)

| Role | Command | Notes |
|---|---|---|
| intake | `python -m fingerprinter intake run` | one owner at a time (Scylla lease, epoch, CAS cursor) |
| scheduler | `python -m fingerprinter scheduler run` | or role `scheduler` inside an encoder; rebuilds the Redis queue from `encode_due` |
| encoder (GPU) | `python -m fingerprinter encoder run --device cuda` | one per host (`fp:gpu:<host_id>`); a second exits 73 |
| encoder (CPU) | `python -m fingerprinter encoder run --device cpu` | FP32 only; never claims FP16 work |
| reconciler + publisher + sweeper | `python -m fingerprinter reconcile run` | intent pass every 60 s, sweeper, daily full pass |

Exit codes of `encoder run`: 0 normal/drained (SIGTERM drains up to
`shutdown_grace_s`), **75 controlled** (single-item GPU OOM, GPU lease lost, RSS
limit) → restart; **70 device error** → restart only after a device health
check; 73 GPU lease held; 69 CUDA or model unavailable; 78 runtime/golden
refusal (fix the environment, do not retry blindly). Each exit appends a line
to `<scratch_dir>/exit_reports.jsonl`. Any other termination (signal, OOM
killer) is **uncontrolled**; the work is recovered by lease expiry.

## 4. Configuration (`FINGERPRINTER_*`, `__` for sections)

Selected values (all initial proposals; tuning needs measurements, exit.md §5):

| Setting | Value | Why / constraint |
|---|---|---|
| `encoder.attempt_timeout_s` | 14 400 (4 h) | longest allowed attempt (approved value); real long-film encode times are not yet measured (exit.md) |
| `orphan_grace_s` | 86 400 | **must exceed** `attempt_timeout_s` (enforced at start-up) |
| `encoder.attempt_lease_s` / `_renew_s` | 600 / 120 | durable lease renewed while an attempt runs |
| `encoder.lease_ms` | 60 000 | Redis work lease (avoidance only), renewed every `lease_ms/3` |
| `encoder.gpu_lease_ms` / `_renew_ms` | 30 000 / 10 000 | GPU ownership per host |
| `encoder.max_attempts` / `max_claims` | 4 / 10 | attributed failures / poison-item bound per episode |
| `encoder.backoff_base_s` | 600 | retry after `600 × 3ᵏ` s |
| `encoder.cpu_batch`, GPU calibration | 8; GPU: `throughput/v1` — best repeatable measured frames/s among memory-eligible B (`calibration_frames` 64, `calibration_reps` 3, `calibration_tolerance` 0.05) | operator decision OF-2; design §12.3; result per host in `encoder_hosts.calibration` |
| `encoder.decode_threads` / `queue_items` | 3 / 4·B | bounded queue; producers block |
| `encoder.max_rss_bytes` | 4 GiB | controlled exit 75 above it |
| `acquire.max_bytes` | 8 GiB | = P8 `hash_max_bytes` |
| `acquire.ssrf_allow_cidrs` | empty | **dev/test only** (loopback fixtures); never set in production |
| `intake.replay_margin_s` | 86 400 | replay window before the cursor's time |
| `publisher.stream_max_bytes` | 64 MiB | ADR-022 budget on our own streams |
| `reconciler.intent_grace_s` | 120 | intents younger than this belong to live workers |
| `retire_grace_s` | 604 800 | objects of retired specs are deleted after it |

## 5. Operations

**Status:** `python -m fingerprinter --json intake status` (exit 1 while any gap
is open), `encode status`, `publisher status`, `reconcile run --once`.

**Intake gaps (ADR-025).** `intake gap list|show <id>` shows the window and
whether it is inside crawler2's 14-day outbox retention. Lossless repair:

```bash
python -m fingerprinter intake gap replay-start <gap> --from <ISO> --to <ISO>
crawler2-storage relay --replay-from <ISO> --replay-to <ISO>     # crawler2 side
python -m fingerprinter intake gap replay-done <gap>             # closes when observed
```

If the window is gone: `intake gap mark-unreplayable <gap> --reason …`, then
`intake accept-gap <gap> --operator <you> --reason …`. **Accepting a gap
accepts that requests may be permanently lost**: crawler2 will not re-request
them (P8-D7) until a future crawler2 mechanism exists. It is audited and never
reported as recovered. A stream reset halts the reader:
`intake reinit --after-reset --reason …` (the reset gap stays open).

**Failed requests.** `encode retry --content <cnt_…> [--spec-key default]` or
`--failure <class>` opens a new episode (failures reset, claim budget re-based;
`attempt` numbers keep increasing).

**Consumers.** `representation.ready`/`encode.failed` stay held until crawler2's
`media-registry` group exists. A new consumer (P10 matcher, P7 follow-up)
creates its group at ID 0, then `publisher register-consumer --stream
target.registered.v1 --group matcher`, then `publisher republish --stream
target.registered.v1` to bootstrap.

**Targets.** `target register --title … --kind movie --media <file>
[--target-id tgt_…]` (hash → staging → verified promotion to `ref/<sha256>`),
`target show|list`, `target retire <id> --reason …`, `target purge <id>`
(deletes `ref/<sha256>` only when no other target version uses it). A failed
build is `BUILD_FAILED` on the target (no `encode.failed` exists for it).

**Specs and backfill.** `spec add` (FP16 → `gated`), `spec enable <digest>
--evidence <json with precision_gate=PASS and labelled_decision_gate=PASS>`,
`backfill create --from-spec … --to-spec …`, `backfill run|pause|resume|cancel|status`
(`status` lists `source_lost` contents: candidate bytes are not archived, D11),
`spec set-default`, `spec deprecate`, `spec retire` (refused while default, in
a running backfill, guarded, or the only READY binding of an active target),
`spec retire-sweep`.

**Spec versions (OF-1).** `dinov2-seg/1` represents the decodable prefix of a
truncated file; `dinov2-seg/2` fails it with `decode_error`
(`truncated_input: matroska_file_ended_prematurely | mov_partial_file`) and is
otherwise identical (design §3.2a; detection limits listed there). Moving an
installation from v1 to v2: `spec add --builtin dinov2-seg/2` → `spec
set-default <v2>` → optionally `backfill create --from-spec <v1> --to-spec <v2>`
and `backfill run <job>` (bounded, rate-limited, pausable; **never automatic**)
→ `spec deprecate`/`retire` v1 when the guards allow. Old v1 rows stay valid
under their own identity until then.

**Crash recovery** needs no manual step: leases expire → due index → requeue;
the reconciler completes or cleans every intent; the sweeper deletes only
unreferenced objects older than `orphan_grace_s`. After Redis data loss the
queue is rebuilt from `encode_due`; intake halts if the stream itself was lost.

## 6. GPU troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| exit 69 `cuda_unavailable` | torch sees no CUDA device | check `nvidia-smi`; driver/library mismatch (seen 2026-10 before the driver fix) |
| exit 78 `golden_check_failed` | CUDA FP32 output differs from the CPU golden (≥ 0.999 cosine) after a driver/runtime change | do not override; re-run `runtime admit-cuda`, investigate the driver |
| exit 78 `runtime_incompatible` | a library version differs from `rt-1` | restore the lock, or run the runtime-compatibility gate for a new profile |
| exit 73 | another process owns `fp:gpu:<host_id>` | one GPU owner per host |
| repeated exit 75 `gpu_oom_single` | an item does not fit at batch 1 | the item fails `internal_error/gpu_oom_single` after `max_attempts`; check VRAM used by the desktop |
| exit 70 | CUDA error other than OOM | no CPU switch; health-check the device before restart |

`encoder recalibrate` forgets the stored batch size for this host; the next
CUDA start re-measures (≈ 1–3 min on the RTX 2050). The stored report shows
every candidate's frames/s, spread and peak VRAM:
`SELECT calibration FROM encoder_hosts WHERE host_id = '<host>'`.
Calibration failure: exit 75 `calibration: batch 1 does not fit` (free VRAM)
or exit 70 `calibration: device_error` (health-check the device).

## 7. Tests and benchmarks

```bash
cd ~/anti_piracy/fingerprinter && . scripts/p9-env.sh
env/bin/python -m pytest -p no:cacheprovider tests/v2 -q            # unit + real stack + GPU + two-host
RUN_P9_SLOW=1 env/bin/python -m pytest -p no:cacheprovider tests/v2/test_sources.py tests/v2/test_targets.py -k "8gib or 5gib"  # real 8 GiB / > 5 GiB; idle disk
cd ~/anti_piracy/crawler2 && scripts/test-fingerprinter-p9.sh -q    # cross-service
cd ~/anti_piracy/fingerprinter/benchmarks/p9-encoder
../../env/bin/python make_real_corpora.py {reel|c-prec|l-pairs|soak-1000}   # derive the corpora from S-SRC (resumable; needs ≥ 6 verified films)
../../env/bin/python make_real_corpora.py verify-soak   # re-check: every SOAK-1000 expected failure fails in the real decoder (OF-5)
../../env/bin/python calibrate.py --runs 2 --out results/b-cal-….json                                 # B-CAL
../../env/bin/python bench_inference.py --corpus <C-PREC dir> --per-item 8 --frames 504 --reps 3 --out results/…json  # B-GPU-a (G-5; GPU batch = encoder calibration)
../../env/bin/python precision.py --clips <C-PREC dir> --out results/…json                            # B-PREC, G-2 on C-PREC
../../env/bin/python precision.py --clips <L-PAIRS dir> --pairs <L-PAIRS dir>/l-pairs.csv --out …     # B-PREC, G-2 + G-3 on L-PAIRS
. ../../scripts/p9-env.sh && ../../env/bin/python bench_e2e.py --corpus <C-PREC dir> --device cpu --out …  # B-CPU (G-6); --device cuda = B-GPU-b
. ../../scripts/p9-env.sh && ../../env/bin/python soak.py --corpus <SOAK-1000 dir> --devices cuda,cpu \
    --attempt-timeout-s 3600 --attempt-lease-s 600 --orphan-grace-s 3900 --max-hours 48 --out results/soak-1000.json  # B-SOAK (G-8)
```

On this laptop, run every heavy job (corpus builds, benchmarks, the slow
tests, the soak) as its own `systemd-run --user --unit=p9-<name> -p MemoryMax=…
-p MemorySwapMax=0` unit with output to a log file: systemd-oomd kills the
terminal's cgroup when user-session memory *pressure* stays > 50 % for 20 s
(USB-HDD writeback), which killed several sessions on 2026-10-11. Wrap the
soak in `systemd-inhibit --what=sleep:idle:shutdown` (a power-off during the
~4 h run aborts it, as SOAK-1000 run 3 showed).

Reports state `gate_status`: a gate is evaluated only on its approved input set
(C-PREC, L-PAIRS, SOAK-1000); any other corpus is reported as a measurement
with `NOT MEASURED`.

## 8. Known limitations

At-least-once delivery (duplicates possible; consumers dedupe); accepted
intake gaps can lose requests permanently; candidate bytes are not archived
(backfill depends on sources still being served); FP16 is a separate, gated
identity; GPU behaviour depends on driver/runtime; NVDEC is not implemented;
DNS rebinding is not closed (P14); real multi-machine operation is validated
only in P14 (P9 = simulated two-host); replay re-publishes every crawler2 event
type in the window; parameter values are initial. Details: exit.md §6.
