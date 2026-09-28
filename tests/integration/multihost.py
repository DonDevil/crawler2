"""Two-host drivers (run by scripts/validate-stack.sh in app-host-1 and app-host-2).

    python -m tests.integration.multihost reset  --keyspace KS
    python -m tests.integration.multihost write  --keyspace KS --run RUN   # on both hosts at once
    python -m tests.integration.multihost verify --keyspace KS --run RUN
    python -m tests.integration.multihost crash-produce --keyspace KS --run RUN
    python -m tests.integration.multihost crash-relay   --keyspace KS --run RUN   # dies mid-way
    python -m tests.integration.multihost crash-verify  --keyspace KS --run RUN

Concurrent writers: both hosts write the *same* logical facts (in different
orders), conflicting facts with a defined winner, and race on E1 (LWT). The
verifier checks convergence. Crash: host-1 commits entities + outbox rows,
a relay on host-1 is killed (os._exit) after publishing part of them and
before marking any; Scylla is restarted; host-2's relay must deliver all of
them (duplicates allowed) and an idempotent consumer applies each once.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import redis
from antipiracy_contracts.events import new_event
from antipiracy_contracts.events.web import PageObserved
from antipiracy_contracts.ids import EvidenceId, MatchId
from antipiracy_contracts.models.media import MediaObservation
from antipiracy_contracts.models.targets import Target
from antipiracy_contracts.models.web import FetchAttempt, PageObservation
from antipiracy_contracts.ownership import Component, Producer

from crawler2.core.configuration import Settings
from crawler2.storage.events.consumer import ConsumeOutcome, IdempotentConsumer, RedisStreamReader
from crawler2.storage.events.publisher import OutboundEvent, RedisStreamPublisher, stream_name
from crawler2.storage.events.relay import OutboxRelay
from crawler2.storage.scylla import Migrator, ScyllaSession, ScyllaStorage
from tests.fixtures.contracts import (
    T0,
    content_key,
    fetch_attempt,
    match,
    media,
    media_observation,
    page_observation,
    target,
    url,
)

SHARED = 150
CONFLICTS = 60
RACES = 40
CRASH_EVENTS = 200
CRASH_AFTER = 80


def _redis(settings: Settings) -> redis.Redis:
    return redis.Redis(host=settings.redis.host, port=settings.redis.port, db=settings.redis.db)


def _open(settings: Settings, keyspace: str) -> ScyllaStorage:
    return ScyllaStorage.open(settings.scylla, keyspace=keyspace, instance=settings.host_id)


def _barrier(client: redis.Redis, run: str, parties: int = 2) -> None:
    """Start both hosts together: the last arrival releases everyone (blocking, no sleeps)."""
    if client.incr(f"mh:{run}:arrived") == parties:
        client.rpush(f"mh:{run}:go", *["go"] * parties)
    if client.blpop([f"mh:{run}:go"], timeout=120) is None:
        raise SystemExit("barrier timed out: the other host never arrived")


def cmd_reset(settings: Settings, args: argparse.Namespace) -> None:
    with ScyllaSession.connect(settings.scylla, keyspace=args.keyspace) as session:
        session.execute_raw(f"DROP KEYSPACE IF EXISTS {args.keyspace}")
        Migrator(session, settings.scylla, applied_by=settings.host_id).migrate()


# --- concurrent writers --------------------------------------------------------------


SharedFacts = tuple[list[PageObservation], list[FetchAttempt], list[MediaObservation], list[Target]]


def _shared_facts(run: str) -> SharedFacts:
    pages = [
        page_observation(url(f"{run}/shared/{i}"), at=T0, seed=f"{run}-p{i}") for i in range(SHARED)
    ]
    attempts = [fetch_attempt(p.requested, seed=f"{run}-a{i}") for i, p in enumerate(pages)]
    item = media(f"{run}/m.mp4")
    medias = [media_observation(item, p, content=content_key(run.encode())) for p in pages[:50]]
    versions = [target(run, version=v) for v in range(1, 6)]
    return pages, attempts, medias, versions


def cmd_write(settings: Settings, args: argparse.Namespace) -> None:
    host = settings.host_id
    rng = random.Random(f"{args.run}-{host}")  # noqa: S311 — test ordering, not security
    storage = _open(settings, args.keyspace)
    client = _redis(settings)
    pages, attempts, medias, versions = _shared_facts(args.run)
    work: list[object] = [*pages, *attempts, *medias, *versions]
    rng.shuffle(work)  # each host applies the same facts in a different order
    offset = 1 if host == "host-1" else 2
    conflicts = [
        page_observation(
            url(f"{args.run}/conflict/{i}"),
            at=T0 + timedelta(seconds=offset),
            body=host.encode(),
            seed=f"{args.run}-{host}-c{i}",
        )
        for i in range(CONFLICTS)
    ]
    ref = versions[0].ref
    matches = [match(content_key(f"{args.run}-{i}".encode()).content_id, ref) for i in range(RACES)]
    _barrier(client, args.run)

    producer = Producer(
        service=Component.CRAWLER_WORKER.service,
        component=Component.CRAWLER_WORKER,
        instance=f"{host}:http:{os.getpid()}:{'0' * 32}",
    )
    for fact in work:
        if isinstance(fact, PageObservation):
            event = new_event(PageObserved(observation=fact), producer=producer, occurred_at=T0)
            storage.pages.record(fact, event=event)
        elif isinstance(fact, FetchAttempt):
            storage.fetch_attempts.record(fact)
        elif isinstance(fact, MediaObservation):
            storage.media.record_observation(fact)
        elif isinstance(fact, Target):
            storage.projections.apply_target_registered(fact)
    for observation in conflicts:
        storage.pages.record(observation)
    winners = {
        str(m.match_id): str(storage.evidence.open_candidate(m.match_id, EvidenceId.new(), at=T0))
        for m in matches
    }
    client.set(f"mh:{args.run}:winners:{host}", json.dumps(winners))
    storage.close()
    print(json.dumps({"host": host, "facts": len(work), "races": len(winners)}))


def cmd_verify(settings: Settings, args: argparse.Namespace) -> None:
    storage = _open(settings, args.keyspace)
    client = _redis(settings)
    pages, attempts, medias, versions = _shared_facts(args.run)
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    for p, a in zip(pages, attempts, strict=True):
        history = storage.pages.history(p.requested.url_id, since=T0, until=T0, limit=10)
        check(history == [p], f"history of {p.requested.url} has {len(history)} rows")
        check(len(storage.fetch_attempts.recent_for_url(a.requested.url_id)) == 1, "attempt dup")
    item = medias[0].media.media_id
    sightings = storage.media.sightings(item, since=T0, until=T0, limit=1000)
    check(len(sightings) == len(medias), f"media sightings {len(sightings)} != {len(medias)}")
    state = storage.projections.target(versions[0].ref.target_id)
    check(state is not None and state.target == versions[-1], "highest target version lost")

    for i in range(CONFLICTS):
        ref = url(f"{args.run}/conflict/{i}")
        latest = storage.pages.latest(ref.url_id)
        check(latest is not None and latest.observed_at == T0 + timedelta(seconds=2), "LWW")
        known = storage.urls.get(ref.url_id)
        check(known is not None and known.first_seen == T0 + timedelta(seconds=1), "first_seen")
        check(
            len(storage.pages.history(ref.url_id, since=T0, until=T0 + timedelta(1))) == 2,
            "both obs kept",
        )

    raw: list[Any] = [client.get(f"mh:{args.run}:winners:{h}") for h in ("host-1", "host-2")]
    views: list[dict[str, str]] = [json.loads(v) if v else {} for v in raw]
    check(len(views[0]) == len(views[1]) == RACES, "a host did not finish its races")
    check(views[0] == views[1], "hosts disagree about the evidence id of a match")
    for match_id, evidence_id in views[0].items():
        opened = storage.evidence.open_candidate(MatchId(match_id), EvidenceId.new(), at=T0)
        check(str(opened) == evidence_id, f"E1 changed for {match_id}")

    producers = {
        json.loads(r.envelope)["producer"]["instance"].split(":")[0]
        for r in storage.session.execute_raw(f"SELECT envelope FROM {args.keyspace}.outbox")
    }
    check(producers == {"host-1", "host-2"}, f"outbox producers {producers}")
    storage.close()
    if failures:
        print("\n".join(sorted(set(failures))), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({"converged": True, "shared": SHARED, "conflicts": CONFLICTS, "races": RACES}))


# --- crash between commit and publication ------------------------------------------


def _crash_prefix(run: str) -> str:
    return f"crash-{run}:"


def cmd_crash_produce(settings: Settings, args: argparse.Namespace) -> None:
    storage = _open(settings, args.keyspace)
    producer = Producer(
        service=Component.CRAWLER_WORKER.service,
        component=Component.CRAWLER_WORKER,
        instance=f"{settings.host_id}:http:{os.getpid()}:{'0' * 32}",
    )
    for i in range(CRASH_EVENTS):
        o = page_observation(url(f"{args.run}/crash/{i}"), seed=f"{args.run}-x{i}")
        storage.pages.record(
            o, event=new_event(PageObserved(observation=o), producer=producer, occurred_at=T0)
        )
    storage.close()


class _DieAfter:
    def __init__(self, inner: RedisStreamPublisher, n: int) -> None:
        self.inner, self.left = inner, n

    def publish(self, events: Sequence[OutboundEvent]) -> None:
        self.inner.publish(events[: self.left])
        self.left -= len(events)
        if self.left <= 0:
            os._exit(137)  # a real process death: nothing after this line runs


def cmd_crash_relay(settings: Settings, args: argparse.Namespace) -> None:
    storage = _open(settings, args.keyspace)
    events = settings.events.model_copy(update={"stream_prefix": _crash_prefix(args.run)})
    publisher = RedisStreamPublisher(_redis(settings), events)
    OutboxRelay(
        storage.outbox,
        _DieAfter(publisher, CRASH_AFTER),
        relay_id=settings.host_id,
        settle_s=600,
        batch_size=CRASH_EVENTS,
    ).run_once()
    raise SystemExit("relay was expected to die before finishing")


def cmd_crash_verify(settings: Settings, args: argparse.Namespace) -> None:
    storage = _open(settings, args.keyspace)
    client = _redis(settings)
    events = settings.events.model_copy(update={"stream_prefix": _crash_prefix(args.run)})
    reader = RedisStreamReader(
        client, stream_name(events, "page.observed", 1), "verify", settings.host_id
    )
    reader.ensure_group()
    wires: list[bytes] = []
    while batch := reader.read(count=1000):
        wires += [e.envelope for e in batch]
        reader.ack(batch)
    applied: list[str] = []
    consumer = IdempotentConsumer(
        f"crash-{args.run}",
        PageObserved,
        lambda e: applied.append(e.payload.observation.observation_id),
        storage.processed_events,
    )
    outcomes = [consumer.handle(w) for w in wires]
    expected = {
        page_observation(url(f"{args.run}/crash/{i}"), seed=f"{args.run}-x{i}").observation_id
        for i in range(CRASH_EVENTS)
    }
    durable = sum(storage.pages.get(o) is not None for o in expected)
    report = {
        "committed": CRASH_EVENTS,
        "durable_after_restart": durable,
        "delivered": len(wires),
        "applied_once": len(applied),
        "duplicates_ignored": outcomes.count(ConsumeOutcome.DUPLICATE),
    }
    print(json.dumps(report))
    for key in client.scan_iter(match=f"{_crash_prefix(args.run)}*"):
        client.delete(key)
    storage.close()
    ok = (
        durable == CRASH_EVENTS
        and set(applied) == expected
        and len(applied) == CRASH_EVENTS
        and len(wires) > CRASH_EVENTS  # the killed relay's unmarked rows were re-sent
    )
    if not ok:
        raise SystemExit(f"crash recovery failed: {report}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--keyspace", required=True)
    parser.add_argument("--run", default="")
    args = parser.parse_args()
    settings = Settings()
    commands = {
        "reset": cmd_reset,
        "write": cmd_write,
        "verify": cmd_verify,
        "crash-produce": cmd_crash_produce,
        "crash-relay": cmd_crash_relay,
        "crash-verify": cmd_crash_verify,
    }
    commands[args.command](settings, args)


if __name__ == "__main__":
    main()
