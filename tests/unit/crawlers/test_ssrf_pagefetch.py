"""P14 TC-060..064: SSRF and DNS-rebinding protection of the page-fetch paths.

Adversarial inputs (redirects to metadata/loopback, exotic IP spellings, bad schemes,
answers that change between check and connect) against ``core.netguard`` and the
fetchers built on it. No real DNS and no connection to a non-loopback address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from antipiracy_contracts.models.web import UrlRef
from pydantic import ValidationError

from crawler2.core.configuration import Settings
from crawler2.core.configuration.settings import BrowserSettings, Environment, HttpFetchSettings
from crawler2.core.netguard import (
    GuardedTransport,
    PinnedNetworkBackend,
    UnsafeDestinationError,
    check_url_literal,
    legacy_ip_literal,
    parse_networks,
)
from crawler2.crawlers.browser import _PageObserver
from crawler2.crawlers.decide import decide
from crawler2.crawlers.http import HttpFetcher
from crawler2.crawlers.interception import AllowAll
from crawler2.crawlers.model import FetchRequest, Outcome
from crawler2.crawlers.recorder import p1_outcome
from tests.fixtures.mediaweb import PUBLIC, public_resolver

LOOPBACK = parse_networks(["127.0.0.0/8"])


def _request(url: str) -> FetchRequest:
    return FetchRequest(url=url, url_id=UrlRef.of(url).url_id)


def _fetch(fetcher: HttpFetcher, url: str) -> Any:
    async def go() -> Any:
        try:
            return await fetcher.fetch(_request(url))
        finally:
            await fetcher.close()

    return asyncio.run(go())


def _resolver_to(*answers: str) -> Any:
    """Returns the first answer on the first call, the second on the second, …"""
    calls = iter(answers)
    last = [answers[-1]]

    def resolve(hostname: str, port: int) -> list[tuple[Any, ...]]:
        ip = next(calls, last[0])
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return resolve


# --- literal spellings -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("2130706433", "127.0.0.1"),  # decimal
        ("0x7f000001", "127.0.0.1"),  # hex
        ("0177.0.0.1", "127.0.0.1"),  # octal
        ("127.1", "127.0.0.1"),  # short form
        ("0x7f.1", "127.0.0.1"),
        ("169.254.169.254", "169.254.169.254"),
        ("::ffff:127.0.0.1", "::ffff:7f00:1"),
        ("[::1]", "::1"),
    ],
)
def test_legacy_spellings_resolve_to_their_real_address(host: str, expected: str) -> None:
    assert legacy_ip_literal(host) == ipaddress.ip_address(expected)


@pytest.mark.parametrize("host", ["example.com", "a.b.c", "1e100.net", "deadbeef.example"])
def test_names_are_not_literals(host: str) -> None:
    assert legacy_ip_literal(host) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://2130706433/",
        "http://0x7f.0.0.1/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://10.1.2.3/",
        "http://172.16.0.9:8080/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[fe80::1]/",
        "http://[fd00::1]/",
        "http://localhost/",
        "http://LOCALHOST./",
        "http://app.localhost/",
        "http://printer.local/",
        "http://db.internal/",
        "http://metadata.google.internal/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/",
        "http:///nohost",
    ],
)
def test_literal_check_refuses_hostile_urls(url: str) -> None:
    with pytest.raises((UnsafeDestinationError, httpx.InvalidURL)):
        check_url_literal(url)


@pytest.mark.parametrize("url", ["http://example.com/", "https://sub.example.org:8443/x?y=1"])
def test_literal_check_accepts_public_names(url: str) -> None:
    assert check_url_literal(url)


def test_literal_check_honours_the_exemption_list() -> None:
    assert check_url_literal("http://127.0.0.1:8000/", allowed=LOOPBACK) == "127.0.0.1"
    with pytest.raises(UnsafeDestinationError):
        check_url_literal("http://10.0.0.1/", allowed=LOOPBACK)


# --- direct path: redirects, outcome typing --------------------------------------------


def _redirecting_fetcher(location: str, *, resolver: Any = None) -> tuple[HttpFetcher, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "start.example":
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            stream=httpx.ByteStream(b"<html>secret</html>"),
        )

    guard = GuardedTransport(
        httpx.MockTransport(handler), resolver=resolver or public_resolver(), allowed=()
    )
    return HttpFetcher(HttpFetchSettings(), transport=guard), seen


@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:6379/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "http://internal.example/admin",  # public-looking name that resolves privately
        "file:///etc/passwd",
        "gopher://example.com/",
    ],
)
def test_redirect_hops_are_revalidated(location: str) -> None:
    fetcher, seen = _redirecting_fetcher(location)
    result = _fetch(fetcher, "http://start.example/")
    assert result.outcome in (Outcome.UNSAFE_DESTINATION, Outcome.REDIRECT_ERROR)
    assert result.body is None
    assert seen == ["http://start.example/"], "the hostile hop must never reach the network"


def test_unsafe_destination_is_typed_final_and_recorded() -> None:
    fetcher, _ = _redirecting_fetcher("http://169.254.169.254/")
    result = _fetch(fetcher, "http://start.example/")
    assert result.outcome is Outcome.UNSAFE_DESTINATION
    assert result.error is not None
    assert result.error.kind == "UnsafeDestinationError"
    assert result.redirects[0].location == "http://169.254.169.254/"
    decision = decide(result, queue=_queue(), network_offline=False)
    assert decision.operation.value == "complete", "retrying cannot make the URL safe"
    assert p1_outcome(Outcome.UNSAFE_DESTINATION) is not None


def _queue() -> Any:
    from crawler2.core.configuration import ExecutionQueue

    return ExecutionQueue.HTTP


def test_legitimate_public_destination_still_fetches() -> None:
    fetcher, seen = _redirecting_fetcher("http://other.example/page")
    result = _fetch(fetcher, "http://start.example/")
    assert result.outcome is Outcome.OK
    assert seen == ["http://start.example/", "http://other.example/page"]


def test_default_fetcher_refuses_loopback_without_exemption() -> None:
    result = _fetch(HttpFetcher(HttpFetchSettings()), "http://127.0.0.1:9/")
    assert result.outcome is Outcome.UNSAFE_DESTINATION


# --- proxy (Tor) path ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/", "http://169.254.169.254/", "http://localhost/"]
)
def test_proxied_fetcher_refuses_literals_before_touching_the_proxy(url: str) -> None:
    # Port 1 is closed: reaching the proxy would give proxy/network errors, not a typed refusal.
    fetcher = HttpFetcher(HttpFetchSettings(), proxy="socks5h://127.0.0.1:1")
    assert _fetch(fetcher, url).outcome is Outcome.UNSAFE_DESTINATION


# --- DNS rebinding: the connect uses the validated address ------------------------------


@contextmanager
def _loopback_server() -> Iterator[tuple[int, list[bytes]]]:
    heads: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        heads.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    loop = asyncio.new_event_loop()
    server = loop.run_until_complete(asyncio.start_server(serve, "127.0.0.1", 0))
    port = server.sockets[0].getsockname()[1]
    import threading

    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield port, heads
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        server.close()
        loop.close()


def test_connection_goes_to_the_validated_address_and_keeps_the_host_name() -> None:
    with _loopback_server() as (port, heads):
        settings = HttpFetchSettings(ssrf_allowed_networks=["127.0.0.0/8"])
        fetcher = HttpFetcher(settings, resolver=_resolver_to("127.0.0.1"))
        result = _fetch(fetcher, f"http://pinned.example:{port}/x")
    assert result.outcome is Outcome.OK
    assert heads
    assert b"Host: pinned.example:" in heads[0], "Host must stay the name, not the IP"


def test_dns_answer_that_flips_between_check_and_connect_is_refused() -> None:
    """Rebinding: 1st lookup (guard pre-check) public, 2nd (connect) loopback."""
    with _loopback_server() as (port, heads):
        fetcher = HttpFetcher(HttpFetchSettings(), resolver=_resolver_to(PUBLIC, "127.0.0.1"))
        result = _fetch(fetcher, f"http://rebind.example:{port}/")
    assert result.outcome is Outcome.UNSAFE_DESTINATION
    assert not heads, "no request may reach the loopback server"


def test_connect_validates_every_answer_not_just_the_first() -> None:
    def mixed(hostname: str, port: int) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
        ]

    backend = PinnedNetworkBackend(mixed)
    with pytest.raises(UnsafeDestinationError):
        asyncio.run(backend.connect_tcp("mixed.example", 80))


def test_unix_sockets_are_refused() -> None:
    with pytest.raises(UnsafeDestinationError):
        asyncio.run(PinnedNetworkBackend().connect_unix_socket("/var/run/docker.sock"))


# --- browser ---------------------------------------------------------------------------


def _observer(**kw: Any) -> _PageObserver:
    return _PageObserver(BrowserSettings(**kw.pop("settings", {})), AllowAll(), **kw)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6379/",
        "http://169.254.169.254/",
        "ws://localhost:9222/devtools",
        "file:///etc/passwd",
        "http://internal.example/",
    ],
)
def test_browser_destination_check_refuses_internal_requests(url: str) -> None:
    obs = _observer(resolver=public_resolver())
    assert asyncio.run(obs.destination_ok(url)) is False
    assert obs.unsafe == 1


@pytest.mark.parametrize("url", ["https://example.com/a.js", "data:text/plain,hi", "about:blank"])
def test_browser_destination_check_allows_public_and_local_schemes(url: str) -> None:
    obs = _observer(resolver=public_resolver())
    assert asyncio.run(obs.destination_ok(url)) is True


def test_browser_unresolvable_name_is_left_to_the_browser() -> None:
    obs = _observer(resolver=public_resolver())
    assert asyncio.run(obs.destination_ok("http://nxdomain.example/")) is True


def test_browser_proxy_mode_checks_literals_without_local_dns() -> None:
    def no_dns(hostname: str, port: int) -> Any:
        raise AssertionError("must not resolve locally when a proxy resolves")

    obs = _observer(settings={"proxy": "socks5h://127.0.0.1:9050"}, resolver=no_dns)
    assert asyncio.run(obs.destination_ok("https://example.com/")) is True
    assert asyncio.run(obs.destination_ok("http://10.0.0.1/")) is False


# --- configuration (TC-064) ------------------------------------------------------------


PROD_SECRETS: dict[str, object] = {
    "redis": {"password": "r-secret-not-a-placeholder"},
    "scylla": {"username": "crawler2_app", "password": "s-secret-not-a-placeholder"},
}


def _prod(**override: object) -> Settings:
    base = dict(PROD_SECRETS)
    minio = {"access_key": "crawler2-svc", "secret_key": "a-long-random-secret-value"}
    base["minio"] = {**minio, **dict(override.pop("minio", {}))}  # type: ignore[call-overload]
    return Settings(environment=Environment.PROD, **{**base, **override})  # type: ignore[arg-type]


def test_exemption_lists_are_refused_in_production() -> None:
    Settings(environment=Environment.DEV, media={"ssrf_allowed_networks": ["127.0.0.0/8"]})  # type: ignore[arg-type]
    _prod()
    with pytest.raises(ValidationError, match="ssrf_allowed_networks"):
        _prod(media={"ssrf_allowed_networks": ["127.0.0.0/8"]})
    with pytest.raises(ValidationError, match="ssrf_allowed_networks"):
        _prod(workers={"fetch": {"ssrf_allowed_networks": ["10.0.0.0/8"]}})


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"redis": {}}, "redis.password"),
        ({"scylla": {"username": "u"}}, "scylla.username/password"),
        ({"minio": {"access_key": "minioadmin"}}, "root/default user"),
        ({"minio": {"access_key": "crawler2-dev"}}, "root/default user"),
        ({"minio": {"secret_key": "change-me-dev-only"}}, "placeholder"),
        ({"minio": {"secret_key": "short"}}, "placeholder or shorter"),
    ],
)
def test_production_refuses_unauthenticated_or_root_credentials(
    override: dict[str, object], fragment: str
) -> None:
    with pytest.raises(ValidationError, match=fragment):
        _prod(**override)


def test_development_keeps_working_without_credentials() -> None:
    Settings(environment=Environment.DEV)
    Settings(environment=Environment.TEST)
