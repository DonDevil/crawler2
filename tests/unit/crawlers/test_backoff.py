"""P6 X-14: per-domain back-off on 429 / Retry-After (crawlers/backoff.py + runtime hook)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any

import pytest
from antipiracy_contracts.ids import DomainId
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration.settings import RateLimitBackoffSettings
from crawler2.crawlers.backoff import DomainBackoff, parse_retry_after
from crawler2.crawlers.model import Outcome
from crawler2.crawlers.runtime import WorkerRuntime
from crawler2.frontier import FrontierUnavailableError

DOMAIN = UrlRef.of("http://limited.example/").domain_id
SETTINGS = RateLimitBackoffSettings(base_s=5, max_s=100, min_window_s=30)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_retry_after_parsing() -> None:
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(" 7 ") == 7.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-5") is None
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
    got = parse_retry_after(future)
    assert got is not None
    assert 85 <= got <= 90
    assert parse_retry_after(format_datetime(datetime(2000, 1, 1, tzinfo=UTC), usegmt=True)) == 0.0


def test_rate_limit_detection() -> None:
    assert DomainBackoff.is_rate_limited(429, {})
    assert DomainBackoff.is_rate_limited(503, {"retry-after": "5"})
    assert not DomainBackoff.is_rate_limited(503, {})
    assert not DomainBackoff.is_rate_limited(200, {"retry-after": "5"})
    assert not DomainBackoff.is_rate_limited(None, {})


def test_interval_doubles_per_consecutive_429_and_is_capped() -> None:
    backoff = DomainBackoff(SETTINGS, clock=Clock())
    intervals = [backoff.on_rate_limited("d", {}) for _ in range(8)]
    assert intervals == [5, 10, 20, 40, 80, 100, 100, 100]


def test_retry_after_raises_the_interval_but_never_above_the_cap() -> None:
    backoff = DomainBackoff(SETTINGS, clock=Clock())
    assert backoff.on_rate_limited("a", {"retry-after": "50"}) == 50
    assert backoff.on_rate_limited("b", {"retry-after": "86400"}) == 100
    assert backoff.on_rate_limited("c", {"retry-after": "1"}) == 5


def test_domains_are_independent_and_success_clears() -> None:
    backoff = DomainBackoff(SETTINGS, clock=Clock())
    backoff.on_rate_limited("a", {})
    backoff.on_rate_limited("a", {})
    assert backoff.on_rate_limited("b", {}) == 5
    assert backoff.on_success("a") is True
    assert backoff.on_success("a") is False, "nothing left to clear"
    assert backoff.on_rate_limited("a", {}) == 5, "strikes restart after a success"


def test_window_expiry_clears_only_after_min_window() -> None:
    clock = Clock()
    backoff = DomainBackoff(SETTINGS, clock=clock)
    backoff.on_rate_limited("a", {})  # interval 5 < min_window 30
    clock.now += 29
    assert backoff.expired() == []
    clock.now += 2
    assert backoff.expired() == ["a"]
    assert len(backoff) == 0


def test_memory_is_bounded() -> None:
    backoff = DomainBackoff(SETTINGS, clock=Clock(), max_domains=3)
    for i in range(10):
        backoff.on_rate_limited(f"d{i}", {})
    assert len(backoff) == 3


# --- runtime hook ----------------------------------------------------------------------


class FakeFrontier:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, float | None]] = []
        self._fail = fail

    def set_domain_interval(self, domain_id: DomainId, interval_s: float | None) -> None:
        if self._fail:
            raise FrontierUnavailableError("down")
        self.calls.append((str(domain_id), interval_s))


def _runtime(frontier: FakeFrontier, backoff: DomainBackoff | None) -> Any:
    rt = object.__new__(WorkerRuntime)
    rt._frontier = frontier  # type: ignore[assignment]
    rt._backoff = backoff
    rt._pool = "http"
    rt._metrics = SimpleNamespace(  # type: ignore[assignment]
        rate_limited=SimpleNamespace(labels=lambda *_: SimpleNamespace(inc=lambda: None))
    )
    return rt


def _claim() -> Any:
    return SimpleNamespace(domain_id=DOMAIN)


def _result(status: int, outcome: Outcome, headers: dict[str, str] | None = None) -> Any:
    return SimpleNamespace(status=status, outcome=outcome, headers=headers or {})


def test_runtime_widens_on_429_and_clears_on_success() -> None:
    frontier = FakeFrontier()
    rt = _runtime(frontier, DomainBackoff(SETTINGS, clock=Clock()))

    async def go() -> None:
        await rt._apply_backoff(_claim(), _result(429, Outcome.BLOCKED, {"retry-after": "40"}))
        await rt._apply_backoff(_claim(), _result(404, Outcome.HTTP_ERROR))  # no content: no clear
        await rt._apply_backoff(_claim(), _result(200, Outcome.OK))

    asyncio.run(go())
    assert frontier.calls == [(str(DOMAIN), 40.0), (str(DOMAIN), None)]


def test_runtime_survives_an_unavailable_frontier_and_a_disabled_backoff() -> None:
    rt = _runtime(FakeFrontier(fail=True), DomainBackoff(SETTINGS, clock=Clock()))
    asyncio.run(rt._apply_backoff(_claim(), _result(429, Outcome.BLOCKED)))  # no raise
    off = _runtime(FakeFrontier(), None)
    asyncio.run(off._apply_backoff(_claim(), _result(429, Outcome.BLOCKED)))
    assert off._frontier.calls == []


def test_sweep_resets_expired_overrides() -> None:
    clock = Clock()
    frontier = FakeFrontier()
    rt = _runtime(frontier, DomainBackoff(SETTINGS, clock=clock))
    asyncio.run(rt._apply_backoff(_claim(), _result(429, Outcome.BLOCKED)))
    clock.now += 100
    asyncio.run(rt._expire_backoffs())
    assert frontier.calls[-1] == (str(DOMAIN), None)


@pytest.mark.parametrize("bad", [0, -1])
def test_settings_reject_nonpositive_values(bad: float) -> None:
    with pytest.raises(ValueError, match="greater than"):
        RateLimitBackoffSettings(base_s=bad)
