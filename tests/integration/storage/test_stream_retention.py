"""Stream retention against real Redis (ADR-022, P6 X-8/X-16).

Runs from the host with ``CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=16379``.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import pytest
import redis

from crawler2.core.configuration import EventSettings
from crawler2.storage.events.consumer import RedisStreamReader
from crawler2.storage.events.publisher import (
    OutboundEvent,
    RedisStreamPublisher,
    StreamBackpressureError,
    stream_name,
)

BUDGET = 64 * 1024


def _raw(client: redis.Redis) -> Any:
    """redis-py's reply types are unions; these assertions read them as returned."""
    return client


def _events(n: int, size: int, start: int = 0, kind: str = "page.observed") -> list[OutboundEvent]:
    return [
        OutboundEvent(f"evt_{start + i:06d}", kind, 1, os.urandom(size // 2).hex().encode())
        for i in range(n)
    ]


def _publish_all(publisher: RedisStreamPublisher, events: Sequence[OutboundEvent]) -> int:
    """Publish until backpressure; return how many were delivered."""
    try:
        publisher.publish(events)
    except StreamBackpressureError as exc:
        return exc.published
    return len(events)


@pytest.fixture
def bounded(event_settings: EventSettings) -> EventSettings:
    return event_settings.model_copy(update={"stream_max_bytes": BUDGET, "stream_maxlen": 50})


def test_unread_entries_are_never_trimmed_and_backpressure_bounds_bytes(
    redis_client: redis.Redis, bounded: EventSettings
) -> None:
    stream = stream_name(bounded, "page.observed", 1)
    reader = RedisStreamReader(redis_client, stream, "extraction", "c1")
    reader.ensure_group()
    publisher = RedisStreamPublisher(redis_client, bounded)
    events = _events(40, 4096)  # ~165 KiB, well over the 64 KiB budget

    delivered = _publish_all(publisher, events)
    assert 0 < delivered < len(events)
    assert _raw(redis_client).xlen(stream) == delivered  # nothing was trimmed
    assert int(_raw(redis_client).memory_usage(stream, samples=0)) <= BUDGET * 1.2

    # The consumer reads part of it but acknowledges nothing: still nothing goes.
    read = reader.read(count=5)
    assert len(read) == 5
    assert _publish_all(publisher, events[delivered:]) == 0
    assert _raw(redis_client).xlen(stream) == delivered

    # It acknowledges everything it has: acknowledged entries are trimmed and
    # publishing resumes; every event is eventually read exactly in order.
    seen = [e.envelope for e in read]
    reader.ack(read)
    offset = delivered
    while offset < len(events) or len(seen) < len(events):
        batch = reader.read(count=7)
        seen += [e.envelope for e in batch]
        reader.ack(batch)
        if offset < len(events):
            offset += _publish_all(publisher, events[offset:])
    assert seen == [e.envelope for e in events]  # no event lost, none reordered


def test_a_pending_entry_holds_the_trim_floor(
    redis_client: redis.Redis, bounded: EventSettings
) -> None:
    stream = stream_name(bounded, "urls.discovered", 1)
    fast = RedisStreamReader(redis_client, stream, "discovery-admission", "a")
    slow = RedisStreamReader(redis_client, stream, "crawl-intelligence", "b")
    fast.ensure_group()
    slow.ensure_group()
    publisher = RedisStreamPublisher(redis_client, bounded)
    publisher.publish(_events(4, 1024, kind="urls.discovered"))
    fast.ack(fast.read(count=10))
    stuck = slow.read(count=1)  # read, never acknowledged (a crashed consumer)
    publisher.publish(_events(1, 1024, start=4, kind="urls.discovered"))
    ids = [_id for _id, _ in _raw(redis_client).xrange(stream)]
    assert ids[0].decode() == stuck[0].entry_id  # the floor is the slow group's pending entry
    assert len(ids) == 5


def test_an_oversized_event_passes_once_its_stream_is_drained(
    redis_client: redis.Redis, bounded: EventSettings
) -> None:
    stream = stream_name(bounded, "urls.discovered", 1)
    reader = RedisStreamReader(redis_client, stream, "discovery-admission", "a")
    reader.ensure_group()
    publisher = RedisStreamPublisher(redis_client, bounded)
    big = _events(2, BUDGET * 2, kind="urls.discovered")  # each larger than the budget
    assert _publish_all(publisher, big) == 1  # accepted alone, the second waits
    reader.ack(reader.read(count=10))
    assert _publish_all(publisher, big[1:]) == 1


def test_streams_without_groups_are_capped_by_count_and_bytes(
    redis_client: redis.Redis, bounded: EventSettings
) -> None:
    stream = stream_name(bounded, "fetch.completed", 1)
    publisher = RedisStreamPublisher(redis_client, bounded)
    for start in range(0, 200, 20):
        publisher.publish(_events(20, 2048, start=start, kind="fetch.completed"))
    assert int(_raw(redis_client).memory_usage(stream, samples=0)) <= BUDGET
    assert _raw(redis_client).xlen(stream) <= 50 + 100  # approximate MAXLEN may keep a node more
    last = _raw(redis_client).xrevrange(stream, count=1)[0][1][b"event_id"]
    assert last == b"evt_000199"  # the newest entries are the ones kept
