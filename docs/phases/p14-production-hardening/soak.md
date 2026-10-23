# The 7-day soak: specification and operator guide

Status: **harness implemented and tested; the 168-hour run has NOT been run and cannot be started yet.**
Blockers are in §9. `crawler2/soak/` is the harness, `crawler2-soak` its command
(`python -m crawler2.soak.cli …` works before `uv sync` installs the entry point).

## 1. What the run must show (M6 soak gate)

Plan wording: *"soak passes with no leaks/lost work"* (P14 exit gate). Measurable form
(`to_complete.md` §4.10), unchanged: ≥ 168 h **continuous**, on the **physical topology**, all code and
configuration frozen (or the run repeated), zero unaccounted-for work from task to evidence, a bounded and explained
duplicate count, no unexplained monotonic growth of RSS/FDs/threads/browser memory/Redis memory/queue depth/Scylla
compactions/disk. Earlier runs (P6 24 h, P9 2.8 h) do not count.

**Verdicts** (`evaluate.py`; strongest first):

| Verdict | Meaning |
|---|---|
| `INVALIDATED` | the git commit/dirtiness of a pinned repo, the configuration, the topology or a watched file changed during the run: it is no longer representative; rerun from the new revision |
| `INTERRUPTED` | the run did not complete its required duration: stopped on purpose, or the runner died and was not resumed within `heartbeat_stale_s` (a dead runner is never reported as "running") |
| `FAIL` | at least one required criterion failed |
| `INCONCLUSIVE` | nothing failed, but required evidence is missing, a limit is **undecided**, or reconciliation cannot rule out loss |
| `PASS` | every criterion has evidence and passed |
| `RUNNING` | not finished; no verdict |

Orthogonal flags: `scope` (`production` / `production-dry-run` / `synthetic`) and `m6_eligible`
(true only for a production, non-dry-run, **PASS**). A synthetic or dry-run PASS prints a banner saying it is
not evidence. A criterion's `category` (`system`, `environment`, `evidence`, `measurement`) says whether a failure
is ours or the environment's; the verdict is the same either way.

## 2. Contract

| Item | Value |
|---|---|
| Duration | `duration_h = 168` (production configs below 168 are rejected unless `dry_run = true`) |
| Sampling | `sample_interval_s = 60` default; each round collects every series, polls new events (bounded batch per stream, no scans), checkpoints |
| Topology | recorded verbatim in `[topology]`, fingerprinted; required (physical hosts and roles). Single-laptop runs are not valid for M6 |
| Versions recorded | git commit/branch/dirtiness of each `repos` entry, config fingerprint, package versions, host CPU/RAM/disks/GPU, preflight results |
| Pinned (change ⇒ `INVALIDATED`) | each repo's commit and dirty flag and (every 10 rounds) the diff hash of tracked files; the config fingerprint; topology; content hash of every `watch_paths` entry |
| Workload / seeds | **not defined** — OD-S0. The soak observes whatever the deployed crawler is configured to do; the fixed seed set, campaign/target configuration and expected throughput are the owner's to fix before the start (the harness cannot invent a workload) |
| Artefacts | `<run_root>/<run_id>/`: `config.json` (redacted), `run.json`, `checkpoint.json`(+`.prev`), `samples.jsonl`, `events.jsonl`, `ledger.sqlite3`, `evidence_check.json`, `report.{json,md,html}`. **Never deleted by the harness** |
| Interrupted run | `resume` appends an `interruption` event (gap seconds) and continues; **continuity is judged by OD-S1**, not assumed. A run that died and is never resumed is `INTERRUPTED` |
| Code/config change | invalidates the run (`INVALIDATED`). After a fix: new run, new id. Nothing is "patched into" a running soak |

## 3. Criteria

