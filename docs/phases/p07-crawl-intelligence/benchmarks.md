# P7 Benchmarks and data collection

Audit: [audit.md](audit.md) · Decisions: [decisions.md](decisions.md).

## 1. M1 history (audited 2026-10-07)

`crawler2_m1`, runs 1 and 2, about 46 h. Measures and gate result are in
[audit.md](audit.md) §2–§3 and `benchmarks/p7-intelligence/results/m1-audit*.json`.
Gate not passed.

## 2. Focused collection run "M1b" (decision D-5) — CONSIDERED AND REJECTED (D-12), NOT RUN

> 2026-10-07: rejected after the read-only reassessment (audit §5). The limit
> is the seed population (11 of 50 live), not collection time. The plan below
> is kept as written, as history. Nothing in it was executed. The old `m1:*`
> Redis keys were not deleted (D-14).

**Purpose.** Collect seed-set history that can satisfy G1–G4 and supply
pairs for G5, under the unchanged static M1 policy. No P7 intelligence is
involved.

### 2.1 Configuration

| Item | Value | Note |
|---|---|---|
| dataset | `M1_DATASET=m1b`: keyspace `crawler2_m1b`, Redis namespace `m1b`, streams `m1b:events:*`, bucket `crawler2-m1b`, state `var/p6-m1b/` | fresh. `crawler2_m1` stays untouched as evidence |
| seeds | `benchmarks/v1-baseline/seeds.txt`: 52 lines, sha256 `26cd2294…d1b428cc`, 50 registrable domains, source `w52-v1-seeds` | the seed set of the P7 gate and M2 |
| search | **off** (`run.sh start` without a query file) | no off-target scope roots (audit §2.2) |
| scope | rooted seed domains plus leaves (P6 D-2) | unchanged |
| revisits | seeds every 6 h (`--every 21600`); a rediscovered link when its last admission is ≥ 24 h old (`discovery.revisit_after_s`) | unchanged static M1 policy (P6 D-3) |
| priorities | seed 70, link 50, leaf 40 | unchanged |
| filter ruleset | built_in + V1 reviewed + EasyList + EasyPrivacy, imported at `setup` (the lists' current revisions) | ruleset id recorded in `var/p6-m1b/ruleset-status.json` and in the results |
| workers | http concurrency 4, browser 2 pages, 2 × extract, 1 admit, relay, monitor (60 s) | as M1 run 2 (HDD-bound, P6 X-9) |
| frontier | P3 defaults: `max_depth` 200,000 per queue, per-domain in-flight 2 | unchanged |
| event retention | ADR-022: `stream_max_bytes` 48 MiB per stream (5 streams ≤ 240 MiB), `stream_maxlen` 10,000 for streams without groups | X-8 |
| relay | in-process retry, 1 s → 30 s | X-11 |

### 2.2 Prerequisites (operator)

1. **Free the old M1 Redis state.** Redis holds 393 MB of `m1:*` keys
   (199,996 frontier task hashes, queues, 5 streams) out of the 768 MB
   `maxmemory`. Those keys are transient frontier and transport state for a
   dataset that will not be continued. The M1 history in Scylla is not
   affected. Deleting them (`m1:*` only) leaves room for the new frontier
   and the stream budget. **Needs approval.**
2. `M1_DATASET=m1b benchmarks/p6-m1/run.sh setup` (migrations, bucket, rules).
3. Chromium on the host for the browser pool, as for M1.

### 2.3 Expected growth

From M1 run 2 (4,528 completions/h): Scylla +4.5 GB per day, MinIO
+9.8 GB per day. For 5 days that is about 23 GB of Scylla and 49 GB of
MinIO, with 354.8 GB free. Seed-site pages are smaller than NCBI's, so this
is an upper estimate.

### 2.4 Monitoring and stop conditions

The monitor samples every 60 s: process RSS/FDs/threads/children, Redis
memory, per-stream length, **bytes** and group lag, frontier depth,
scheduled, leased and dead letters, relay and admission metrics (including
`outbox_backpressure_total` and `outbox_publish_failures_total`), the
`fetch.completed` tally, host iowait, and Scylla/MinIO sizes.
`run.sh check` flags:

- a process down, or the monitor stale;
- `fetch.completed` entries the monitor missed;
- Redis > 700 MB;
- relay backpressure in every sample for 15 min;
- **`page.observed` not advancing for 15 min while fetches finish** (a fetch
  is not a propagated observation).

`benchmarks/p6-m1/watch.sh` (with `M1_DATASET=m1b`) prints these as alerts.
A watcher started from a Claude session lives only as long as that session.

**Stop and report** if:

- an event is lost: a grouped stream's lag inconsistent with its entries,
  or extraction/admission skipping entries;
- Redis reaches its limit;
- Scylla, Redis or MinIO cannot sustain the run;
- the loop stalls for more than an hour.

### 2.5 Start and recovery

```bash
export M1_DATASET=m1b
benchmarks/p6-m1/run.sh setup
benchmarks/p6-m1/run.sh start          # no query file: search off
benchmarks/p6-m1/run.sh check          # health (exit 1 = problem)
benchmarks/p6-m1/run.sh stop           # stop; `start` again continues the dataset
```

Every process runs under a `setsid` supervisor that restarts it, and the
relay no longer exits on storage errors. After a host restart, `start`
resumes the same dataset: the frontier refills from seed re-admission
(6 h) and rediscovery, and unpublished events wait in the outbox for 14
days.

### 2.6 How the run is evaluated (pre-registered 2026-10-07)

- The run ends when G1–G4 are met on its own data, or when they stall, and
  never on the calendar alone (D-5). Progress is measured once a day with
  `P7_EXPORT=var/p7/m1b-export env/bin/python benchmarks/p7-intelligence/export_m1.py
  --host <ip> --keyspace crawler2_m1b`, followed by the audit scripts.
- **The gate is evaluated on `crawler2_m1b` alone.** Thresholds and
  definitions are those of audit §3.2. The combined M1 + M1b seed-set
  figures are reported for information only.
- **G2 windows:** A = the run's first 24 h, B = from 48 h after start, so
  they are ≥ 24 h apart (`P7_WINDOW_A_END`, `P7_WINDOW_B_START`). The
  domain criterion is unchanged: ≥ 30 attempts in the run and attempts in
  both windows.
- **G5:** ≥ 50 changed pairs sampled from `crawler2_m1b` across several seed
  domains and path depths, labelled from the stored snapshots before any
  P5/P7 rule change, and held out (D-9).
- **G6** is reported for http and browser. Tor is deferred (D-7).
- **G7** is deferred until P9 (D-7).
- **M2 baseline:** the browser-requiring subset (domains/patterns with
  http `needs_js` → browser escalation) and its M1b baseline are measured
  from this run. The M2 metric and X/Y are fixed before any P7 evaluation
  (D-8).

**Risk.** Half of the seed domains were unreachable within days in P0/P4,
and 25 of 50 never answered 2xx in M1. G1/G2 count attempts of any outcome,
but G3 needs ≥ 10 seed domains with live, revisitable HTML. If fewer
domains are alive, G3 cannot be met by collection. That will be reported
as such, and the threshold will not be lowered.

## 3. Increment 1 offline replay (2026-10-07)

`env/bin/python benchmarks/p7-intelligence/replay_sources.py` (reads
`var/p7/m1-export`). Output: `results/p7-sources-replay.json`. Method:
design §5.3, policy `source-health/v1` (design §4.5). It is deterministic:
attempts are ordered by start, then id.

| Population | Attempts | Executed | Suppressed | Probes | Lost answers | M1 seconds | Seconds saved |
|---|---:|---:|---:|---:|---:|---:|---:|
| all | 247,104 | 209,306 | 37,798 | 15 | 3 | 606,965 | 37,763 (6.2 %) |
| off-target, run 1 | 57,774 | 55,756 | 2,018 | 5 | 1 | 190,944 | 6,874 (3.6 %) |
| off-target, run 2 | 157,199 | 121,543 | 35,656 | 4 | 1 | 357,503 | 29,954 (8.4 %) |
| seed, run 1 | 30,190 | 30,112 | 78 | 6 | 1 | 54,362 | 682 (1.3 %) |
| seed, run 2 | 1,941 | 1,895 | 46 | 0 | 0 | 4,156 | 252 (6.1 %) |

Hosts' states at the end of M1 (2026-10-05 09:31 UTC):

- seed hosts: active 15, degraded 42, unreachable 9, error 1;
- off-target hosts: active 176, degraded 581, blocked 11, unreachable 9,
  error 8, moved 2.

Many dead seed hosts read `degraded` at the end because their few failures
have aged out (design §4.3). Under the policy that triggers a normal
admission, and then the host is classified again.

Not measurable offline: what the freed attempts would have discovered
(M2 live A/B, design §10).

## 4. Increment 2: admission latency and reader scan (2026-10-07)

**Admission latency.** `env/bin/python benchmarks/p7-intelligence/admission_latency.py`
→ `results/p7-admission-latency.json`. The P6 `Admitter` runs in-process
over an in-memory frontier and store, so only the cost P7 adds is measured.
Setup: 10,000 hosts (a seeded mix of live, dead and JS), 400 batches of 50
URLs, 5 rounds.

| Scenario | Median µs/URL | p99 µs/URL | vs P7 off |
|---|---:|---:|---:|
| P7 off (`advice_enabled=false`) | 18.57 | 34.56 | — |
| informed, 10k-host snapshot | 41.84 | 63.43 | +23.27 |
| fallback: no snapshot | 19.97 | 37.53 | +1.40 |
| fallback: projection stale | 20.73 | 36.99 | +2.16 |
| fallback: snapshot store hanging | 19.77 | 35.14 | +1.20 |

- The informed figure includes a structlog debug line per deferred URL
  (about a third of the URLs here).
- Real admission adds a Redis `admit_many` and Scylla F5/F6 writes per
  batch, which are not part of this measurement. P7 adds no remote call.
- Snapshot: 10,000 rows of ~487 bytes of JSON decode in 72.5 ms, inside
  the background refresh (every 300 s), never on the admission path.

**Reader scan.** `benchmarks/p7-intelligence/reader_scan.py` on the
throwaway integration keyspace → `results/p7-reader-scan.json`: 63 buckets
(2,016 shard reads, 96 facts) in 1.39 s, which is 45 buckets/s on the dev
host (USB HDD). A full 14-day rebuild is 20,160 buckets, about **7 min** at
that rate. That is a lower bound, because these buckets were nearly empty.
A busy outbox costs more per bucket. This is a sample of one, not a load
test. The steady state is 1 bucket per minute.


## 5. Increment 3 (WHAT): CPU cost (2026-10-08)

`benchmarks/p7-intelligence/what_bench.py` → `results/what-bench.json`.
It runs on the host (Python 3.12, x86_64). The 20,966 seed pages are loaded
first, outside the timing. It times `build_site_profile` per host and
`analyze_page` per page. No network or storage is involved.

| Measure | Value |
|---|---|
| pages / hosts | 20,966 / 27 (incl. spam subdomains) |
| anchor links per page | median 31, p99 287, max 310 |
| profile build, all hosts | 3.2 s total (the largest host, ~4,800 pages, sampled to 1,000) |
| `analyze_page` | median **268 µs**, p99 **4.5 ms**, max 9.9 ms |

Decisions over the whole population, unlabelled, descriptive only:

| Kind | relevant | spam | unknown |
|---|---:|---:|---:|
| detail | 8,912 | 4,474 | 2,269 |
| hub | 3,538 | 95 | 1,030 |
| other | 6 | 17 | 625 |

The cost scales with a page's links: each one is tokenized and checked
against the vocabulary. This is fine for batch use. A runtime hook would
want a per-profile cache of link templates, and that optimization is
deferred until a runtime use exists.

## 6. WHEN readiness audit (2026-10-08, D-20)

`benchmarks/p7-intelligence/when_audit.py audit` → `results/when-audit.json`,
`var/p7/when/events.jsonl.gz`. Offline, on the host, one pass over the
export's observations and extracts: **39 s**, peak RSS **445 MB**. This is a
dev-host offline figure, not a production claim. The results are in
[when.md](when.md) §5.

`when_audit.py hubs` → `results/when-hub-proposal.json`: the 84-hub
population of the proposed hub-only collection **W1** (when.md §7:
6-hourly, 7–14 days, ≈ 8,000 fetches, fresh dataset `m1w`). **W1 has not
been run.** It needs the operator's explicit go and a reviewed `run.sh`
change (seed file, no `admit`).
