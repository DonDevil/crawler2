"""Frontier types, contract mapping, settings and the heartbeat helper (no Redis)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from antipiracy_contracts.events.web import CrawlReason, CrawlRequested
from antipiracy_contracts.models.web import FetchCapability, UrlRef

from crawler2.core.configuration import ExecutionQueue as Q
from crawler2.core.configuration import FrontierSettings, Settings
from crawler2.frontier import (
    Admission,
    Claim,
    ClaimLostError,
    FrontierUnavailableError,
    admission_from_request,
    queue_for_capability,
    run_with_heartbeat,
)

REF = UrlRef.of("https://example.test/a")


def test_priority_is_p1_scale() -> None:
    Admission(REF, priority=0)
    Admission(REF, priority=100)
    for bad in (-1, 101):
        with pytest.raises(ValueError, match="priority"):
            Admission(REF, priority=bad)


def test_not_before_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Admission(REF, not_before=datetime(2026, 1, 1))


@pytest.mark.parametrize(
    ("capability", "queue"),
    [
        (None, Q.HTTP),
        (FetchCapability.HTTP, Q.HTTP),
        (FetchCapability.BROWSER, Q.BROWSER),
        (FetchCapability.TOR_HTTP, Q.TOR),
        (FetchCapability.TOR_BROWSER, Q.TOR),
    ],
)
def test_capability_to_queue(capability: FetchCapability | None, queue: Q) -> None:
    assert queue_for_capability(capability) is queue


def test_every_capability_has_a_queue() -> None:
    for capability in FetchCapability:
        assert isinstance(queue_for_capability(capability), Q)


def test_admission_from_crawl_requested() -> None:
    when = datetime(2026, 10, 1, tzinfo=UTC)
    request = CrawlRequested(
        url=REF,
        reason=CrawlReason.RECRAWL,
        priority=70,
        capability=FetchCapability.TOR_BROWSER,
        not_before=when,
    )
    assert admission_from_request(request) == Admission(
        REF, queue=Q.TOR, priority=70, not_before=when, reason="recrawl"
    )


def test_settings_defaults_cover_every_queue() -> None:
    cfg = FrontierSettings()
    assert set(cfg.max_depth) == set(Q)
    assert cfg.max_attempts == 3
    assert cfg.lease_ttl_s == 90.0
    assert cfg.max_inflight_per_domain == 2  # chosen by measurement, ADR-019


def test_settings_env_override_is_partial(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CRAWLER2_FRONTIER__MAX_DEPTH", '{"browser": 7}')
    clean_env.setenv("CRAWLER2_FRONTIER__DEFAULT_INTERVAL_S", "0.3")
    cfg = Settings().frontier
    assert cfg.max_depth[Q.BROWSER] == 7
    assert cfg.max_depth[Q.HTTP] == 200_000
    assert cfg.default_interval_s == 0.3


def test_settings_reject_inverted_backoff() -> None:
    with pytest.raises(ValueError, match="max_backoff_s"):
        FrontierSettings(base_backoff_s=10, max_backoff_s=1)


# -- heartbeat helper ---------------------------------------------------------------

CLAIM = Claim(
    url_id=REF.url_id,
    url=REF.url,
    domain_id=REF.domain_id,
    queue=Q.HTTP,
    priority=50,
    attempt=1,
    token="t",  # noqa: S106
    lease_expires_at=10.0,
    claimed_at=0.0,
)


class FakeFrontier:
    lease_ttl_s = 0.3

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies  # "ok", "lost" or "down", one per heartbeat
        self.calls = 0

    def heartbeat(self, claim: Claim) -> Claim | None:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        if reply == "down":
            raise FrontierUnavailableError("down")
        if reply == "lost":
            return None
        return replace(claim, lease_expires_at=claim.lease_expires_at + 1)


async def slow(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "done"


def test_heartbeat_renews_until_work_finishes() -> None:
    fake = FakeFrontier(["ok"])
    result, claim = asyncio.run(
        run_with_heartbeat(fake, CLAIM, slow(0.35), interval_s=0.1)  # type: ignore[arg-type]
    )
    assert result == "done"
    assert fake.calls >= 3
    assert claim.lease_expires_at == CLAIM.lease_expires_at + fake.calls


def test_lost_claim_cancels_work() -> None:
    fake = FakeFrontier(["ok", "lost"])
    cancelled = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    async def scenario() -> None:
        with pytest.raises(ClaimLostError):
            await run_with_heartbeat(fake, CLAIM, work(), interval_s=0.05)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_outage_during_heartbeat_is_not_a_lost_claim() -> None:
    fake = FakeFrontier(["down", "down", "ok"])
    result, _ = asyncio.run(
        run_with_heartbeat(fake, CLAIM, slow(0.3), interval_s=0.05)  # type: ignore[arg-type]
    )
    assert result == "done"
