"""Unit tests for the pure parts of P4: decide(), signals, classifier, health, projection."""

from __future__ import annotations

import asyncio
import socket
import ssl
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from antipiracy_contracts.models.web import FetchCapability, FetchOutcome, UrlRef

from crawler2.core.configuration import ExecutionQueue
from crawler2.core.configuration.settings import NetworkHealthSettings, Settings
from crawler2.crawlers import signals
from crawler2.crawlers.classify import classify_exception
from crawler2.crawlers.decide import Operation, decide
from crawler2.crawlers.health import NetworkHealth, NetworkState
from crawler2.crawlers.model import BodyKind, FetchResult, Hop, Outcome
from crawler2.crawlers.recorder import p1_outcome, project_attempt

HTTP, BROWSER, TOR = ExecutionQueue.HTTP, ExecutionQueue.BROWSER, ExecutionQueue.TOR


def result(outcome: Outcome, status: int | None = None, **kw: object) -> FetchResult:
    return FetchResult(
        outcome=outcome,
        capability=FetchCapability.HTTP,
        requested_url="https://site.example/a",
        final_url="https://site.example/a" if status is not None else None,
        status=status,
        **kw,  # type: ignore[arg-type]
    )


# --- decide(): design §22 --------------------------------------------------
@pytest.mark.parametrize(
    ("outcome", "status", "queue", "op", "next_queue"),
    [
        (Outcome.OK, 200, HTTP, Operation.COMPLETE, None),
        (Outcome.NOT_MODIFIED, 304, HTTP, Operation.COMPLETE, None),
        (Outcome.MEDIA, 206, HTTP, Operation.COMPLETE, None),
        (Outcome.HTTP_ERROR, 404, HTTP, Operation.COMPLETE, None),
        (Outcome.HTTP_ERROR, 410, BROWSER, Operation.COMPLETE, None),
        (Outcome.HTTP_ERROR, 503, HTTP, Operation.FAIL, None),
        (Outcome.HTTP_ERROR, 408, TOR, Operation.FAIL, None),
        (Outcome.NEEDS_JS, 200, HTTP, Operation.FAIL, BROWSER),
        (Outcome.NEEDS_JS, 200, BROWSER, Operation.COMPLETE, None),
        (Outcome.NEEDS_JS, 200, TOR, Operation.COMPLETE, None),
        (Outcome.BLOCKED, 429, HTTP, Operation.FAIL, None),
        (Outcome.BLOCKED, 403, HTTP, Operation.COMPLETE, None),
        (Outcome.CAPTCHA, 200, HTTP, Operation.COMPLETE, None),
        (Outcome.TOO_LARGE, None, HTTP, Operation.COMPLETE, None),
        (Outcome.REDIRECT_ERROR, 302, HTTP, Operation.COMPLETE, None),
        (Outcome.INVALID_RESPONSE, None, HTTP, Operation.FAIL, None),
        (Outcome.TLS_ERROR, None, HTTP, Operation.FAIL, None),
        (Outcome.TIMEOUT, None, HTTP, Operation.FAIL, None),
        (Outcome.DNS_ERROR, None, TOR, Operation.FAIL, None),
        (Outcome.NETWORK_ERROR, None, BROWSER, Operation.FAIL, None),
        (Outcome.FETCHER_CRASH, None, BROWSER, Operation.FAIL, None),
        (Outcome.PROXY_UNAVAILABLE, None, TOR, Operation.DEFER, None),
        (Outcome.FETCHER_UNAVAILABLE, None, BROWSER, Operation.DEFER, None),
        (Outcome.CANCELLED, None, HTTP, Operation.DEFER, None),
    ],
)
def test_decide_default_profile(
    outcome: Outcome,
    status: int | None,
    queue: ExecutionQueue,
    op: Operation,
    next_queue: ExecutionQueue | None,
) -> None:
    decision = decide(result(outcome, status), queue, network_offline=False)
    assert decision.operation is op
    assert decision.next_queue is next_queue
    assert decision.consumes_attempt is (op is not Operation.DEFER)
    assert len(decision.reason) <= 64


