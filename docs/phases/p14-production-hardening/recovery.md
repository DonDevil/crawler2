# Backup, restore, rebuild and component recovery

Status: **tooling implemented and unit-tested; one local single-node restore REHEARSAL passed. No
production restore drill (M6) was run. RPO/RTO are undecided.**

## 1. Backup  {#backup}

`crawler2-backup` (`crawler2/ops/`), one backup set = one directory:

```
crawler2-backup create /backups/2026-10-22 --scylla-container crawler2-scylla-1 \
    --keyspace crawler2 --bucket crawler2-raw [--keyspace fingerprinter] [--redis-container …] --repo /opt/crawler2
crawler2-backup verify /backups/2026-10-22
```

| Store | What is copied | How |
|---|---|---|
| Scylla (per keyspace) | schema (`DESCRIBE`) + flushed snapshot SSTables | Scylla REST API (`keyspace_flush`, `snapshots`) through `docker exec` or on the node itself — *not* `nodetool`, whose native binary needs AIO budget the host does not have |
| MinIO (per bucket) | every object, listed and read through the S3 API; **each is re-hashed against its content-addressed key** | stdlib signed requests |
| Redis (optional) | `dump.rdb` after `BGSAVE` | recovery policy undecided (OD-P14-5): recorded, **never auto-restored** |
| Provenance | git commits, environment, tool version, RPO/RTO fields (empty) | `MANIFEST.json` |

`MANIFEST.json` lists every file with its SHA-256. `verify` re-reads **everything** and reports changed,
missing and unexpected files; for MinIO it re-checks each object against its key and then **cross-checks
the stores**: every artefact a backed-up evidence manifest names must be in the set with the same hash.
A backup set is written once into an empty directory. `crawler2-backup prune ROOT --keep N` lists what it
would delete and deletes only with `--yes`.

**Schedule, retention, RPO/RTO: owner decisions (OD-P14-5).** The tool provides the mechanism; nothing
runs on a schedule. The frontier in Redis is the open question: what must be restored vs rebuilt.

## 2. Restore  {#restore}

```
crawler2-backup restore /backups/2026-10-22 --scylla-container crawler2-scylla-1 \
    --keyspace-map crawler2=crawler2_restored --bucket-map crawler2-raw=crawler2-raw-restored
```
Verifies the set first and refuses if it does not verify. Writes only to **new** names, refuses a target
that exists or holds data, and refuses same-name restores. Scylla: creates the keyspace from the saved
schema with the new name, copies SSTables into the live table's `upload/` and loads them via the REST API.
MinIO: re-puts each object with its expected digest. Switching the application to the restored names (or
renaming) is a deliberate manual step.

**Order for a full recovery (proposal):** Scylla → MinIO → verify evidence hashes (`crawler2-evidence
validate`) → Redis derived state rebuilt (§3) → relay/consumers → worker pools last.

### Local rehearsal (not the M6 drill)
`scripts/backup/rehearsal.py` creates a throwaway keyspace/bucket in the dev stack, backs it up, **flips a
byte and confirms `verify` fails**, restores into new names, compares 50 rows and 20 objects, confirms a
second restore is refused, and removes everything it made. Result in [validation.md](validation.md) §5.
One node, RF=1, synthetic rows, no real evidence, no RTO measurement: a rehearsal of the tooling only.

## 3. Rebuilding derived state  {#rebuild}

| Derived state | Source | Command | Drilled? |
|---|---|---|---|
| Media read models | `media.discovered` outbox | `crawler2-media rebuild` | P8 tests |
| Source-health projection | outbox facts | `crawler2-intel sources rebuild` | P7 tests |
| Feedback evidence | match projections | `crawler2-intel feedback rebuild` | P11 tests |
| Evidence candidates | projections | `crawler2-evidence collect rebuild` | P12 tests |
| Analytics Parquet / graph | Scylla/MinIO | `crawler2-analytics export` (revisioned) | P13 synthetic + throwaway keyspace |
| Vector index | fingerprinter representations | fp `index` commands (drop/rebuild) | P10 single host |
| **F5/F6 (`url_admission`, `filter_decisions_by_url`)** | replay of `urls.discovered` | **manual only** — run `crawler2-discover admit` with a *new* `CRAWLER2_DISCOVERY__CONSUMER_GROUP` over the retained stream; retention (ADR-022) bounds how far back it reaches. **Not automated, not drilled** (TC-034 open) | no |
| Frontier (Redis) | admissions in Scylla / re-seeding | no tool; recovery model undecided (OD-P14-5) | no |

## 4. Component recovery

### Redis  {#redis}
Lost or restarted with AOF intact: restart; leases lapse and `recover()` reschedules (P3 proof: 1M claims, random
kills, 0 lost/0 duplicate). **Memory full** (M1 run 2): writes fail, the relay exits repeatedly; free space by
fixing consumers (`ConsumerStalled`), never by `FLUSHALL`; retention is byte-bounded since ADR-022. AOF lost
entirely: the frontier is empty and the streams are gone. The Scylla outbox keeps every event and
`OutboxRelay.replay(since, until)` (`crawler2/storage/events/relay.py`) can re-publish a window, but **no CLI
exposes it** (gap RB-1) and consumers' dedupe is by event id, so a replay is safe but unscripted; re-seed the frontier. No HA/failover exists (single primary).

### Scylla  {#scylla}
Single node (dev): restart; data is on the volume. Multi-node RF=3: **never run** (TC-010…019). One node down
with RF=3 is the designed case (LOCAL_QUORUM continues) but unproven. Loss of the node/volume: restore (§2).

### MinIO  {#minio}
Objects are content-addressed and written with `If-None-Match`; a restart loses nothing. Single-node MinIO
disk loss loses raw snapshots and evidence artefacts → restore from backup (§2) and run
`crawler2-evidence validate` on every finalized item. Distributed mode is undecided (TC-043).

### Worker / consumer processes
Stateless by design (ADR-006). Delete scratch and restart: claims return after the lease, events are
redelivered and ignored if already applied.
