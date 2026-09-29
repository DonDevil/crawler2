"""Worker runtime against the real P3 frontier (design §10, §16, §22)."""

from __future__ import annotations

import asyncio

import pytest
from antipiracy_contracts.models.web import FetchOutcome, UrlRef

from crawler2.core.configuration import ExecutionQueue
from crawler2.crawlers.http import HttpFetcher
from crawler2.crawlers.model import Outcome
from crawler2.storage.errors import StorageUnavailableError
from tests.fixtures.web import FixtureSite
from tests.integration.crawlers.conftest import (
    HTTP_SETTINGS,
    FrontierFactory,
    MemoryRecorder,
    admit,
    health,
    run_until_idle,
    runtime,
)

pytestmark = pytest.mark.integration
HTTP = ExecutionQueue.HTTP
BROWSER = ExecutionQueue.BROWSER


def test_http_pool_maps_outcomes_onto_the_frontier(
    site: FixtureSite, make_frontier: FrontierFactory
) -> None:
    frontier = make_frontier(max_attempts=2)
    paths = [
        "/ok",
        "/chain/1",
        "/does-not-exist",
        "/captcha",
        "/blocked",
        "/ratelimited",
        "/media/huge.mp4",
        "/js-only",
        "/stall",
        "/master.m3u8",
    ]
    for path in paths:
        admit(frontier, site.url(path))
    recorder = MemoryRecorder()
    rt = runtime(frontier, HTTP, HttpFetcher(HTTP_SETTINGS), recorder, concurrency=8)
    asyncio.run(run_until_idle(rt, frontier, HTTP))

    by_path = {p: recorder.outcomes_for(site.url(p)) for p in paths}
    assert by_path["/ok"] == ["ok"]
    assert by_path["/chain/1"] == ["ok"]
    assert by_path["/does-not-exist"] == ["http_error"]
    assert by_path["/captcha"] == ["captcha"]  # recorded, completed, not escalated
    assert by_path["/blocked"] == ["blocked"]
    assert by_path["/ratelimited"] == ["blocked", "blocked"]  # 429 retried, then exhausted
    assert by_path["/media/huge.mp4"] == ["media"]
    assert by_path["/master.m3u8"] == ["ok"]
    assert by_path["/stall"] == ["timeout", "timeout"]  # frontier retry, one attempt each
    assert by_path["/js-only"] == ["needs_js"]
    # needs_js moved the task to the browser queue, consuming one attempt
    stats = frontier.stats()
    assert stats.depth[BROWSER] == 1
    assert stats.depth[HTTP] == 0
    browser_claim = frontier.claim(BROWSER)
    assert browser_claim is not None
    assert browser_claim.url == site.url("/js-only")
    assert browser_claim.attempt == 2
    frontier.complete(browser_claim)
    assert rt.stats.reports["fail:exhausted"] == 2
    assert site.state.bytes_sent["/media/huge.mp4"] < 4 * 1024 * 1024
    # every recorded attempt is a valid P1 FetchAttempt
    assert {a.outcome for a in recorder.attempts} >= {
        FetchOutcome.RESPONSE,
        FetchOutcome.BLOCKED,
        FetchOutcome.TIMEOUT,
    }


def test_conditional_request_uses_stored_validators(
    site: FixtureSite, make_frontier: FrontierFactory
) -> None:
    frontier = make_frontier()
    recorder = MemoryRecorder()
    admit(frontier, site.url("/etag"))
    asyncio.run(
        run_until_idle(
            runtime(frontier, HTTP, HttpFetcher(HTTP_SETTINGS), recorder), frontier, HTTP
        )
    )
    first = recorder.results[-1]
    assert first.outcome is Outcome.OK
    assert first.validators is not None
    recorder.validators[str(UrlRef.of(site.url("/etag")).url_id)] = first.validators
    admit(frontier, site.url("/etag"))
    asyncio.run(
        run_until_idle(
            runtime(frontier, HTTP, HttpFetcher(HTTP_SETTINGS), recorder), frontier, HTTP
        )
    )
    assert recorder.results[-1].outcome is Outcome.NOT_MODIFIED


def test_confirmed_local_outage_defers_without_spending_attempts(
    make_frontier: FrontierFactory,
) -> None:
    import socket

    frontier = make_frontier(max_attempts=50)
    refused = socket.socket()
    refused.bind(("127.0.0.1", 0))
    port = refused.getsockname()[1]
    # six loopback hosts = six domains, so the per-domain in-flight limit
    # (ADR-019) leaves attempts in flight when the outage is confirmed
    for i in range(6):
        admit(frontier, f"http://127.0.0.{i + 1}:{port}/down/{i}")

    async def offline() -> bool:
        return False

    network = health(offline, trigger_threshold=2)
    recorder = MemoryRecorder()
    rt = runtime(frontier, HTTP, HttpFetcher(HTTP_SETTINGS), recorder, network=network)

    async def go() -> tuple[int, int]:
        task = asyncio.ensure_future(rt.run())
        while not network.offline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)  # in-flight attempts finish and are deferred
        claims_at_offline = rt.stats.claims
        await asyncio.sleep(1.5)
        claims_later = rt.stats.claims
        rt.stop()
        await task
        return claims_at_offline, claims_later

    at_offline, later = asyncio.run(asyncio.wait_for(go(), 30))
    refused.close()
    assert network.offline
    assert rt.stats.reports.get("defer:deferred", 0) >= 1
    assert later == at_offline  # a confirmed-offline worker stops claiming
    assert rt.stats.reports.get("fail:exhausted", 0) == 0
    stats = frontier.stats()
    assert stats.depth[HTTP] == 6  # all six tasks still exist
    frontier.clear()


def test_storage_outage_defers_instead_of_losing_the_attempt(
    site: FixtureSite, make_frontier: FrontierFactory
) -> None:
    frontier = make_frontier()
    admit(frontier, site.url("/ok"))
    recorder = MemoryRecorder(fail_with=StorageUnavailableError("scylla down"))
    rt = runtime(frontier, HTTP, HttpFetcher(HTTP_SETTINGS), recorder)

    async def go() -> None:
        task = asyncio.ensure_future(rt.run(max_claims=3))
        await asyncio.wait_for(task, 30)

    asyncio.run(go())
    assert rt.stats.reports.get("defer:deferred") == 3
    claim = frontier.claim(HTTP)
    assert claim is not None
    assert claim.attempt == 1  # three deferrals refunded every attempt
    frontier.complete(claim)


def test_shutdown_mid_attempt_defers(site: FixtureSite, make_frontier: FrontierFactory) -> None:
    frontier = make_frontier()
    admit(frontier, site.url("/stall"))
    slow = HTTP_SETTINGS.model_copy(update={"total_timeout_s": 30, "read_timeout_s": 30})
    rt = runtime(frontier, HTTP, HttpFetcher(slow), MemoryRecorder(), shutdown_grace_s=0.2)

    async def go() -> None:
        task = asyncio.ensure_future(rt.run())
        await asyncio.sleep(1.0)
        rt.stop()
        await asyncio.wait_for(task, 10)

    asyncio.run(go())
    claim = frontier.claim(HTTP)
    assert claim is not None
    assert claim.attempt == 1
    frontier.complete(claim)
