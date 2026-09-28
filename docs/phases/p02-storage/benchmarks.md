# P2 Benchmarks

Code: `benchmarks/p2-storage/` (`run.sh partitions|latency`). Results:
`benchmarks/p2-storage/results/<UTC time>/`. Host: 12 cores, 15 GB RAM;
Scylla 6.2.3 single node (`--smp 2 --memory 1400M`, 2 GB container, RF=1);
app containers 1 CPU / 512 MB each (two-host profile).

## 1. Partition size at a 1M-page load

### Method

- **Worst case by construction.** Each scenario concentrates the *whole*
  1M-page load on one key (one domain, one hub URL, one media file, one
  target …). Any real distribution of 1M pages is less concentrated.
- **Real layout, real writes.** Pass 1 maps every logical row to its
  partition with `crawler2/storage/layout.py` and counts rows per shard
  (exact distribution below). Pass 2 writes *every* row of the heaviest
  shard through the production repositories and contract models, so the
  measured partition is exactly the one the full load would create. The
  other shards (same row counts ± 0.4 %) are not written.
- **Sizes from Scylla.** After `keyspace_flush` + major compaction (REST
  API), sizes come from `system.large_partitions` (threshold lowered to
  1 MB at runtime, restored afterwards: exact bytes and row count per
  partition) and the `max_row_size` metric.
- Gate: no partition > 100 MB (10⁸ bytes).

### Scenarios

| Code | Table (primary) | Load |
|---|---|---|
| S1a / S1b | W2 / W14 | 1M fetches / observations of one domain within one UTC day |
| S1c | W11 | 10M known URLs of one domain (10 discovered per crawled page) |
| S2 | W10 | one hub URL linked from all 1M pages |
| S3a | M3 (+M1, M6) | one media file embedded on all 1M pages within one month |
| S3b | M5 (+M2, M4) | the same bytes at 1M distinct locators |
| S4a / S4b | W6 (+W8) / W3 | one URL observed every minute for 31 days, a new version each time |
| S5 | W9 | one page version with the maximum 10 000 links |
| S6a / S6b | P4 (+P5) / E4 | 1M matches / evidence items of one target within one month |
| S7 | M2 (+M4) | one page observation with the maximum 1 000 media |
| S8 | P2 | 10 000 targets in the listing partition |
| S9 | outbox | one minute at 10 000 events/s cluster-wide |

### Results (run `20260927T220347Z`) — gate **passed**

Largest partition per table (exact, from `system.large_partitions`;
tables written only as a side effect of a scenario have no scenario code):

| Table | Scenario | Rows in largest partition | Size | Bytes/row | Shard rows min / max (σ) |
|---|---|---|---|---|---|
| page_observations_by_url (W6) | S4a | 44 640 | **43.4 MB** | 829 | unsharded |
| outbox (X1) | S9 | 19 055 | 25.1 MB | 1 311 | 18 461 / 19 055 (130) |
| urls_by_domain (W11) | S1c | 157 223 | 14.5 MB | 79 | 155 499 / 157 223 (370) |
| media_by_content (M5) | S3b | 125 299 | 12.1 MB | 84 | 124 518 / 125 299 (245) |
| page_versions_by_url (W8) | via S4a | 44 640 | 10.1 MB | 205 | unsharded |
| media_observations_by_media (M3) | S3a | 62 915 | 8.4 MB | 131 | 62 041 / 62 915 (200) |
| matches_by_target / _by_domain (P4/P5) | S6a | 62 859 | 8.4 MB | 126 | 62 206 / 62 859 (220) |
| observations_by_domain_day (W14) | S1b | 63 025 | 7.0 MB | 108 | 62 144 / 63 025 (225) |
| fetch_attempts_by_domain_day (W2) | S1a | 62 937 | 5.8 MB | 86 | 62 053 / 62 937 (235) |
| evidence_by_target (E4) | S6b | 62 969 | 4.9 MB | 70 | 62 131 / 62 969 (255) |
| inlinks_by_url (W10) | S2 | 31 397 | 4.1 MB | 108 | 30 841 / 31 397 (139) |
| fetch_attempts_by_url (W3) | S4b | 44 640 | 2.8 MB | 62 | unsharded |
| targets (P2) | S8 | 10 000 | 2.4 MB | 228 | unsharded |
| links_by_page_version (W9) | S5 | 10 000 | 1.6 MB | 143 | unsharded |
| media_observations (M2) | S7 | 1 000 | 0.8 MB | ~800 | unsharded |
| all single-row tables (W1, W5, W7, W12, W13, M1, E1, E2, P1, X3, …) | — | 1 | < 1 MB | — | — |

**Headroom**: the largest partition (W6) is at 43 % of the gate *with one
observation per minute for a whole month* — the frontier (P3) must not
recrawl a URL more often than about every 30 s (≈ 100 MB/month). The outbox
stays below the gate up to ≈ 24 000 events/s cluster-wide (100 MB ÷ 1 311 B ×
32 shards ÷ 60 s); raise `OUTBOX_SHARDS` beyond that.

**Model changes the benchmark forced** (made before the recorded run):
`evidence_by_target` gained a 16-way shard (unsharded it would hold 1M
rows ≈ 70 MB for a hot target) and the outbox went from 8 to 32 shards
(8 shards would reach ≈ 100 MB at ≈ 6 000 events/s).

