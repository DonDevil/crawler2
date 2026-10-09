"""P8 Redis coordination on the real Redis: TIME clock, per-media dedupe of wants,
work leases, per-host spacing and per-host slots (design §5.1-§5.2)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from crawler2.core.configuration import Settings
from crawler2.frontier.redis import connect_redis
from crawler2.media.model import MediaLatest
from crawler2.media.schedule import ProbeScheduler
from crawler2.media.work import Job, RedisWork
from tests.fixtures.contracts import T0, media, media_observation, page_observation, url

pytestmark = pytest.mark.integration


@pytest.fixture
def work(settings: Settings) -> Iterator[RedisWork]:
    client = connect_redis(settings.redis)
    namespace = f"it-p8-{uuid.uuid4().hex[:10]}"
    yield RedisWork(client, namespace=namespace)
    for key in client.scan_iter(match=f"{namespace}:*"):
        client.delete(key)
    client.close()


def test_redis_time_is_the_clock(work: RedisWork) -> None:
    assert abs(work.now() - datetime.now(UTC)) < timedelta(seconds=5)


def test_one_want_per_media_and_stage(work: RedisWork) -> None:
    job = Job("med_1", "https://cdn.example/v.mp4", "video_file")
    assert work.want("probe", job)
    assert not work.want("probe", job)  # already queued: no per-sighting duplicates
    assert work.want("hash", job)  # other stage
    assert work.depth("probe") == 1
    assert work.pop("probe", timeout_s=1) == job
    assert work.pop("probe", timeout_s=0.1) is None
    assert work.want("probe", job)  # popped: wanted again later is allowed


def test_leases_and_host_limits(work: RedisWork) -> None:
    assert work.lease("probe", "m", "w1", ttl_s=5)
    assert not work.lease("probe", "m", "w2", ttl_s=5)
    work.release("probe", "m", "w2")  # not the holder: no effect
    assert not work.lease("probe", "m", "w2", ttl_s=5)
    work.release("probe", "m", "w1")
    assert work.lease("probe", "m", "w2", ttl_s=5)

    assert work.throttle("cdn.example", interval_s=0.5) == 0
    wait = work.throttle("cdn.example", interval_s=0.5)
    assert 0 < wait <= 0.5
    assert work.throttle("other.example", interval_s=0.5) == 0
    time.sleep(wait + 0.05)
    assert work.throttle("cdn.example", interval_s=0.5) == 0
    assert work.throttle("cdn.example", interval_s=0) == 0

    slot = work.host_slot("cdn.example", "w1", slots=1, ttl_s=5)
    assert slot is not None
    assert work.host_slot("cdn.example", "w2", slots=1, ttl_s=5) is None
    assert work.host_slot("cdn.example", "w2", slots=2, ttl_s=5) is not None
    work.release_slot(slot, "w1")
    assert work.host_slot("cdn.example", "w3", slots=1, ttl_s=5) is not None


def test_probe_scheduler_wants_once_per_media(work: RedisWork) -> None:
    scheduler = ProbeScheduler(work, reprobe_after=timedelta(days=7))
    item = media("v.mp4")
    for i in range(5):  # one locator on five pages
        page = page_observation(url(f"p{i}"), at=T0)
        scheduler.observed(media_observation(item, page), None)
    assert work.depth("probe") == 1
    job = work.pop("probe", timeout_s=1)
    assert job is not None
    assert job.media_id == str(item.media_id)
    assert job.referer == url("p0").url
    scheduler.observed(
        media_observation(item, page_observation(url("x"), at=T0)), MediaLatest(item.media_id)
    )
    assert work.depth("probe") == 1
