"""SSRF guard, ported from the fingerprinter's ``tests/test_acquisition_ssrf.py`` (same
cases, same "no real DNS, no sockets past the guard" rule), plus redirect-to-private
and redirect-to-loopback through **both** P8 paths: the probe (P4 ``HttpFetcher`` with
the guarded transport) and the full-content hash reader (design §5.3)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from antipiracy_contracts.ids import MediaId
from antipiracy_contracts.models.media import MediaKind, ProbeStatus
from antipiracy_contracts.models.web import FetchCapability, UrlRef

from crawler2.core.configuration import MediaSettings
from crawler2.core.configuration.settings import HttpFetchSettings
from crawler2.crawlers.model import ErrorInfo, Expectation, FetchRequest, FetchResult, Outcome
from crawler2.media.hashing import FullContentHasher, HashLimits
from crawler2.media.model import HashOutcome, HashRecord, ProbeRecord
from crawler2.media.probe import guarded, probe_fetcher, probe_record, probe_status
from crawler2.media.ssrf import (
    GuardedTransport,
    UnsafeDestinationError,
    is_unsafe_address,
    parse_networks,
    validate_destination,
)
from crawler2.media.work import Job
from tests.fixtures.mediaweb import (
    MediaSite,
    NeverCalled,
    RoutingTransport,
    blob,
    media_site,
    public_resolver,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)


async def _through_guard(url: str, *, inner: httpx.AsyncBaseTransport | None = None) -> None:
    transport = GuardedTransport(inner or NeverCalled(), resolver=public_resolver())
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get(url)


def _rejected(url: str) -> None:
    with pytest.raises(UnsafeDestinationError):
        asyncio.run(_through_guard(url))


# --- ported address cases (fingerprinter test_acquisition_ssrf.py) -----------------------


@pytest.mark.parametrize("url", ["http://127.0.0.1:80/video.mp4", "http://127.1.2.3:80/video.mp4"])
def test_ipv4_loopback_rejected(url: str) -> None:
    _rejected(url)


def test_ipv6_loopback_rejected() -> None:
    _rejected("http://[::1]:80/video.mp4")


@pytest.mark.parametrize(
    "url",
    ["http://10.1.2.3/video.mp4", "http://172.16.5.5/video.mp4", "http://192.168.0.9/video.mp4"],
)
def test_rfc1918_private_ipv4_rejected(url: str) -> None:
    _rejected(url)


def test_ipv6_private_local_address_rejected() -> None:
    _rejected("http://[fd12:3456:789a::1]/video.mp4")


@pytest.mark.parametrize(
    "url", ["http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/video.mp4"]
)
def test_link_local_address_rejected(url: str) -> None:
    _rejected(url)


@pytest.mark.parametrize("url", ["http://0.0.0.0/video.mp4", "http://[::]/video.mp4"])
def test_unspecified_address_rejected(url: str) -> None:
    _rejected(url)


@pytest.mark.parametrize(
    "url",
    ["http://224.0.0.1/video.mp4", "http://240.0.0.1/video.mp4", "http://[ff02::1]/video.mp4"],
)
def test_multicast_and_reserved_addresses_rejected(url: str) -> None:
    _rejected(url)


def test_carrier_grade_nat_range_rejected() -> None:
    assert is_unsafe_address(ipaddress.ip_address("100.64.1.1")) is True
    _rejected("http://100.64.1.1/video.mp4")


def test_ipv4_mapped_ipv6_is_checked_as_ipv4() -> None:
    assert is_unsafe_address(ipaddress.ip_address("::ffff:127.0.0.1")) is True
    assert is_unsafe_address(ipaddress.ip_address("::ffff:93.184.216.34")) is False


def test_any_unsafe_record_rejects_the_host() -> None:
    def two(hostname: str, port: int) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
        ]

    with pytest.raises(UnsafeDestinationError):
        validate_destination("mixed.example", 80, two)


def test_normal_public_destination_remains_allowed() -> None:
    assert validate_destination("cdn.example", 443, public_resolver())


def test_explicit_allowlist_is_the_only_opt_out() -> None:
    allowed = parse_networks(["10.20.30.0/24"])
    resolver = public_resolver(private=("internal.example",))
    assert validate_destination("internal.example", 80, resolver, allowed=allowed)
    with pytest.raises(UnsafeDestinationError):
        validate_destination("internal.example", 80, resolver)
    assert MediaSettings().ssrf_allowed_networks == []  # production default: none


def test_dns_resolution_failure_is_a_connect_error_not_ssrf() -> None:
    with pytest.raises(httpx.ConnectError) as info:
        asyncio.run(_through_guard("http://nxdomain.example/v.mp4"))
    assert not isinstance(info.value, UnsafeDestinationError)
    assert isinstance(info.value.__cause__, socket.gaierror)


@pytest.mark.parametrize("url", ["ftp://cdn.example/v.mp4", "file:///etc/passwd"])
def test_only_http_and_https(url: str) -> None:
    transport = GuardedTransport(NeverCalled(), resolver=public_resolver())

    async def go() -> None:
        request = httpx.Request("GET", url)
        await transport.handle_async_request(request)

    with pytest.raises(UnsafeDestinationError):
        asyncio.run(go())


# --- both P8 paths: every redirect hop is re-checked ---------------------------------------


@pytest.fixture
def site() -> Iterator[MediaSite]:
    with media_site() as s:
        yield s


def _probe_job(url: str) -> Job:
    return Job(str(MediaId.of_locator(UrlRef.of(url).url)), UrlRef.of(url).url, "video_file")


async def _probe(site: MediaSite, url: str) -> ProbeRecord:
    rejected: list[str] = []
    transport = guarded(
        MediaSettings(),
        resolver=public_resolver(),
        inner=RoutingTransport(site),
        on_reject=rejected.append,
    )
    fetcher = probe_fetcher(HttpFetchSettings(), transport, connections=4)
    try:
        result = await fetcher.fetch(
            FetchRequest(url=url, url_id=UrlRef.of(url).url_id, expect=Expectation.MEDIA_PROBE)
        )
    finally:
        await fetcher.close()
    return probe_record(_probe_job(url), result, worker="t", at=NOW)


def _probed(url: str) -> ProbeRecord:
    return ProbeRecord(
        probe_id="p",
        media_id=str(MediaId.of_locator(UrlRef.of(url).url)),
        locator_url=UrlRef.of(url).url,
        kind=MediaKind.VIDEO_FILE,
        probed_at=NOW,
        probe_status=ProbeStatus.PROBED,
        worker="t",
        fetch_outcome="media",
    )


async def _hash(site: MediaSite, url: str) -> HashRecord:
    transport = guarded(MediaSettings(), resolver=public_resolver(), inner=RoutingTransport(site))
    hasher = FullContentHasher(transport, HashLimits())
    try:
        return await hasher.hash(_probed(url), worker="t", now=NOW)
    finally:
        await hasher.close()


@pytest.mark.parametrize(
    "target",
    [
        "http://internal.example/secret",
        "http://127.0.0.1/secret",
        "http://[::1]/x",
        "http://169.254.169.254/",
    ],
)
def test_redirect_to_private_or_loopback_is_refused_on_both_paths(
    site: MediaSite, target: str
) -> None:
    site.put("/r.mp4", redirect=target)
    url = "https://cdn.example/r.mp4"
    probe = asyncio.run(_probe(site, url))
    assert probe.probe_status is ProbeStatus.FORBIDDEN
    assert probe.fetch_detail == "unsafe_destination"
    record = asyncio.run(_hash(site, url))
    assert record.outcome is HashOutcome.FORBIDDEN
    assert str(record.detail).startswith("unsafe_destination")
    assert record.content_id is None
    # Only the first (public) hop was ever sent, never the internal one.
    assert len(site.seen("/r.mp4")) == 2


def test_redirect_between_public_hosts_still_works_on_both_paths(site: MediaSite) -> None:
    body = blob("public", 200_000)
    site.put("/final.mp4", body)
    site.put("/start.mp4", redirect="https://cdn2.example/final.mp4")
    probe = asyncio.run(_probe(site, "https://cdn.example/start.mp4"))
    assert probe.probe_status is ProbeStatus.PROBED
    assert probe.redirect_count == 1
    assert probe.final_url == "https://cdn2.example/final.mp4"
    record = asyncio.run(_hash(site, "https://cdn.example/start.mp4"))
    assert record.outcome is HashOutcome.OK
    assert record.redirect_count == 1


def test_probe_status_of_an_unsafe_failure() -> None:
    result = FetchResult(
        outcome=Outcome.NETWORK_ERROR,
        capability=FetchCapability.HTTP,
        requested_url="http://10.0.0.1/x",
        error=ErrorInfo(kind="UnsafeDestinationError", message="x"),
    )
    assert probe_status(result, MediaKind.VIDEO_FILE) == (
        ProbeStatus.FORBIDDEN,
        "unsafe_destination",
    )