**First-run finding (fixed in the benchmark, not the model):** the first
run reported `media_observations` at 108 MB because scenario S3b attached
all 125 000 mirror sightings to *one* page observation — impossible under
the contract (`media.discovered` carries ≤ 1 000 references per page
observation and is idempotent per page observation). S3b now puts each
mirror on its own page; M2's contractual worst case is S7 (0.8 MB).

Tooling notes: S6b (LWT evidence: 3 Paxos rounds per item) took 46 min
for 63k items on this disk; the whole run takes ≈ 50 min.


## 2. Latency at 10× the expected per-host load

### Load definition

- **Expected per-host load: 10 fetches/s.** V1 did 1.02 pages/s per host
  (P0 baseline, politeness-bound); V2's per-host budget of 64 HTTP slots at
  a ~5 s fetch+politeness cycle gives ≈ 13/s; 10/s is the round figure.
- **10× = 100 "fetch units"/s per host**, both simulated hosts at once
  (200 units/s against the single dev node). One unit = the storage work of
  one fetch (P1 frequencies): W5 read, 2× W12 read, attempt record (+event),
  32 KiB snapshot put, observation record (+event), 4 discovered URLs,
  consumer dedupe check+mark; 50 %: 20-link batch (+event); 30 %: media
  observation (+event), M5 and P1 lookups; 10 %: outbox append; 5 %: W7
  strong read, object HEAD and GET. ≈ 40 CQL statements per unit.
- Open-loop schedule; all contract objects are built before the timed
  phase; generators run in throwaway client containers (4 CPUs, 4
  processes each) so client CPU is not measured as storage latency.
  A relay publishes the outbox to Redis concurrently.

### p99 targets

Point reads/upserts (W5, W7, W12, P1, X3 marker, outbox append) ≤ 25 ms;
fan-out reads and small multi-row writes (M5 8-way, discovered URLs,
attempt record) ≤ 50 ms; observation/media records ≤ 100 ms; 20-link
batch ≤ 150 ms; object PUT 32 KiB ≤ 250 ms, HEAD ≤ 50 ms, GET ≤ 100 ms;
outbox publication lag p99 ≤ 5 s. Rationale: a fetch takes ~1 s or more,
so storage p99 must stay a small fraction of it.

### Results — 10× gate **NOT met on this host**

| | 1× expected (10 units/s/host) `20260927T231958Z` | 10× (100 units/s/host) `20260927T232213Z` |
|---|---|---|
| achieved | 10.1 units/s per host | **25 units/s per host** (load not sustained) |
| Scylla point reads p50 / p99 | 0.4–0.9 ms / 1.6–148 ms | 135–205 ms / 690–920 ms |
| attempt / observation record p50 / p99 | 1.2–2.2 ms / 27–232 ms | 850–950 ms / 2.5–2.9 s |
| 20-link batch p50 / p99 | 3.3 ms / 119–147 ms | 1.2 s / 3.4–3.5 s |
| object PUT 32 KiB p50 / p99 (MinIO) | 30 ms / 423–630 ms | 520 ms / 2.2 s; ~100 PUT timeouts per host |
| object HEAD / GET p99 (MinIO) | 3.5–4.9 ms / 2.6–3.3 ms | 310–430 ms / 220–300 ms |
| publication lag p50 / p99 | 0.6 s / 1.8 s | 106 s / 237 s |
| disk `sda` utilisation (iostat) | 57–82 % | **99–100 %**, queue ≈ 2, read await 20–27 ms |
| CPU (docker stats) | — | Scylla ≈ 20 % of a core, clients ≈ 40 % of 4 CPUs |

At 1× the medians are sub-millisecond to 3 ms and most point operations
meet their p99 targets; p99 tails of writes (27–232 ms) already come from
disk stalls. At 10× nothing is CPU-bound, the disk is saturated, and every
p99 target is missed. Full per-operation tables: `latency.md` in each
result directory.

**Cause (measured, not assumed):** Docker's data root is on `sda`, a
5 400-rpm WD laptop HDD attached over USB (`lsblk`: ROTA=1, TRAN=usb),
≈ 150–180 IOPS. Scylla is designed for SSD/NVMe (the dev node runs in
developer mode without an I/O profile). The machine also has an NVMe
drive that Docker does not use.

**Consequence for the exit gate:** the "p99 at 10× expected per-host
load" criterion is **unmet on the current dev host** and remains open.
It is an environment limitation, not a data-model or code limit (CPU idle,
partition sizes small, 1× medians sub-ms), but that claim is only proven
once re-run on SSD/NVMe storage: move Docker's data root (or the
`scylla-data`/`minio-data` volumes) to the NVMe and re-run
`benchmarks/p2-storage/run.sh latency`, or run it on the P14 cluster.


## 3. What these benchmarks do not show

- RF=3 behaviour, cross-node latency, node loss: no multi-node environment
  (P14 entry criterion in the plan).
- Long-run compaction/tombstone behaviour of TTL tables (needs days).
- Production-grade client throughput: generators are Python; workers in P4
  will measure their own storage overhead.
