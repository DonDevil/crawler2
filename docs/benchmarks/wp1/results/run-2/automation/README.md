# WP1 Run 2 — automated checks (workstream H)

Tool: `docs/benchmarks/wp1/scripts/wp1r2_hostguard.py` (stdlib only, read-only). Unit tests:
`tests/unit/test_wp1r2_hostguard.py`. Every check returns `OK | WARN | FAIL | UNKNOWN`; **missing telemetry is `UNKNOWN`, never `OK`**;
overall = worst of `FAIL > UNKNOWN > WARN > OK`; exit codes 0/1/2/3 = OK/WARN/FAIL/UNKNOWN. Gates that need infrastructure
this host does not have are reported by the benchmark reports as `BLOCKED` / `NOT_TESTED`, never as a pass.

Three kinds of threshold are kept apart (they are *not* interchangeable and none of them is a performance acceptance gate):

| Kind | Purpose | Example here |
|---|---|---|
| Safety | protect hardware / data; abort | NVMe >= 81 C, CPU sustained >= 95 C, free space < 20 GiB after the operation |
| Operational throttling | slow down before a safety limit is hit | NVMe >= 75 C (TMT1 throttle is 81 C), CPU median >= 90 C, free < 30 GiB |
| Performance acceptance | the project's gates (P2 p99, E-R1 p95 <= 10 ms, ...) | **unchanged; never edited by this tooling** |

## Check catalogue

| # | Check | Measures | Requirement protected | Authoritative source | Thresholds (origin) | Failure behaviour | Unavailable telemetry | Read-only | Tested by | Output | Integration |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `nvme_temp_hwmon` | NVMe composite temp | H-02 sustained-write/thermal; H-09 | `/sys/class/nvme/nvme0/hwmon*/temp1_input` (no root) | warn 75, fail 81 (controller TMT1 = 81 C, TMT2 = 83 C, WCTEMP 83, CCTEMP 85) | `FAIL` -> exit 2; callers must pause/refuse heavy I/O | `UNKNOWN` | yes | `test_nvme_temperature_levels`, `test_unavailable_temperature_is_unknown` | JSON (`--out`) | call `preflight` before a heavy job |
| 2 | `critical_warning`, `available_spare`, `percentage_used`, `media_errors`, `error_log_entries` | NVMe health | H-02 (device health) | `nvme smart-log -o json` (root; `--sudo`) | cw == 0; spare > spare_thresh; used warn 80 / fail 100 %; errors == 0 | `FAIL` | `UNKNOWN` with reason `permission_denied` / `tool_missing` / `command_failed` (a zero is only a zero when the field is present) | yes (`smart-log` only) | `test_missing_smart_is_unknown_not_ok`, `test_absent_field_is_unknown_but_zero_is_ok`, `test_media_error_and_critical_warning_fail`, `test_spare_below_threshold_fails` | JSON incl. `raw_smart` | same |
| 3 | `thermal_events` | new thermal-management transitions / warning / critical time | H-02 (throttling evidence) | SMART log counters (`thm_temp1/2_trans_count`, `warning_temp_time`, `critical_comp_time`) | with `--baseline`: delta must be 0 (new T1/T2 transitions = `WARN`, new critical time = `FAIL`); without baseline lifetime counters only | `WARN` / `FAIL` | `UNKNOWN` | yes | `test_thermal_events_delta_detects_new_throttling`, `test_critical_time_delta_fails` | JSON | store the previous report as the baseline |
| 4 | `free_space_after_operation` | free space minus the operation's planned consumption | H-08 / 20 GiB safety floor | `os.statvfs('/')` | fail < 20 GiB after, warn < 30 GiB after (task safety floor) | `FAIL` + "REFUSE operation" -> exit 2 | `UNKNOWN` | yes | `test_storage_preflight_with_simulated_capacity` (simulated capacity; the real disk is never filled), `test_cli_storage_json_and_exit_code` | JSON | `wp1r2_hostguard.py storage --planned-gib N` before any bulk write |
| 5 | `cpu_package_sustained` | CPU package temp over a 1 Hz window | H-09 thermal safety | `coretemp` hwmon "Package id 0" | FAIL: >= 95 C for 3 consecutive polls or any >= 100 C; WARN: spike >= 95 or median >= 90 (i5-11400H TjMax 100 C; 1 s boost spikes to 85-95 C occur at idle, see thermal report) | `FAIL` | `UNKNOWN` | yes | `test_cpu_*` | JSON | `thermal --window-s 5` |
| 6 | `gpu_temp` | GPU temp | H-05 / H-09 | `nvidia-smi` | warn 87 (vendor target; SW thermal slowdown 0x20 was observed at 87 C), fail 95 (slowdown 97 C) | `FAIL` | `UNKNOWN` | yes | `test_thermal_levels` | JSON | same |
| 7 | `<container>:oom_killed / restarts / health / mem_pct_of_limit` | container state and memory pressure | Docker resource limits (workstream F) | `docker inspect`, `docker stats` | OOM -> FAIL; restarts > 0 -> WARN; health != healthy -> FAIL; memory warn 85 % / fail 95 % of the limit | `FAIL`/`WARN` | `UNKNOWN` (no healthcheck is reported as `none`, which is accepted as not-failed but visible) | yes | `test_container_checks` | JSON | `containers` |

| 8 | `qdrant_build_peak_anon_gib` | predicted peak cgroup anon memory of a Qdrant two-generation rebuild for `--target-points N` | FP-05 / E-R1 memory rule (anon <= 1.35 GiB) | linear model anon = 0.178 GiB + 1.18 KB x resident points, fitted on the first-pass 1x/3x runs; the quiet reruns (0.4215 / 0.656 / 0.8915 GiB) lie 1.3-3.0 % *below* the model, i.e. it stays slightly conservative | refuse (FAIL, exit 2) if prediction x 1.10 > 1.35 GiB; 250,000 -> 0.76 GiB OK; 626,658 -> FAIL | `FAIL` | `UNKNOWN` if N missing | yes | `test_qdrant_model_reproduces_measurements`, `test_qdrant_*` | JSON | `qdrant-build --target-points N` before a rebuild |

Memory only: the latency requirement (E-R1 p95 <= 10 ms) is the binding constraint and is not predicted by this model.

**Evidence of use (post-run):** `../raw/automation/nvme-final.json` (all SMART-derived NVMe checks OK with `--sudo`, 20:09) and `preflight-final.json` (without sudo the SMART checks are `UNKNOWN`, overall exit 3 — by design, not OK).

Not implemented as checks (see the report): GPU batch selection (the project's `throughput/v1` calibration already does it) and offline model availability (verified in the GPU probe, not a recurring check).