| Id | Criterion | Rule | Limit comes from |
|---|---|---|---|
| FREEZE | code/config/watched files unchanged | any fingerprint differs ⇒ INVALIDATED | fixed |
| DURATION | ran for `duration_h` and completed | elapsed (start → the run's own `completed` record, not first-to-last sample) < required ⇒ INTERRUPTED | fixed (168 h) |
| CONTINUITY | no interruption | none ⇒ PASS; else needs `max_interruptions` **and** `max_interruption_gap_s` (undecided ⇒ INCONCLUSIVE) | OD-S1 |
| CLOCK | no wall-clock anomaly | wall/monotonic mismatch or time going backwards ⇒ INCONCLUSIVE | fixed |
| METRICS | every required series present | never produced ⇒ INCONCLUSIVE; gaps judged by `min_series_coverage` (undecided ⇒ INCONCLUSIVE) | OD-S2 |
| SAMPLES | sample log intact | unreadable lines or no samples ⇒ INCONCLUSIVE | fixed |
| OUTAGE | dependency outages | longest `up == 0` run ≤ `max_dependency_outage_s` (none ⇒ PASS; undecided ⇒ INCONCLUSIVE; exceeded ⇒ FAIL, category environment) | OD-S3 |
| RESTARTS | process restarts | per service ≤ `max_restarts_per_service` (pid/start-time change of a watched process, or a changed `process_start_time_seconds`) | OD-S4 |
| GROWTH | no unbounded growth | per `growth_watch` series: regression slope per hour **and** relative growth over the run vs. its limit; no limit ⇒ INCONCLUSIVE; < 20 points ⇒ INCONCLUSIVE | OD-S8 |
| QUEUE_AGE / EVENT_LAG | peaks | ≤ `max_queue_age_s` / `max_event_lag_s`; no series ⇒ INCONCLUSIVE | OD-S6 |
| DISK | headroom | minimum free fraction ≥ `min_disk_free_fraction` | OD-S5 |
| RETRIES | no retry storm | retries ÷ completed work ≤ `max_retry_ratio` | OD-S7 |
| LOST_WORK | **no unaccounted-for work** | any required edge with a source older than its `grace_s` and no target ⇒ **FAIL**; undecided `grace_s`, observer gaps (trimmed entries) or read errors ⇒ INCONCLUSIVE | zero is fixed by the plan; `grace_s` is OD-S9 |
| DUPLICATES | no duplicate business effects | one key with two different effects ⇒ **FAIL**; the same `event_id` again is a counted *redelivery* (explained) | fixed (zero) |
| STAGES | every required stage exercised | `required_stages` with zero events ⇒ INCONCLUSIVE (with the note that `match.found` is held) | owner lists the stages |
| EVIDENCE | manifests resolve to artefacts with matching hashes | final `crawler2-evidence validate --dry-run` on up to `sample` finalized items: corrupted/rejected/missing ⇒ **FAIL**; none verified ⇒ INCONCLUSIVE | fixed (zero) |

**No criterion ever treats a missing value as zero.** A collector that cannot read a series records `null` and a
reason; the report lists every missing metric.

## 4. Owner decisions the run needs (none is invented)

`production.example.toml` leaves each of these unset. **Proposed starting points are suggestions for the owner to
accept or change; they are not approved thresholds.**

| Id | Decision | Suggestion (not approved) |
|---|---|---|
| OD-S0 | workload: fixed seed set, campaign targets, expected throughput, who may start | seed set from `benchmarks/v1-baseline`; targets from the P12 fixture campaign until real ones exist |
| OD-S1 | continuity: max interruptions and max single gap | 0 interruptions for a "continuous" claim, or ≤ 1 gap ≤ 15 min if a runner restart is tolerated |
| OD-S2 | min series coverage | 0.99 |
| OD-S3 | max dependency outage | 300 s (and a chaos run covers longer ones separately) |
| OD-S4 | max restarts per service | 0 for data-path services; browser recycling is not a restart |
| OD-S5 | min disk free fraction | 0.10 |
| OD-S6 | max queue age / event lag | 600 s |
| OD-S7 | max retry ratio | 0.5 (P4 data: 2xx 95.5 %) |
| OD-S8 | growth limits per series | slope ≤ a few % of the median per day after a warm-up window |
| OD-S9 | edge grace periods | measured from a short run (p99 stage latency × margin) |
| OD-S10 | who approves, and where it is recorded | `[approval]` must be complete before a production start |

## 5. What reconciliation can and cannot see

Events observed on the real Redis streams are keyed by contract fields and joined per **edge**. Implemented
joins that are deterministic from event ids alone:

* `encode.requested` → `representation.ready` **or** `encode.failed` (non-retryable) by `content_id`, with the
  representation id as the *effect* (two different ids for one content = duplicate effect);
* `evidence.finalized` self-join by `evidence_id` with the manifest digest as the effect;
* `fetch.completed` → `page.observed` by `fetch_attempt_id` — **informational** (not every fetch has a body);
* consumer progress (queue age, lag) from the Redis collector / exporter, not from joins.

**Not joinable from events (gaps):** RG-1 `match.found` → `evidence.*` (the evidence id is minted at
collection and linked to the match id only in Scylla); RG-2 `media.discovered` → `encode.requested` (media ids are
derived from locators, not carried); RG-3 whole-chain `CrawlTask` → evidence needs the correlation id to be
propagated by the fingerprinter and finalizer (observability.md §4). The harness reports exactly the edges it
checks; it does not claim end-to-end loss detection beyond them. The strongest remaining check is the final
`crawler2-evidence validate` sweep.

Streams are trimmed (ADR-022). The tailer starts at the current end of each stream and compares Redis's
`entries-added` counter with what it read; any difference is a recorded **gap** that makes LOST_WORK
INCONCLUSIVE rather than silently passing. Keep the poll interval well below the retention window.

## 6. Operator procedure

**Prerequisites:** the physical topology running and healthy; the P14 observability stack scraping it; the
`monitor` Redis user; `crawler2-exporter` running; the real `match.found` path released (§9); all
OD-S decisions made and recorded.

```
# 1. install (on the runner host - any host with network access to the above)
uv sync --frozen
cp scripts/soak/production.example.toml scripts/soak/production.toml     # edit: hosts, topology, limits, approval
export SOAK_REDIS_USER=monitor SOAK_REDIS_PASSWORD="$(cat secrets/redis_monitor_password)"

# 2. preflight (creates nothing): every check prints [OK]/[WARN]/[FAIL]
crawler2-soak prepare scripts/soak/production.toml            # exit 0 = may start

# 3. dry run of the same pipeline on the real services (4 min, never M6-eligible)
crawler2-soak dry-run scripts/soak/dryrun-local-stack.toml    # adapt hosts; expect verdict INCONCLUSIVE until match.found flows

# 4. start the real run, supervised (recommended)
sudo cp deploy/systemd/crawler2-soak.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl start crawler2-soak          # first start creates the run; note the run id in `journalctl -u crawler2-soak`
#    set SOAK_RUN_DIR=<run dir> in /etc/crawler2/soak.env so a restart RESUMES it, not starts a new one
#    (or without systemd: crawler2-soak start scripts/soak/production.toml)

# 5. watch it (any time, from any shell)
crawler2-soak status /var/lib/crawler2/soak/<run_id>         # phase, runner_alive, progress, last_sample_age_s, interruptions, anomalies
crawler2-soak report /var/lib/crawler2/soak/<run_id>         # provisional criteria while RUNNING

# 6. runner died?   status shows needs_resume=true
crawler2-soak resume /var/lib/crawler2/soak/<run_id>         # records the gap as an interruption, continues

# 7. stop on purpose (graceful; resumable)
crawler2-soak stop /var/lib/crawler2/soak/<run_id>

# 8. final report (written automatically at completion; re-runnable)
crawler2-soak report /var/lib/crawler2/soak/<run_id>         # report.html / report.md / report.json; exit 0 only for PASS
```

`status` expected shape: `"phase": "running", "runner_alive": true, "progress": 0.42, "last_sample_age_s": 12.3,
"needs_resume": false`. `last_sample_age_s` above ~3× the interval with `runner_alive: true` means the runner is
wedged; stop and resume. **Deciding M6:** the soak gate is met only if `verdict == PASS`, `m6_eligible == true`
and the report's criteria table has no INCONCLUSIVE row; M6 additionally requires the chaos matrix, restore
drill and RF=3 evidence (exit.md).

**Troubleshooting.** `refusing to start: preflight failed` → read the `[FAIL]` lines (clean git trees are required;
`approval` incomplete). `tail_error: … no such key` is benign only for a stream that never existed (fixed);
repeated `tail_error` means Redis auth/ACL (the `monitor` user needs `XRANGE`, `XINFO`, `XLEN`, `XPENDING`).
`stream_gap` events mean the tailer fell behind retention. A corrupt checkpoint is recovered from the previous
generation, or rebuilt from the sample log with the stream positions marked lost (⇒ LOST_WORK INCONCLUSIVE).

## 7. Robustness of the harness itself
Atomic writes (temp file + fsync + rename + directory fsync); checksummed two-generation checkpoint;
append-only fsync'd sample/event logs that tolerate a torn last line; an `flock` per run directory so two
runners can never share one (the lock dies with the process); a broken collector is a missing metric, not a dead
run; secrets are referenced by environment-variable *name* and never written; the HTML report escapes every
string and loads nothing remote. Disk use ≈ 10–15 MB per 168 h at 60 s with a dozen series.

## 8. What was actually tested (this sprint)
77 unit tests (`tests/unit/soak/`): configuration and redaction, store crash safety, collectors, ledger
(lost/pending/duplicate/redelivery/orphan/filtered), tailer gap detection, evaluator verdict matrix
(INVALIDATED, INTERRUPTED, FAIL, INCONCLUSIVE, PASS), resume after stop, after a corrupt checkpoint and after a
clock step, restart counting, report escaping, secret handling, CLI. Two short end-to-end runs:
synthetic (2 min, verdict PASS, `m6_eligible=false`) and against the local dev stack with real collectors and real
stream tailing (4 min, verdict INCONCLUSIVE as designed — no `match.found`). Details: [validation.md](validation.md) §2.
**Short runs validate mechanics only**: not long-duration behaviour, not multi-host, not production acceptance.

## 9. Blockers to a valid production soak
1. **No `match.found`** can be produced (P10 calibration held; T4/T6/T8/T10) — the pipeline's back half is
   unexercised; `required_stages` makes the run INCONCLUSIVE without it. Do not remove it from the list.
2. **Physical topology** does not exist (TC-001/TC-040). The laptop cannot run 7 days (recorded constraint).
3. **OD-S0…S10** undecided; `[approval]` therefore cannot be completed honestly.
4. Observability gaps: no fingerprinter latency series (FP-1); gaps RG-1…RG-3; correlation not propagated past the
   registry (TR-1/TR-2).
5. Known fetch pre-conditions fixed this sprint (X-14 429 back-off, X-15 declared-size early refusal) must be
   *in the revision that is soaked* — they are, if this branch is what is deployed.