def test_every_outcome_is_mapped() -> None:
    for outcome in Outcome:
        decide(result(outcome, 200 if outcome.has_response else None), HTTP, network_offline=False)


@pytest.mark.parametrize("outcome", [Outcome.TIMEOUT, Outcome.DNS_ERROR, Outcome.NETWORK_ERROR])
def test_only_a_confirmed_outage_refunds_ambiguous_failures(outcome: Outcome) -> None:
    offline = decide(result(outcome), HTTP, network_offline=True)
    assert offline.operation is Operation.DEFER
    assert not offline.consumes_attempt
    # a TLS failure proves the network worked: never refunded
    assert decide(result(Outcome.TLS_ERROR), HTTP, network_offline=True).operation is Operation.FAIL


# --- signals -----------------------------------------------------------------
def page(body: str, status: int = 200) -> tuple[Outcome, tuple[str, ...]]:
    return signals.inspect_page(status, "https://site.example/", body.encode())


def test_content_page_with_framework_marker_is_ok() -> None:
    body = '<div id="root">' + "".join(f'<a href="/{i}">x</a>' for i in range(10)) + "</div>"
    assert page(body)[0] is Outcome.OK


def test_spa_shell_needs_js() -> None:
    assert page('<div id="root"></div><script src="a.js"></script>')[0] is Outcome.NEEDS_JS
    assert page("<noscript>Please enable JavaScript</noscript>")[0] is Outcome.NEEDS_JS
    assert page("<script></script>" * 6 + "<a href='/'>x</a>")[0] is Outcome.NEEDS_JS


def test_captcha_and_blocks() -> None:
    assert page('<div class="g-recaptcha"></div>')[0] is Outcome.CAPTCHA
    assert page("<title>Just a moment...</title>", 403)[0] is Outcome.BLOCKED
    assert page("anything", 429)[0] is Outcome.BLOCKED
    assert page("forbidden", 403)[0] is Outcome.HTTP_ERROR
    comments = '<div class="g-recaptcha"></div>' + "<a href='/'>x</a>" * 20
    assert page(comments)[0] is Outcome.OK  # a comment-form captcha is not a wall


def test_media_and_manifest_detection() -> None:
    assert signals.is_media("https://c.example/v.mp4", "application/octet-stream")
    assert signals.is_media("https://c.example/stream", "video/mp2t")
    assert not signals.is_media("https://c.example/m.m3u8", "audio/mpegurl")
    assert not signals.is_media("https://c.example/f.bin", "application/octet-stream")
    assert signals.body_kind("https://c.example/x.mpd", None) is BodyKind.MANIFEST
    assert signals.sniff_container(b"\x00\x00\x00\x18ftypmp42") == "mp4"
    assert signals.sniff_container(b"\x1a\x45\xdf\xa3....") == "webm"
    assert signals.sniff_container(b"<html>") is None


# --- classifier ----------------------------------------------------------------
def test_classify_by_type() -> None:
    dns = httpx.ConnectError("x")
    dns.__cause__ = socket.gaierror(-2, "Name or service not known")
    assert classify_exception(dns) is Outcome.DNS_ERROR
    assert classify_exception(httpx.ReadTimeout("x")) is Outcome.TIMEOUT
    assert classify_exception(TimeoutError()) is Outcome.TIMEOUT  # V1 made this "unknown"
    assert classify_exception(ssl.SSLError("bad")) is Outcome.TLS_ERROR
    assert classify_exception(httpx.RemoteProtocolError("x")) is Outcome.INVALID_RESPONSE
    assert classify_exception(ConnectionRefusedError()) is Outcome.NETWORK_ERROR
    # aiohttp-style "ssl:default" boilerplate must not look like TLS (V1 N3 bug)
    assert classify_exception(OSError("Cannot connect ssl:default [None]")) is (
        Outcome.NETWORK_ERROR
    )


