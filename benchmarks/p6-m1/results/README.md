# M1 results (P6 Gate F/G)

Evaluation and verdicts: `docs/phases/p06-filter-discovery/validation.md`
§4.1–§4.3; deviations X-10–X-16 in `decisions.md` there.

| File | What |
|---|---|
| `m1-run1.json` | `report.py --no-scylla` (old flag) for run 1, window 2026-09-29 21:20 → 09-30 13:59:58 UTC (16.7 h, ended by a terminal crash) |
| `m1-run2.json` | `report.py --window-h 24 --since 2026-10-04T07:39:53Z --until 2026-10-05T07:39:53Z` for run 2 (full 24 h window) |
| `run1/`, `run2/` | raw material of each run, copied from git-ignored `var/p6-m1/` |
| `run2/mid-run-diagnostics.md` | per-host breakdowns computed from the live streams during run 2 (not reproducible afterwards: streams keep 3,000 entries) |

Each `runN/` holds:

| File | What |
|---|---|
| `samples.jsonl.gz` | every monitor sample (one JSON object per minute; schema in `monitor.py`). Run 1: whole run 17:26 → 13:59:58 UTC incl. the pre-window reconfigurations; its samples predate the X-13 monitor (no `fetch`/`host`, `*_created` series in `metrics`). Run 2: the 24 h window only (1,440 samples) |
| `restarts.log` | supervisor log: every process start/exit (exit code) plus operator actions (reconfigure, `xtrim`), UTC. Run 2's runs past the window end |
| `config.env` | the `CRAWLER2_*` environment at the last (re)configuration, secrets removed. Run 2 shows `STREAM_MAXLEN=3000` (set at 17:04:27 UTC; 10,000 before, X-16) |
| `ruleset-status.json` | the active filter ruleset (`rs-df6cc49a…`, 106,110 rules), same for both runs |
| `started_at.txt`, `window_start.txt` | start of the run / of the Gate G window |
| `log-summary.txt` | `logsummary.py` over the process logs (log levels, warning/error events, exception classes with the Scylla table). Log timestamps are local time (IST, UTC+5:30). The raw logs (32 MB and 50 MB) stay in `var/p6-m1/` |

Not kept: the operator query file (private; sha256 `338ffb33…` recorded in
validation §4.1), the raw process logs, and the M1 dataset itself (Scylla
keyspace `crawler2_m1`, MinIO bucket `crawler2-m1`, Redis namespace `m1`),
which M1 keeps using as P7 history.
