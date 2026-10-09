"""The worker pool loop (failures never stop it) and the probe-status mapping table."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from antipiracy_contracts.models.media import MediaKind, ProbeStatus
from antipiracy_contracts.models.web import FetchCapability

from crawler2.core.configuration import Settings
from crawler2.core.observability import Metrics
from crawler2.crawlers.model import BodyKind, FetchResult, Outcome
from crawler2.media.probe import ProbeMetrics, probe_status, run_pool
from crawler2.media.work import Job
from crawler2.storage.errors import StorageUnavailableError


class FakeWork:
    def __init__(self, jobs: list[Job], *, fail_pops: int = 0) -> None:
        self.queue = list(jobs)
        self.wanted: list[Job] = []
        self.fail_pops = fail_pops

    def pop(self, stage: str, *, timeout_s: float) -> Job | None:
        if self.fail_pops:
            self.fail_pops -= 1
            raise StorageUnavailableError("redis down")
        return self.queue.pop(0) if self.queue else None

    def want(self, stage: str, job: Job) -> bool:
        self.wanted.append(job)
        return True


def _job(i: int) -> Job:
    return Job(f"m{i}", f"https://cdn.example/{i}.mp4", "video_file")


def test_pool_survives_storage_errors_and_bad_jobs() -> None:
    work = FakeWork([_job(i) for i in range(4)], fail_pops=1)
    handled: list[str] = []

    async def handle(job: Job) -> None:
        if job.media_id == "m1":
            raise StorageUnavailableError("scylla down")
        if job.media_id == "m2":
            raise RuntimeError("bug in one job")
        handled.append(job.media_id)

    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return not work.queue and calls["n"] > 8

    asyncio.run(run_pool("probe", work, handle, concurrency=2, stop=stop, block_s=0.01))  # type: ignore[arg-type]
    assert sorted(handled) == ["m0", "m3"]
    assert [j.media_id for j in work.wanted] == ["m1"]  # a storage failure wants it again


def test_metrics_and_reject_counter() -> None:
    metrics = Metrics(Settings())
    pm = ProbeMetrics(metrics)
    pm.rejected("hash")("internal.example")
    pm.skip("probe", "fresh")
    rendered = metrics.render()
    assert b'crawler2_media_ssrf_rejections_total{path="hash"} 1.0' in rendered
    assert b'crawler2_media_work_skipped_total{reason="fresh",stage="probe"} 1.0' in rendered
    silent = ProbeMetrics(None)
    silent.rejected("probe")("x")
    silent.skip("probe", "x")
    assert not silent.enabled


def _result(outcome: Outcome, status: int | None = None, **kw: Any) -> FetchResult:
    return FetchResult(
        outcome=outcome,
        capability=FetchCapability.HTTP,
        requested_url="https://cdn.example/v.mp4",
        status=status,
        **kw,
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_result(Outcome.BLOCKED, 200), (ProbeStatus.FORBIDDEN, "http_200")),
        (_result(Outcome.CAPTCHA), (ProbeStatus.FORBIDDEN, None)),
        (_result(Outcome.HTTP_ERROR, 500), (ProbeStatus.UNREACHABLE, "http_500")),
        (_result(Outcome.TIMEOUT), (ProbeStatus.UNREACHABLE, "timeout")),
        (_result(Outcome.DNS_ERROR), (ProbeStatus.UNREACHABLE, "dns_error")),
        (_result(Outcome.NEEDS_JS, 200), (ProbeStatus.NOT_MEDIA, "needs_js")),
        (_result(Outcome.TOO_LARGE, 200), (ProbeStatus.NOT_MEDIA, "too_large")),
        (_result(Outcome.OK, 200, body_kind=BodyKind.HTML), (ProbeStatus.NOT_MEDIA, "ok")),
        (
            _result(Outcome.OK, 200, body_kind=BodyKind.MANIFEST),
            (ProbeStatus.PROBED, "manifest"),
        ),
    ],
)
def test_probe_status_mapping(
    result: FetchResult, expected: tuple[ProbeStatus, str | None]
) -> None:
    assert probe_status(result, MediaKind.VIDEO_FILE) == expected
