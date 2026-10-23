"""P6 X-15: a body whose declared size exceeds the limit is refused without being streamed."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration.settings import HttpFetchSettings
from crawler2.core.netguard import GuardedTransport
from crawler2.crawlers.http import HttpFetcher
from crawler2.crawlers.model import FetchRequest, Outcome
from tests.fixtures.mediaweb import public_resolver


class _Explodes(httpx.AsyncByteStream):
    """Fails the test if the fetcher reads any of the body."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise AssertionError("bulk body was streamed")
        yield b""  # pragma: no cover


def _fetch(headers: dict[str, str], stream: httpx.AsyncByteStream, limit: int = 1000) -> Outcome:
    transport = GuardedTransport(
        httpx.MockTransport(lambda r: httpx.Response(200, headers=headers, stream=stream)),
        resolver=public_resolver(),
    )
    fetcher = HttpFetcher(HttpFetchSettings(max_body_bytes=limit), transport=transport)

    async def go() -> Outcome:
        try:
            url = "http://files.example/big.tar"
            return (
                await fetcher.fetch(FetchRequest(url=url, url_id=UrlRef.of(url).url_id))
            ).outcome
        finally:
            await fetcher.close()

    return asyncio.run(go())


def test_declared_oversize_body_is_not_read() -> None:
    headers = {"content-type": "application/octet-stream", "content-length": "5000000"}
    assert _fetch(headers, _Explodes()) is Outcome.TOO_LARGE


def test_declared_size_within_the_limit_is_read() -> None:
    body = b"x" * 500
    headers = {"content-type": "application/octet-stream", "content-length": "500"}
    assert _fetch(headers, httpx.ByteStream(body)) is Outcome.OK


def test_undeclared_oversize_body_still_hits_the_streaming_bound() -> None:
    headers = {"content-type": "application/octet-stream"}
    assert _fetch(headers, httpx.ByteStream(b"x" * 5000)) is Outcome.TOO_LARGE
