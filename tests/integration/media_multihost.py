"""P8 two-host drivers (run by scripts/test-media-two-host.sh in app-host-1 and app-host-2).

    python -m tests.integration.media_multihost resolve-setup  --keyspace KS --run RUN
    python -m tests.integration.media_multihost resolve        --keyspace KS --run RUN [--die-after]
    python -m tests.integration.media_multihost resolve-verify --keyspace KS --run RUN
    python -m tests.integration.media_multihost serve-media    --keyspace KS --run RUN
    python -m tests.integration.media_multihost workers        --keyspace KS --run RUN
    python -m tests.integration.media_multihost workers-verify --keyspace KS --run RUN

C-11 (design §10.1): both hosts run a resolver with a short real lease (Redis TIME).
host-1 owns the lease and dies (``os._exit``) between a claim and its commit, mid-bucket;
host-2 stands by, takes over after the lease expires, commits host-1's stored envelope
and finishes. The verifier checks the invariant for every reference and that both hosts
decided claims (a handover inside the run).

Probe/hash workers: host-1 serves a fixture media web (``serve-media``, port 18080,
reached as ``app-host-1`` through the SSRF guard with the compose subnet as the
explicit ``ssrf_allowed_networks`` opt-out). Both hosts run probe and hash pools on
the same queues at once; the verifier checks one probe and one hash per media,
work done by both hosts, and one ContentId per byte-identical group.
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
from antipiracy_contracts.urls import CanonicalUrl

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


MEDIA_PORT = 18080
GROUPS = 6
COPIES = 3


def _bodies(run: str) -> dict[str, bytes]:
    """GROUPS distinct bodies, each served at COPIES locators."""
    from tests.fixtures.mediaweb import blob

    return {
        f"/{run}/g{g}/c{c}.mp4": blob(f"{run}-g{g}", 256 * 1024 + g * 1000)
        for g in range(GROUPS)
        for c in range(COPIES)
    }


def cmd_serve_media(settings: Settings, args: argparse.Namespace) -> None:
    from tests.fixtures.mediaweb import media_site

    client = connect_redis(settings.redis)
    with media_site("0.0.0.0", MEDIA_PORT) as site:  # noqa: S104 -- compose network only
        for path, body in _bodies(args.run).items():
            site.put(path, body, stall_s=0.02)
        client.set(f"p8mh:{args.run}:media-up", "1", ex=600)
        print(json.dumps({"serving": len(_bodies(args.run)), "port": MEDIA_PORT}))
        sys.stdout.flush()
        while client.get(f"p8mh:{args.run}:media-stop") is None:
            time.sleep(0.5)


def _media_settings(settings: Settings) -> Any:
    return settings.media.model_copy(
        update={
            "ssrf_allowed_networks": ["172.16.0.0/12"],  # the compose bridge network
            "probe_per_host_interval_s": 0.0,
            "hash_per_host_concurrency": 4,
        }
    )


def _url(run: str, path: str) -> CanonicalUrl:
    return UrlRef.of(f"http://app-host-1:{MEDIA_PORT}{path}").url


def cmd_workers(settings: Settings, args: argparse.Namespace) -> None:
    import asyncio

    from crawler2.media.hashing import FullContentHasher
    from crawler2.media.probe import (
        HashWorker,
        ProbeWorker,
        guarded,
        hash_limits,
        probe_fetcher,
        run_pool,
    )
    from crawler2.media.work import Job

    storage = _open(settings, args.keyspace)
    client = connect_redis(settings.redis)
    deadline = time.monotonic() + 60
    while client.get(f"p8mh:{args.run}:media-up") is None:
        if time.monotonic() > deadline:
            raise SystemExit("media server never came up")
        time.sleep(0.5)
    work = RedisWork(client, namespace=f"p8mh-{args.run}")
    media = _media_settings(settings)
    store = ScyllaRegistryStore(storage.session)
    identity = f"{settings.host_id}:media_probe:{os.getpid()}:{os.urandom(16).hex()}"
    if client.incr(f"p8mh:{args.run}:arrived") == 2:  # start both hosts together
        client.rpush(f"p8mh:{args.run}:go", "go", "go")
    if client.blpop([f"p8mh:{args.run}:go"], timeout=120) is None:
        raise SystemExit("barrier timed out: the other host never arrived")
    for path in _bodies(args.run):  # both hosts want every media: Redis dedupes
        url = _url(args.run, path)
        work.want("probe", Job(str(MediaId.of_locator(url)), url, "video_file"))
    end = time.monotonic() + args.max_s
    counts = {"probes": 0, "hashes": 0}

    def stop() -> bool:
        if time.monotonic() > end:
            return True
        key = f"p8mh:{args.run}:idle:{settings.host_id}"
        if work.depth("probe") or work.depth("hash"):
            client.delete(key)
            return False
        return int(client.incr(key)) > 40

    async def main() -> None:
        fetcher = probe_fetcher(settings.workers.fetch, guarded(media), connections=8)
        hasher = FullContentHasher(guarded(media), hash_limits(media, settings.workers.fetch))
        probe = ProbeWorker(fetcher, store, work, worker=identity, settings=media)
        hashw = HashWorker(hasher, store, work, worker=identity, settings=media)

        async def counted_probe(job: Job) -> None:
            if await probe.handle(job) is not None:
                counts["probes"] += 1

        async def counted_hash(job: Job) -> None:
            if await hashw.handle(job) is not None:
                counts["hashes"] += 1

        try:
            await asyncio.gather(
                run_pool("probe", work, counted_probe, concurrency=4, stop=stop, block_s=0.5),
                run_pool("hash", work, counted_hash, concurrency=2, stop=stop, block_s=0.5),
            )
        finally:
            await fetcher.close()
            await hasher.close()

    asyncio.run(main())
    print(json.dumps({"host": settings.host_id} | counts))


def cmd_workers_verify(settings: Settings, args: argparse.Namespace) -> None:
    storage = _open(settings, args.keyspace)
    client = connect_redis(settings.redis)
    store = ScyllaRegistryStore(storage.session)
    window = {
        "since": datetime.now(UTC) - timedelta(days=1),
        "until": datetime.now(UTC) + timedelta(days=1),
    }
    hosts: set[str] = set()
    groups: dict[str, set[str]] = {}
    for path in _bodies(args.run):
        media = MediaId.of_locator(_url(args.run, path))
        probes = store.probes(media, **window)
        hashes = store.hashes(media, **window)
        assert len(probes) == 1, (path, len(probes))
        assert len(hashes) == 1, (path, len(hashes))
        assert hashes[0].outcome.value == "ok", hashes[0]
        hosts |= {probes[0].worker.split(":")[0], hashes[0].worker.split(":")[0]}
        group = path.split("/")[2]
        groups.setdefault(group, set()).add(str(hashes[0].content_id))
    assert all(len(ids) == 1 for ids in groups.values()), groups  # byte-identical → one id
    distinct = {next(iter(ids)) for ids in groups.values()}
    assert len(distinct) == GROUPS  # different bytes → different ids
    assert hosts == {"host-1", "host-2"}, f"work done by {hosts}"
    client.set(f"p8mh:{args.run}:media-stop", "1", ex=600)
    print(
        json.dumps(
            {
                "verified": True,
                "media": len(_bodies(args.run)),
                "content_ids": len(distinct),
                "hosts": sorted(hosts),
            }
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media_multihost")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (
        ("resolve-setup", cmd_resolve_setup),
        ("resolve", cmd_resolve),
        ("resolve-verify", cmd_resolve_verify),
        ("serve-media", cmd_serve_media),
        ("workers", cmd_workers),
        ("workers-verify", cmd_workers_verify),
    ):
        p = sub.add_parser(name)
        p.add_argument("--keyspace", required=True)
        p.add_argument("--run", required=True)
        p.set_defaults(func=fn)
        if name == "resolve":
            p.add_argument("--die-after", type=int, default=0)
            p.add_argument("--lease-s", type=float, default=4.0)
            p.add_argument("--max-s", type=float, default=180.0)
        if name == "workers":
            p.add_argument("--max-s", type=float, default=240.0)
    args = parser.parse_args(argv)
    args.func(Settings(), args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
