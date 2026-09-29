"""Recorder on real storage, and the B.5 #1 end-to-end test (design §27).

B.5 #1: with no intelligence service running (no P6, no P7), real
``crawler2-worker`` processes claim from the P3 frontier, fetch the
fixture web, persist attempts/observations/snapshots through P2 and
recover from a killed worker — using only default profiles and priority.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from antipiracy_contracts.models.web import FetchCapability, FetchOutcome, UrlRef

from crawler2.core.configuration import FrontierSettings, Settings
from crawler2.crawlers.http import HttpFetcher
from crawler2.crawlers.model import FetchRequest, Outcome
from crawler2.crawlers.recorder import StorageRecorder
from crawler2.frontier import Admission
from crawler2.frontier.redis import RedisFrontier
from tests.fixtures.web import FixtureSite
from tests.integration.crawlers.conftest import HTTP_SETTINGS, TEST_DB, browser_only

pytestmark = pytest.mark.integration
WORKER = "dev-1:http:1:" + "0" * 32
ROOT = Path(__file__).resolve().parents[3]


def test_storage_recorder_persists_attempt_observation_and_snapshot(
    site: FixtureSite, storage: tuple[Any, Any]
) -> None:
    repos, objects = storage
    recorder = StorageRecorder(repos.fetch_attempts, repos.pages, objects, worker=WORKER)
    url = site.url("/etag")
    ref = UrlRef.of(url)

    async def fetch(validators: Any = None) -> Any:
        fetcher = HttpFetcher(HTTP_SETTINGS)
        try:
            return await fetcher.fetch(FetchRequest(url, ref.url_id, validators=validators))
        finally:
            await fetcher.close()

    now = datetime.now(UTC) - timedelta(seconds=5)
    first = asyncio.run(fetch())
    attempt = recorder.record(ref, first, started_at=now, finished_at=now + timedelta(seconds=1))
    assert attempt is not None
    assert repos.fetch_attempts.get(attempt.fetch_attempt_id) == attempt
    latest = repos.pages.latest(ref.url_id)
    assert latest is not None
    assert latest.http_status == 200
    assert latest.validators == first.validators
    assert recorder.latest_validators(ref.url_id) == first.validators  # W5 feeds the 304 path
    observation = repos.pages.get(latest.observation_id)
    assert observation.snapshot is not None
    assert objects.get(observation.snapshot) == first.body

    second = asyncio.run(fetch(recorder.latest_validators(ref.url_id)))
    assert second.outcome is Outcome.NOT_MODIFIED
    later = now + timedelta(seconds=2)
    not_modified = recorder.record(ref, second, started_at=later, finished_at=later)
    assert not_modified is not None
    assert not_modified.detail == "outcome=not_modified"
    assert repos.pages.latest(ref.url_id).observation_id == latest.observation_id  # no new version


def _worker_env(namespace: str, storage_settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "CRAWLER2_REDIS__DB": str(TEST_DB),
            "CRAWLER2_REDIS__NAMESPACE": namespace,
            "CRAWLER2_SCYLLA__KEYSPACE": storage_settings.scylla.keyspace,
            "CRAWLER2_MINIO__BUCKET_RAW": storage_settings.minio.bucket_raw,
            "CRAWLER2_SCRATCH_DIR": str(storage_settings.scratch_dir),
            "CRAWLER2_METRICS__ENABLED": "false",
            "CRAWLER2_LOGGING__FORMAT": "json",
            "CRAWLER2_FRONTIER__DEFAULT_INTERVAL_S": "0",
            "CRAWLER2_FRONTIER__BASE_BACKOFF_S": "0.2",
            "CRAWLER2_FRONTIER__MAX_BACKOFF_S": "0.5",
            "CRAWLER2_FRONTIER__LEASE_TTL_S": "4",
            "CRAWLER2_WORKERS__RECOVER_EVERY_S": "1",
            "CRAWLER2_WORKERS__FETCH__TOTAL_TIMEOUT_S": "3",
            "CRAWLER2_WORKERS__FETCH__READ_TIMEOUT_S": "2",
            "CRAWLER2_WORKERS__BROWSER_ENGINE__NAVIGATION_TIMEOUT_S": "8",
            "CRAWLER2_WORKERS__BROWSER_ENGINE__SETTLE_S": "0.5",
            "CRAWLER2_WORKERS__NETWORK_HEALTH__ENABLED": "false",  # no internet in tests
        }
    )
    return env


def _spawn(pool: str, env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 -- fixed argv: this interpreter + our CLI
        [sys.executable, "-m", "crawler2.crawlers.cli", "--pool", pool],
        env=env,
        cwd=ROOT,
        stdout=log.open("ab"),
        stderr=subprocess.STDOUT,
    )


@browser_only
def test_b51_workers_crawl_the_fixture_web_without_intelligence(
    site: FixtureSite,
    storage: tuple[Any, Any],
    storage_settings: Settings,
    redis_conn: Any,
    tmp_path: Path,
) -> None:
    repos, _objects = storage
    namespace = f"p4e2e-{uuid.uuid4().hex[:10]}"
    frontier = RedisFrontier(
        redis_conn,
        FrontierSettings(
            default_interval_s=0, base_backoff_s=0.2, max_backoff_s=0.5, lease_ttl_s=4
        ),
        namespace=namespace,
    )
    paths = [
        "/ok",
        "/chain/1",
        "/gzip",
        "/brotli",
        "/js-only",
        "/captcha",
        "/blocked",
        "/does-not-exist",
        "/media/huge.mp4",
        "/master.m3u8",
        "/slowloris",
        *[f"/page/{i}" for i in range(30)],
    ]
    for path in paths:  # default priority, no capability: exactly what P6/P7 would not add
        assert frontier.admit(Admission(UrlRef.of(site.url(path)))).accepted
    env = _worker_env(namespace, storage_settings)
    log = tmp_path / "workers.log"
    http_a = _spawn("http", env, log)
    browser = _spawn("browser", env, log)
    workers = [http_a, browser]
    try:
        time.sleep(1.5)
        http_a.send_signal(signal.SIGKILL)  # a worker dies mid-run
        http_a.wait(10)
        workers.append(_spawn("http", env, log))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if frontier.stats().active_tasks == 0:
                break
            time.sleep(0.5)
        stats = frontier.stats()
    finally:
        for proc in workers:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in workers:
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()
    problems = frontier.audit()
    frontier.clear()
    assert stats.active_tasks == 0, log.read_text()[-3000:]
    assert problems == []
    assert stats.counters.get("dead", 0) == 0
    assert stats.counters.get("recovered", 0) >= 1  # the killed worker's lease

    def attempts(path: str) -> list[Any]:
        ref = UrlRef.of(site.url(path))
        return sorted(repos.fetch_attempts.recent_for_url(ref.url_id), key=lambda a: a.finished_at)

    # plain pages reached storage with snapshots
    for path in ("/ok", "/gzip", "/brotli", "/page/0", "/page/29"):
        latest = repos.pages.latest(UrlRef.of(site.url(path)).url_id)
        assert latest is not None, path
        assert latest.snapshot_uri is not None
    # the JS-only page was escalated http -> browser inside one task
    js = attempts("/js-only")
    assert [(a.capability, a.outcome) for a in js][-2:] == [
        (FetchCapability.HTTP, FetchOutcome.RESPONSE),
        (FetchCapability.BROWSER, FetchOutcome.RESPONSE),
    ]
    rendered = repos.pages.get(
        repos.pages.latest(UrlRef.of(site.url("/js-only")).url_id).observation_id
    )
    assert rendered.capability is FetchCapability.BROWSER
    assert _objects.get(rendered.snapshot).find(b"rendered-by-js") >= 0
    # redirects recorded; captcha/blocked recorded without escalation
    chain = repos.fetch_attempts.get(attempts("/chain/1")[-1].fetch_attempt_id)
    assert [hop.status for hop in chain.redirects] == [301, 302, 307]
    assert {a.capability for a in attempts("/captcha")} == {FetchCapability.HTTP}
    assert attempts("/blocked")[-1].outcome is FetchOutcome.BLOCKED
    # media probed, never downloaded
    assert site.state.bytes_sent["/media/huge.mp4"] < 4 * 1024 * 1024
    # slowloris ended as timeouts, each one frontier attempt
    assert {a.outcome for a in attempts("/slowloris")} == {FetchOutcome.TIMEOUT}
    assert len(attempts("/slowloris")) <= FrontierSettings().max_attempts
