"""P8 two-host drivers (run by scripts/test-media-two-host.sh in app-host-1 and app-host-2).

    python -m tests.integration.media_multihost resolve-setup  --keyspace KS --run RUN
    python -m tests.integration.media_multihost resolve        --keyspace KS --run RUN [--die-after]
    python -m tests.integration.media_multihost resolve-verify --keyspace KS --run RUN

C-11 (design §10.1): both hosts run a resolver with a short real lease (Redis TIME).
host-1 owns the lease and dies (``os._exit``) between a claim and its commit, mid-bucket;
host-2 stands by, takes over after the lease expires, commits host-1's stored envelope
and finishes. The verifier checks the invariant for every reference and that both hosts
decided claims (a handover inside the run).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import redis
from antipiracy_contracts.events import decode_event_as
from antipiracy_contracts.events.media import MediaObserved
from antipiracy_contracts.ids import EventId, MediaId
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration import Settings
from crawler2.frontier.redis import connect_redis
from crawler2.media.cli import producer_for
from crawler2.media.model import ReaderState
from crawler2.media.resolver import Resolver, ResolverConfig
from crawler2.media.store import ScyllaRegistryStore
from crawler2.media.work import RedisWork
from crawler2.storage.events.idempotency import idempotency_key
from crawler2.storage.layout import OUTBOX_SHARDS, bucket_start, outbox_bucket
from crawler2.storage.scylla import Migrator, ScyllaSession, ScyllaStorage
from crawler2.storage.scylla.session import Consistency
from tests.fixtures.contracts import T0, page_observation
from tests.fixtures.media import discovered, reference

EVENTS = 30
REFS = 4
BUCKETS = 3
READER = "mh-resolver"


def _open(settings: Settings, keyspace: str) -> ScyllaStorage:
    return ScyllaStorage.open(settings.scylla, keyspace=keyspace, instance=settings.host_id)


def _meta(client: redis.Redis, run: str) -> dict[str, Any]:
    raw = client.get(f"p8mh:{run}:meta")
    if raw is None:
        raise SystemExit("run not set up")
    doc: dict[str, Any] = json.loads(raw)
    return doc


def _pages(run: str) -> list[Any]:
    return [
        page_observation(UrlRef.of(f"https://site.example/{run}/{i}"), at=T0, seed=f"{run}-{i}")
        for i in range(EVENTS)
    ]


def _refs(run: str, i: int) -> list[Any]:
    return [reference(f"https://cdn.example/{run}/{i}-{j}.mp4") for j in range(REFS)]


def cmd_resolve_setup(settings: Settings, args: argparse.Namespace) -> None:
    with ScyllaSession.connect(settings.scylla, keyspace=args.keyspace) as session:
        session.execute_raw(f"DROP KEYSPACE IF EXISTS {args.keyspace}")
        Migrator(session, settings.scylla, applied_by=settings.host_id).migrate()
    storage = _open(settings, args.keyspace)
    client = connect_redis(settings.redis)
    now = RedisWork(client, namespace="p8mh").now()
    first = outbox_bucket(now - timedelta(minutes=BUCKETS + 2))
    for i, page in enumerate(_pages(args.run)):
        at = bucket_start(first + i % BUCKETS) + timedelta(seconds=5)
        storage.session.execute(
            storage.outbox.statement(discovered(page, _refs(args.run, i)), now=at)
        )
    store = ScyllaRegistryStore(storage.session)
    assert store.create_state(
        ReaderState(
            reader=READER,
            epoch=1,
            owner=None,
            lease_until=now - timedelta(hours=1),
            next_bucket=first,
            started_bucket=first,
        ),
        at=now,
        by="mh-setup",
    )
    client.set(
        f"p8mh:{args.run}:meta",
        json.dumps({"first": first, "started": now.isoformat()}),
        ex=3600,
    )
    print(json.dumps({"setup": True, "first_bucket": first, "events": EVENTS * REFS}))


def cmd_resolve(settings: Settings, args: argparse.Namespace) -> None:
    storage = _open(settings, args.keyspace)
    client = connect_redis(settings.redis)
    meta = _meta(client, args.run)
    end = meta["first"] + BUCKETS
    work = RedisWork(client, namespace="p8mh")
    identity = f"{settings.host_id}:media_probe:{os.getpid()}:{os.urandom(16).hex()}"
    seen = {"refs": 0}

    def hook(point: str, **context: Any) -> None:
        if point == "after_claim":
            seen["refs"] += 1
            time.sleep(0.05)  # slow enough that a lease handover happens mid-bucket
            if args.die_after and seen["refs"] >= args.die_after:
                print(json.dumps({"host": settings.host_id, "died_after": seen["refs"]}))
                sys.stdout.flush()
                os._exit(137)  # crash between claim and commit, lease still held

    resolver = Resolver(
        storage.outbox,
        ScyllaRegistryStore(storage.session),
        storage.media,
        producer=producer_for(identity),
        config=ResolverConfig(
            reader=READER,
            lease_ttl=timedelta(seconds=args.lease_s),
            settle=timedelta(0),
            max_buckets_per_cycle=1,
        ),
        clock=work.now,
        hook=hook,
    )
    deadline = time.monotonic() + args.max_s
    acquired = buckets = standby = 0
    while time.monotonic() < deadline:
        report = resolver.run_once()
        acquired += report.acquired
        buckets += report.buckets
        standby += report.standby
        state = ScyllaRegistryStore(storage.session).state(READER)
        if state is not None and state.next_bucket >= end:
            break
        if report.standby or not report.buckets:
            time.sleep(0.5)
    print(
        json.dumps(
            {
                "host": settings.host_id,
                "acquired": acquired,
                "buckets": buckets,
                "standby_cycles": standby,
                "claims_seen": seen["refs"],
            }
        )
    )


def cmd_resolve_verify(settings: Settings, args: argparse.Namespace) -> None:
    storage = _open(settings, args.keyspace)
    client = connect_redis(settings.redis)
    meta = _meta(client, args.run)
    store = ScyllaRegistryStore(storage.session)
    state = store.state(READER)
    assert state is not None
    assert state.next_bucket >= meta["first"] + BUCKETS, state
    assert state.epoch >= 3, f"no handover: epoch {state.epoch}"
    started = datetime.fromisoformat(meta["started"])
    keys: dict[str, str] = {}
    deciders: set[str] = set()
    pages = _pages(args.run)
    for i, page in enumerate(pages):
        for ref in _refs(args.run, i):
            media_id = MediaId.of_locator(ref.locator.url)
            claim = store.claim(page.observation_id, media_id)
            assert claim is not None, (page.observation_id, media_id)
            assert claim.committed
            deciders.add(str(claim.claimed_by).split(":")[0])
            decided = decode_event_as(claim.envelope, MediaObserved)
            assert storage.media.observation(page.observation_id, media_id) == (
                decided.payload.observation
            )
            keys[idempotency_key(decided)] = claim.envelope
    rows = _observed_rows(storage, started)
    by_key: dict[str, set[str]] = {}
    physical = 0
    for r in rows:
        if r.idempotency_key in keys:
            physical += 1
            by_key.setdefault(r.idempotency_key, set()).add(str(EventId.from_uuid(r.event_id)))
            assert r.envelope == keys[r.idempotency_key], "a copy differs from the stored claim"
    assert set(by_key) == set(keys), "an observation has no outbox row"
    assert all(len(ids) == 1 for ids in by_key.values()), "two event_ids for one observation"
    assert deciders == {"host-1", "host-2"}, f"no mid-run handover: {deciders}"
    print(
        json.dumps(
            {
                "verified": True,
                "observations": len(keys),
                "physical_outbox_rows": physical,
                "epoch": state.epoch,
                "deciders": sorted(deciders),
            }
        )
    )


def _observed_rows(storage: ScyllaStorage, started: datetime) -> list[Any]:
    first = outbox_bucket(started - timedelta(minutes=2))
    last = outbox_bucket(datetime.now(UTC) + timedelta(minutes=2))
    results = storage.session.execute_many(
        storage.session.bind(
            "SELECT event_id, idempotency_key, envelope FROM {ks}.outbox "
            "WHERE shard = ? AND bucket = ?",
            Consistency.STRONG_READ,
            (shard, bucket),
        )
        for bucket in range(first, last + 1)
        for shard in range(OUTBOX_SHARDS)
    )
    return [r for rows in results for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media_multihost")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (
        ("resolve-setup", cmd_resolve_setup),
        ("resolve", cmd_resolve),
        ("resolve-verify", cmd_resolve_verify),
    ):
        p = sub.add_parser(name)
        p.add_argument("--keyspace", required=True)
        p.add_argument("--run", required=True)
        p.set_defaults(func=fn)
        if name == "resolve":
            p.add_argument("--die-after", type=int, default=0)
            p.add_argument("--lease-s", type=float, default=4.0)
            p.add_argument("--max-s", type=float, default=180.0)
    args = parser.parse_args(argv)
    args.func(Settings(), args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
