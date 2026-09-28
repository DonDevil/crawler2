"""``crawler2-storage``: schema bootstrap/migrations, status, and the outbox relay.

    crawler2-storage migrate [--keyspace KS]   # create keyspace/tables, apply pending, raw bucket
    crawler2-storage status  [--keyspace KS]   # JSON: version, applied, pending, problems
    crawler2-storage check   [--keyspace KS]   # exit 1 unless the schema is current
    crawler2-storage relay [--once] [--sweep-hours H] [--replay-from ISO --replay-to ISO]

Schema changes happen only through ``migrate`` — never implicitly when a
process opens a repository (repositories call ``require_current``).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import redis

from crawler2.core.configuration import Settings
from crawler2.core.observability import Metrics, configure_logging, get_logger
from crawler2.storage.events.publisher import RedisStreamPublisher
from crawler2.storage.events.relay import OutboxRelay
from crawler2.storage.objectstore import S3ObjectStore
from crawler2.storage.scylla import Migrator, ScyllaSession, ScyllaStorage


def _instance(settings: Settings, role: str) -> str:
    return f"{settings.host_id}:{role}:{os.getpid()}"


def _status(settings: Settings, keyspace: str | None) -> tuple[dict[str, object], bool]:
    with ScyllaSession.connect(settings.scylla, keyspace=keyspace) as session:
        migrator = Migrator(session, settings.scylla, applied_by=_instance(settings, "storage"))
        status = migrator.status()
        report: dict[str, object] = {
            "keyspace": status.keyspace,
            "keyspace_exists": status.keyspace_exists,
            "current_version": status.current_version,
            "target_version": len(migrator.migrations),
            "applied": [
                {
                    "version": m.version,
                    "name": m.name,
                    "applied_at": m.applied_at.isoformat(),
                    "applied_by": m.applied_by,
                }
                for m in status.applied
            ],
            "pending": [f"V{m.version:03d}__{m.name}" for m in status.pending],
            "problems": list(status.problems),
            "up_to_date": status.up_to_date,
        }
        return report, status.up_to_date


def cmd_migrate(settings: Settings, args: argparse.Namespace) -> int:
    log = get_logger("storage.migrate")
    with ScyllaSession.connect(settings.scylla, keyspace=args.keyspace) as session:
        migrator = Migrator(session, settings.scylla, applied_by=_instance(settings, "storage"))
        applied = migrator.migrate(lock_wait_s=args.lock_wait)
        log.info("schema_migrated", keyspace=session.keyspace, applied=list(applied))
    if not args.skip_bucket:
        store = S3ObjectStore(settings.minio, scratch_dir=settings.scratch_dir)
        created = store.ensure_bucket()
        store.close()
        log.info("bucket_ready", bucket=settings.minio.bucket_raw, created=created)
    return 0


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    report, up_to_date = _status(settings, args.keyspace)
    print(json.dumps(report, indent=2))
    return 0 if up_to_date or args.command == "status" else 1


def cmd_relay(settings: Settings, args: argparse.Namespace) -> int:
    log = get_logger("storage.relay")
    metrics = Metrics(settings)
    storage = ScyllaStorage.open(
        settings.scylla, keyspace=args.keyspace, instance=_instance(settings, "relay")
    )
    cfg = settings.redis
    client = redis.Redis(
        host=cfg.host,
        port=cfg.port,
        db=cfg.db,
        password=cfg.password.get_secret_value() if cfg.password else None,
        socket_timeout=cfg.socket_timeout_s,
    )
    relay = OutboxRelay(
        storage.outbox,
        RedisStreamPublisher(client, settings.events),
        relay_id=_instance(settings, "relay"),
        settle_s=settings.events.relay_settle_s,
        batch_size=settings.events.relay_batch_size,
        metrics=metrics,
    )
    try:
        now = datetime.now(UTC)
        if args.replay_from:
            until = datetime.fromisoformat(args.replay_to) if args.replay_to else now
            report = relay.replay(since=datetime.fromisoformat(args.replay_from), until=until)
            log.info("outbox_replayed", published=report.published)
            return 0
        if args.sweep_hours:
            report = relay.sweep(since=now - timedelta(hours=args.sweep_hours))
            log.info("outbox_swept", late=report.late)
        if args.once:
            report = relay.run_once()
            log.info("outbox_relayed", published=report.published)
            return 0
        stopping = False

        def _stop(*_: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        metrics.serve()
        relay.run_forever(
            poll_interval_s=settings.events.relay_poll_interval_s, stop=lambda: stopping
        )
        return 0
    finally:
        client.close()
        storage.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-storage", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("migrate", "status", "check", "relay"):
        p = sub.add_parser(name)
        p.add_argument("--keyspace", default=None, help="override CRAWLER2_SCYLLA__KEYSPACE")
    sub.choices["migrate"].add_argument("--lock-wait", type=float, default=60.0)
    sub.choices["migrate"].add_argument("--skip-bucket", action="store_true")
    relay = sub.choices["relay"]
    relay.add_argument("--once", action="store_true", help="one relay cycle, then exit")
    relay.add_argument("--sweep-hours", type=float, default=0.0)
    relay.add_argument("--replay-from", default=None, help="ISO timestamp")
    relay.add_argument("--replay-to", default=None, help="ISO timestamp (default: now)")
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings, role="storage")
    handlers = {
        "migrate": cmd_migrate,
        "status": cmd_status,
        "check": cmd_status,
        "relay": cmd_relay,
    }
    return handlers[args.command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
