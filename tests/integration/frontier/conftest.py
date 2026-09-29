"""Frontier tests run against the compose Redis (host: CRAWLER2_REDIS__HOST/PORT).

Each test gets its own namespace and a controllable clock, and must leave
the frontier structurally consistent (``audit()`` is checked on teardown).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import redis
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration import ExecutionQueue, FrontierSettings, Settings
from crawler2.frontier import Admission
from crawler2.frontier.redis import RedisFrontier, connect_redis

TEST_DB = 9


@dataclass
class Clock:
    now: float = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def url(domain: str, path: str | int) -> UrlRef:
    return UrlRef.of(f"https://{domain}.test/{path}")


def admission(
    domain: str,
    path: str | int,
    *,
    queue: ExecutionQueue = ExecutionQueue.HTTP,
    priority: int = 50,
    **kwargs: Any,
) -> Admission:
    return Admission(url(domain, path), queue=queue, priority=priority, **kwargs)


def redis_client() -> redis.Redis:
    return connect_redis(Settings().redis.model_copy(update={"db": TEST_DB}))


@pytest.fixture(scope="session")
def redis_conn() -> redis.Redis:
    client = redis_client()
    client.ping()
    return client


@pytest.fixture
def clock() -> Clock:
    return Clock()


FrontierFactory = Callable[..., RedisFrontier]


@pytest.fixture
def make_frontier(redis_conn: redis.Redis, clock: Clock) -> Iterator[FrontierFactory]:
    namespace = f"test-{uuid.uuid4().hex[:12]}"
    created: list[RedisFrontier] = []

    def factory(*, real_clock: bool = False, **overrides: Any) -> RedisFrontier:
        settings = FrontierSettings(**{"default_interval_s": 0.0, **overrides})
        frontier = RedisFrontier(
            redis_conn, settings, namespace=namespace, clock=None if real_clock else clock
        )
        created.append(frontier)
        return frontier

    yield factory
    if created:
        problems = created[0].audit()
        created[0].clear()
        assert problems == []


@pytest.fixture
def frontier(make_frontier: FrontierFactory) -> RedisFrontier:
    return make_frontier()
