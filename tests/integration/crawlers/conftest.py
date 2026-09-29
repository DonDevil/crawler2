"""P4 integration fixtures: real Redis frontier, fixture web, optional real storage.

Run from the host (browser workers live on the host; the host reaches
Scylla at its container IP — docs/development.md "P4 tests"). Frontier
state lives in Redis db 9 under a namespace per test and must pass
``audit()`` at teardown; storage tests use their own keyspace and bucket.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import redis
from antipiracy_contracts.ids import UrlId
from antipiracy_contracts.models.web import FetchAttempt, HttpValidators, UrlRef

from crawler2.core.configuration import ExecutionQueue, FrontierSettings, Settings
from crawler2.core.configuration.settings import (
    HttpFetchSettings,
    NetworkHealthSettings,
    PoolSettings,
)
from crawler2.core.observability import Metrics
from crawler2.crawlers.health import NetworkHealth, ProbeRound
from crawler2.crawlers.model import Fetcher, FetchResult
from crawler2.crawlers.recorder import project_attempt
from crawler2.crawlers.runtime import WorkerRuntime
from crawler2.frontier import Admission
from crawler2.frontier.redis import RedisFrontier, connect_redis
from tests.fixtures import fetchweb
from tests.fixtures.web import FixtureSite, serve

TEST_DB = 9
WORKER = "dev-1:http:1:" + "0" * 32
HTTP_SETTINGS = HttpFetchSettings(
    connect_timeout_s=2, read_timeout_s=2, total_timeout_s=4, media_probe_bytes=64 * 1024
)
browser_only = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1", reason="needs Chromium; set RUN_BROWSER_TESTS=1"
)


@dataclass
class MemoryRecorder:
    """Keeps projected attempts in memory; optional validators per URL (W5 stand-in)."""

    attempts: list[FetchAttempt] = field(default_factory=list)
    results: list[FetchResult] = field(default_factory=list)
    validators: dict[str, HttpValidators] = field(default_factory=dict)
    fail_with: Exception | None = None

    def latest_validators(self, url_id: UrlId) -> HttpValidators | None:
        return self.validators.get(str(url_id))

    def record(
        self,
        requested: UrlRef,
        result: FetchResult,
        *,
        started_at: datetime,
        finished_at: datetime,
    ) -> FetchAttempt | None:
        if self.fail_with is not None:
            raise self.fail_with
        self.results.append(result)
        attempt = project_attempt(
            requested, result, worker=WORKER, started_at=started_at, finished_at=finished_at
        )
        if attempt is not None:
            self.attempts.append(attempt)
        return attempt

    def outcomes_for(self, url: str) -> list[str]:
        return [r.outcome.value for r in self.results if r.requested_url == url]


@pytest.fixture(scope="session")
def redis_conn() -> redis.Redis:
    client = connect_redis(Settings().redis.model_copy(update={"db": TEST_DB}))
    client.ping()
    return client


FrontierFactory = Callable[..., RedisFrontier]


@pytest.fixture
def make_frontier(redis_conn: redis.Redis) -> Iterator[FrontierFactory]:
    namespace = f"p4-{uuid.uuid4().hex[:12]}"
    created: list[RedisFrontier] = []

    def factory(**overrides: Any) -> RedisFrontier:
        settings = FrontierSettings(
            **{
                "default_interval_s": 0.0,
                "base_backoff_s": 0.0,
                "max_backoff_s": 0.0,
                "defer_delay_s": 0.0,
                "lease_ttl_s": 10.0,
                **overrides,
            }
        )
        frontier = RedisFrontier(redis_conn, settings, namespace=namespace)
        created.append(frontier)
        return frontier

    yield factory
    if created:
        problems = created[0].audit()
        created[0].clear()
        assert problems == []


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with serve({**fetchweb.fetch_routes(), **fetchweb.pages(1000)}) as s:
        yield s


def admit(frontier: RedisFrontier, url: str, queue: ExecutionQueue = ExecutionQueue.HTTP) -> None:
    assert frontier.admit(Admission(UrlRef.of(url), queue=queue)).accepted


def health(probe: ProbeRound | None = None, **overrides: Any) -> NetworkHealth:
    settings = NetworkHealthSettings(
        **{
            "trigger_threshold": 10,
            "confirm_delay_s": 0.0,
            "recovery_probe_interval_s": 0.05,
            "probe_endpoints": ["http://a.invalid", "http://b.invalid"],
            **overrides,
        }
    )

    async def always_up() -> bool:
        return True

    return NetworkHealth(settings, probe or always_up)


def runtime(
    frontier: RedisFrontier,
    queue: ExecutionQueue,
    fetcher: Fetcher,
    recorder: Any,
    *,
    network: NetworkHealth | None = None,
    concurrency: int = 4,
    shutdown_grace_s: float = 5,
) -> WorkerRuntime:
    return WorkerRuntime(
        frontier=frontier,
        queue=queue,
        fetcher=fetcher,
        recorder=recorder,
        settings=PoolSettings(
            concurrency=concurrency,
            idle_poll_min_s=0.05,
            idle_poll_max_s=0.2,
            shutdown_grace_s=shutdown_grace_s,
        ),
        health=network or health(),
        metrics=Metrics(Settings()),
        max_memory_mb=4096,
        recover_every_s=0.5,
    )


async def run_until_idle(
    rt: WorkerRuntime, frontier: RedisFrontier, queue: ExecutionQueue, *, timeout: float = 60
) -> None:
    """Run the runtime until ``queue`` has no active task (or ``timeout``)."""
    task = asyncio.ensure_future(rt.run())
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
            stats = await asyncio.to_thread(frontier.stats)
            if stats.depth[queue] == 0:
                break
    finally:
        rt.stop()
        await asyncio.wait_for(task, 30)


@pytest.fixture(scope="session")
def storage_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    base = Settings()
    return base.model_copy(
        update={
            "scylla": base.scylla.model_copy(update={"keyspace": "crawler2_p4_it"}),
            "minio": base.minio.model_copy(update={"bucket_raw": "crawler2-p4-it"}),
            "scratch_dir": Path(tmp_path_factory.mktemp("scratch")),
        }
    )


@pytest.fixture(scope="session")
def storage(storage_settings: Settings) -> Iterator[Any]:
    from crawler2.storage.objectstore import S3ObjectStore
    from crawler2.storage.scylla import Migrator, ScyllaSession, ScyllaStorage

    s = storage_settings
    session = ScyllaSession.connect(s.scylla, keyspace=s.scylla.keyspace)
    session.execute_raw(f"DROP KEYSPACE IF EXISTS {s.scylla.keyspace}")
    Migrator(session, s.scylla, applied_by="p4-it").migrate()
    objects = S3ObjectStore(s.minio, scratch_dir=s.scratch_dir)
    objects.ensure_bucket()
    yield ScyllaStorage.over(session), objects
    objects.close()
    session.close()