def test_classify_chromium_codes() -> None:
    assert classify_exception(Exception("net::ERR_NAME_NOT_RESOLVED at x")) is Outcome.DNS_ERROR
    assert classify_exception(Exception("net::ERR_CERT_DATE_INVALID")) is Outcome.TLS_ERROR
    assert classify_exception(Exception("net::ERR_CONNECTION_REFUSED")) is Outcome.NETWORK_ERROR
    assert classify_exception(Exception("net::ERR_TOO_MANY_REDIRECTS")) is (
        Outcome.REDIRECT_ERROR
    )


# --- network health --------------------------------------------------------------
def _health(probe_results: list[bool]) -> NetworkHealth:
    rounds = iter(probe_results)

    async def probe() -> bool:
        return next(rounds, True)

    settings = NetworkHealthSettings(
        trigger_threshold=3,
        confirm_delay_s=0,
        recovery_probe_interval_s=0.01,
        recovery_confirm_rounds=2,
        probe_endpoints=["http://a.invalid", "http://b.invalid"],
    )
    return NetworkHealth(settings, probe)


def test_dead_targets_do_not_declare_an_outage() -> None:
    async def go() -> NetworkHealth:
        health = _health([True])
        for _ in range(3):
            health.observe(Outcome.TIMEOUT)
        await asyncio.sleep(0.05)
        return health

    health = asyncio.run(go())
    assert health.transitions == [NetworkState.SUSPECT, NetworkState.HEALTHY]


def test_interleaved_success_resets_the_trigger() -> None:
    async def go() -> NetworkHealth:
        health = _health([])
        for _ in range(10):
            health.observe(Outcome.TIMEOUT)
            health.observe(Outcome.TIMEOUT)
            health.observe(Outcome.OK)
        return health

    assert asyncio.run(go()).transitions == []


def test_confirmed_outage_then_recovery() -> None:
    async def go() -> list[NetworkState]:
        health = _health([False, False, False, True, True])
        seen = []
        for _ in range(3):
            health.observe(Outcome.NETWORK_ERROR)
        for _ in range(50):
            await asyncio.sleep(0.01)
            seen.append(health.state)
        await health.close()
        return seen

    seen = asyncio.run(go())
    assert NetworkState.OFFLINE in seen
    assert seen[-1] is NetworkState.HEALTHY


def test_health_requires_two_endpoints() -> None:
    with pytest.raises(ValueError, match="two"):
        NetworkHealthSettings(probe_endpoints=["http://only.one"])


# --- P1 projection -----------------------------------------------------------------
@pytest.mark.parametrize("outcome", list(Outcome))
def test_every_recorded_outcome_is_a_valid_p1_attempt(outcome: Outcome) -> None:
    status = 302 if outcome is Outcome.REDIRECT_ERROR else 200
    res = result(
        outcome,
        status if outcome.has_response or outcome is Outcome.REDIRECT_ERROR else None,
        redirects=(Hop(301, "https://site.example/b"),),
        bytes_read=10,
    )
    now = datetime(2026, 6, 1, tzinfo=UTC)
    attempt = project_attempt(
        UrlRef.of("https://site.example/a"),
        res,
        worker="dev-1:http:1:" + "0" * 32,
        started_at=now,
        finished_at=now + timedelta(seconds=1),
    )
    if p1_outcome(outcome) is None:
        assert attempt is None
        return
    assert attempt is not None
    assert attempt.outcome is p1_outcome(outcome)
    assert attempt.detail is not None
    assert attempt.detail.startswith(f"outcome={outcome.value}")
    if attempt.outcome is FetchOutcome.RESPONSE:
        assert attempt.final is not None


def test_worker_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWLER2_WORKERS__HTTP__CONCURRENCY", "5")
    monkeypatch.setenv("CRAWLER2_WORKERS__BROWSER_ENGINE__RECYCLE_PAGES", "7")
    monkeypatch.setenv("CRAWLER2_WORKERS__FETCH__MEDIA_PROBE_BYTES", "1024")
    settings = Settings()
    assert settings.workers.http.concurrency == 5
    assert settings.workers.browser_engine.recycle_pages == 7
    assert settings.workers.fetch.media_probe_bytes == 1024
