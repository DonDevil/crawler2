# What P2 built

Rationale: [design.md](design.md), ADR-012…014. Tables: [schema.md](schema.md).
Evidence it works: [validation.md](validation.md), [benchmarks.md](benchmarks.md).

## Code (`crawler2/storage/`)

| Module | Contents |
|---|---|
| `repositories.py` | 7 repository `Protocol`s + read-model records (no driver types) |
| `layout.py` | shard counts, day/month/minute buckets, `latest_wins` / `earliest_wins` / `version_wins` |
| `errors.py` | `StorageError` → `StorageUnavailableError`, `IntegrityError`, `ConflictError`, `SchemaError` |
| `scylla/session.py` | connection, prepared statements, named consistency levels, logged/partition batches, bounded concurrency, paging |
| `scylla/cql/V001__initial_schema.cql` | the 29 crawler2 tables |
| `scylla/migrations.py` | loader (ordering, checksums, destructive-DDL refusal), `Migrator` (keyspace bootstrap without tablets, LWT lease, history, status, `require_current`) |
| `scylla/web.py`, `media.py`, `projections.py`, `evidence.py` | repository implementations, `rebuild_url`, `rebuild_media` |
| `scylla/outbox.py` | `ScyllaOutbox` (outbox rows, relay store), `ScyllaProcessedEventStore` |
| `scylla/__init__.py` | `ScyllaStorage` (all repositories over one session) |
| `objectstore/` | `ObjectStore` interface, content-addressed layout, stdlib SigV4 `S3ObjectStore` |
| `events/idempotency.py` | catalog-driven idempotency keys |
| `events/publisher.py` | `EventPublisher`, `RedisStreamPublisher`, stream naming |
| `events/relay.py` | `OutboxRelay` (cycle, checkpoints, sweep, replay) |
| `events/consumer.py` | `IdempotentConsumer`, `RedisStreamReader` |
| `cli.py` | `crawler2-storage migrate | status | check | relay` |

## Commands

```bash
docker compose exec app crawler2-storage migrate     # keyspace + tables + raw bucket (idempotent)
docker compose exec app crawler2-storage status      # JSON: version, applied, pending, problems
docker compose exec app crawler2-storage check       # exit 1 unless current
docker compose exec app crawler2-storage relay       # outbox → Redis Streams
benchmarks/p2-storage/run.sh partitions|latency      # two-host profile
```

## Other changes

| File | Change |
|---|---|
| `crawler2/core/configuration/settings.py` | `ScyllaSettings.request_timeout_s`; MinIO timeouts/limits/spool; new `EventSettings` |
| `pyproject.toml` | `crawler2-storage` script; mypy covers `benchmarks/p2-storage`; S608 ignored for constant CQL |
| `docker/app/Dockerfile`, `.dockerignore` | benchmark scripts copied into the image (run where Scylla is reachable) |
| `scripts/validate-stack.sh` | P2 steps: concurrent migrate, concurrent writers, crash + Scylla restart recovery |
| `tests/fixtures/contracts.py` | deterministic contract builders (tests + benchmarks) |
| `tests/unit/storage/` | 31 unit tests |
| `tests/integration/storage/` | 29 integration tests (real Scylla/MinIO/Redis) |
| `tests/integration/multihost.py` | two-host writer/verifier and crash drivers |
| `docs/adr/ADR-012…014`, index | storage model, outbox, object layout |

No dependency was added (`uv.lock` unchanged); Docker Compose topology
unchanged. Nothing of P3+ exists: no frontier, scheduling, leases, workers,
fetching, extraction, probing, encoding, matching, feedback or evidence
policy.
