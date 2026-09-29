"""HTTP fetcher (httpx): one attempt, manual redirects, bounded reads, media probe.

Design §11, §15, §17, §18. Guarantees:

- Redirects are followed hop by hop (≤ ``max_redirects``, loop detection,
  http/https only) and every hop is recorded.
- The whole attempt runs under ``total_timeout_s``; ``read_timeout_s``
  bounds inactivity, so a trickling (slowloris) server hits the total.
- Bodies are read raw and decoded here with an output bound, so neither a
  large body nor a compression bomb can exceed ``max_body_bytes``.
- A media response is recognised from its headers and at most
  ``media_probe_bytes`` of its body are read; URLs with a media extension
  are requested with ``Range: bytes=0-(N-1)`` (D2).
- No cookies survive an attempt; no browser-impersonation headers.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import time
import zlib
from collections.abc import Mapping
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

import brotli
import httpx
from antipiracy_contracts.models.web import FetchCapability, HttpValidators

from crawler2.core.configuration.settings import HttpFetchSettings
from crawler2.crawlers import signals
from crawler2.crawlers.classify import classify_exception, error_info
from crawler2.crawlers.model import (
    BodyKind,
    Conditional,
    Expectation,
    FetcherHealth,
    FetcherState,
    FetchRequest,
    FetchResult,
    Hop,
    MediaProbe,
    Outcome,
    Timings,
)

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_DROPPED_HEADERS = frozenset({"set-cookie", "set-cookie2", "authorization", "proxy-authenticate"})
_MAX_HEADERS = 64
_MAX_HEADER_VALUE = 500
_SNIFF = 4096


class _TooLargeError(Exception):
    pass


class _BadEncodingError(Exception):
    pass


class _BoundedDecoder:
    """Content-Encoding decoder whose output can never exceed ``limit`` (+1 to detect it)."""

    def __init__(self, encoding: str, limit: int) -> None:
        self._limit = limit
        self._size = 0
        self._zlib: zlib._Decompress | None = None
        self._brotli: brotli.Decompressor | None = None
        self._deflate_probe = False
        codings = [c.strip() for c in encoding.lower().split(",") if c.strip()]
        codings = [c for c in codings if c != "identity"]
        if len(codings) > 1:
            raise _BadEncodingError(f"stacked content-encoding {encoding!r}")
        coding = codings[0] if codings else ""
        if coding in ("gzip", "x-gzip"):
            self._zlib = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif coding == "deflate":
            self._zlib = zlib.decompressobj(zlib.MAX_WBITS)
            self._deflate_probe = True
        elif coding == "br":
            self._brotli = brotli.Decompressor()
        elif coding:
            raise _BadEncodingError(f"unsupported content-encoding {coding!r}")

    def _room(self) -> int:
        return self._limit - self._size + 1

    def _emit(self, out: bytes) -> bytes:
        self._size += len(out)
        if self._size > self._limit:
            raise _TooLargeError
        return out

    def feed(self, data: bytes) -> bytes:
        try:
            if self._zlib is not None:
                return self._feed_zlib(data)
            if self._brotli is not None:
                return self._feed_brotli(data)
        except (zlib.error, brotli.error) as exc:
            raise _BadEncodingError(str(exc)) from exc
        return self._emit(data)

    def _feed_zlib(self, data: bytes) -> bytes:
        assert self._zlib is not None  # noqa: S101 -- guarded by feed()
        if self._deflate_probe:
            self._deflate_probe = False
            try:
                return self._drain_zlib(data)
            except zlib.error:  # raw deflate without zlib header (common server bug)
                self._zlib = zlib.decompressobj(-zlib.MAX_WBITS)
        return self._drain_zlib(data)

    def _drain_zlib(self, data: bytes) -> bytes:
        assert self._zlib is not None  # noqa: S101
        parts = [self._emit(self._zlib.decompress(data, self._room()))]
        while self._zlib.unconsumed_tail:
            parts.append(
                self._emit(self._zlib.decompress(self._zlib.unconsumed_tail, self._room()))
            )
        return b"".join(parts)

    def _feed_brotli(self, data: bytes) -> bytes:
        assert self._brotli is not None  # noqa: S101
        parts = [self._emit(self._brotli.process(data, output_buffer_limit=self._room()))]
        while not self._brotli.can_accept_more_data():
            parts.append(self._emit(self._brotli.process(b"", output_buffer_limit=self._room())))
        return b"".join(parts)


class _RejectAllCookies(http.cookiejar.DefaultCookiePolicy):
    """The shared client never stores cookies; each attempt keeps its own jar."""

    def set_ok(self, cookie: http.cookiejar.Cookie, request: object) -> bool:
        return False


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    kept: dict[str, str] = {}
    for name, value in headers.multi_items():
        key = name.lower()
        if key in _DROPPED_HEADERS or key in kept or len(kept) >= _MAX_HEADERS:
            continue
        kept[key] = value[:_MAX_HEADER_VALUE]
    return kept


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name)
    if value is None or not value.strip().isdigit():
        return None
    return int(value.strip())


def _content_range_total(value: str | None) -> int | None:
    if not value or "/" not in value:
        return None
    total = value.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None


def _validators(headers: Mapping[str, str]) -> HttpValidators | None:
    etag = headers.get("etag")
    last_modified = headers.get("last-modified")
    if not etag and not last_modified:
        return None
    return HttpValidators(
        etag=etag[:500] if etag else None,
        last_modified=last_modified[:100] if last_modified else None,
    )


class HttpFetcher:
    """Plain HTTP(S). With ``proxy`` set to a SOCKS URL it is the Tor transport."""

    def __init__(
        self,
        settings: HttpFetchSettings,
        *,
        capability: FetchCapability = FetchCapability.HTTP,
        proxy: str | None = None,
        max_connections: int = 64,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._capability = capability
        self._proxy = proxy if proxy is not None else settings.proxy
        self._max_connections = max_connections
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def capability(self) -> FetchCapability:
        return self._capability

    @property
    def total_timeout_s(self) -> float:
        return self._settings.total_timeout_s

    async def start(self) -> None:
        if self._client is not None:
            return
        s = self._settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=s.connect_timeout_s,
                read=s.read_timeout_s,
                write=s.read_timeout_s,
                pool=s.connect_timeout_s,
            ),
            limits=httpx.Limits(
                max_connections=self._max_connections,
                max_keepalive_connections=self._max_connections,
            ),
            follow_redirects=False,
            trust_env=False,
            proxy=self._proxy,
            transport=self._transport,
            cookies=http.cookiejar.CookieJar(policy=_RejectAllCookies()),
            headers={
                "User-Agent": s.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en;q=0.9,*;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
            },
        )

    def health(self) -> FetcherHealth:
        if self._client is None:
            return FetcherHealth(FetcherState.UNAVAILABLE, "not started")
        return FetcherHealth(FetcherState.READY)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, request: FetchRequest) -> FetchResult:
        if self._client is None:
            await self.start()
        started = time.monotonic()
        state = _Attempt(request)
        try:
            async with asyncio.timeout(self._settings.total_timeout_s):
                result = await self._run(request, state, started)
        except TimeoutError as exc:
            result = state.failure(self, Outcome.TIMEOUT, exc)
        except _TooLargeError:
            result = state.failure(self, Outcome.TOO_LARGE, None)
        except _BadEncodingError as exc:
            result = state.failure(self, Outcome.INVALID_RESPONSE, exc)
        except httpx.InvalidURL as exc:
            result = state.failure(self, Outcome.REDIRECT_ERROR, exc)
        except httpx.UnsupportedProtocol as exc:
            result = state.failure(self, Outcome.REDIRECT_ERROR, exc)
        except (httpx.HTTPError, OSError) as exc:
            outcome = classify_exception(exc)
            if outcome is Outcome.NETWORK_ERROR and await self.proxy_unavailable():
                outcome = Outcome.PROXY_UNAVAILABLE
            result = state.failure(self, outcome, exc)
        timings = replace(result.timings, total_s=round(time.monotonic() - started, 4))
        return replace(result, timings=timings, bytes_read=state.bytes_read)

    async def proxy_unavailable(self) -> bool:
        """Whether a failure was the local proxy's (overridden by the Tor fetcher)."""
        return False

    async def _run(self, request: FetchRequest, state: _Attempt, started: float) -> FetchResult:
        assert self._client is not None  # noqa: S101
        s = self._settings
        url = request.url
        cookies = httpx.Cookies()
        seen = {url}
        conditional = Conditional.NONE
        while True:
            headers: dict[str, str] = {}
            ranged = request.expect is Expectation.MEDIA_PROBE or signals.has_media_extension(url)
            if ranged:
                headers["Range"] = f"bytes=0-{s.media_probe_bytes - 1}"
            if not state.hops and request.validators is not None:
                if request.validators.etag:
                    headers["If-None-Match"] = request.validators.etag
                if request.validators.last_modified:
                    headers["If-Modified-Since"] = request.validators.last_modified
                conditional = Conditional.SENT if headers.keys() - {"Range"} else Conditional.NONE
            outgoing = self._client.build_request("GET", url, headers=headers)
            cookies.set_cookie_header(outgoing)
            response = await self._client.send(outgoing, stream=True)
            try:
                ttfb = time.monotonic() - started
                cookies.extract_cookies(response)
                status = response.status_code
                location = response.headers.get("location")
                if status in _REDIRECTS and location:
                    target = urljoin(url, location.strip())
                    if urlsplit(target).scheme not in ("http", "https"):
                        return state.result(self, Outcome.REDIRECT_ERROR, response, url, ttfb)
                    if target in seen or len(state.hops) >= s.max_redirects:
                        state.hops.append(Hop(status, target))
                        return state.result(self, Outcome.REDIRECT_ERROR, response, url, ttfb)
                    state.hops.append(Hop(status, target))
                    seen.add(target)
                    url = target
                    continue
                return await self._final(request, state, response, url, ttfb, conditional, ranged)
            finally:
                state.bytes_read += response.num_bytes_downloaded
                await response.aclose()

    async def _final(
        self,
        request: FetchRequest,
        state: _Attempt,
        response: httpx.Response,
        url: str,
        ttfb: float,
        conditional: Conditional,
        ranged: bool,
    ) -> FetchResult:
        s = self._settings
        status = response.status_code
        content_type = response.headers.get("content-type")
        if status == 304 and conditional is Conditional.SENT:
            result = state.result(self, Outcome.NOT_MODIFIED, response, url, ttfb)
            return replace(result, conditional=Conditional.NOT_MODIFIED)
        if status < 400 and signals.is_media(url, content_type):
            prefix = await self._read_raw(response, s.media_probe_bytes, state)
            headers = response.headers
            probe = MediaProbe(
                content_type=content_type,
                declared_length=_int_header(_safe_headers(headers), "content-length"),
                total_length=_content_range_total(headers.get("content-range")),
                accept_ranges=headers.get("accept-ranges"),
                range_requested=f"bytes=0-{s.media_probe_bytes - 1}" if ranged else None,
                range_honoured=status == 206,
                etag=headers.get("etag"),
                last_modified=headers.get("last-modified"),
                bytes_read=len(prefix),
                container=signals.sniff_container(prefix[:_SNIFF]),
            )
            result = state.result(self, Outcome.MEDIA, response, url, ttfb)
            return replace(result, media=probe)
        kind = signals.body_kind(url, content_type)
        limit = s.max_manifest_bytes if kind is BodyKind.MANIFEST else s.max_body_bytes
        decoder = _BoundedDecoder(response.headers.get("content-encoding", ""), limit)
        chunks: list[bytes] = []
        async for raw in response.aiter_raw():
            chunks.append(decoder.feed(raw))
        body = b"".join(chunks)
        if kind is BodyKind.HTML:
            outcome, found = signals.inspect_page(status, url, body[: s.sniff_bytes])
        else:
            outcome, found = (Outcome.HTTP_ERROR if status >= 400 else Outcome.OK), ()
        result = state.result(self, outcome, response, url, ttfb)
        return replace(
            result,
            body=body,
            body_kind=kind,
            signals=found,
            conditional=conditional,
        )

    @staticmethod
    async def _read_raw(response: httpx.Response, limit: int, state: _Attempt) -> bytes:
        """Read at most ``limit`` body bytes, then stop (the caller closes the stream)."""
        parts: list[bytes] = []
        size = 0
        async for raw in response.aiter_raw():
            take = raw[: limit - size]
            parts.append(take)
            size += len(take)
            if size >= limit:
                break
        return b"".join(parts)


class _Attempt:
    """Mutable bookkeeping of one attempt (hops, bytes) shared with error paths."""

    def __init__(self, request: FetchRequest) -> None:
        self.request = request
        self.hops: list[Hop] = []
        self.bytes_read = 0

    def result(
        self,
        fetcher: HttpFetcher,
        outcome: Outcome,
        response: httpx.Response,
        url: str,
        ttfb: float,
    ) -> FetchResult:
        headers = _safe_headers(response.headers)
        return FetchResult(
            outcome=outcome,
            capability=fetcher.capability,
            requested_url=self.request.url,
            final_url=url,
            redirects=tuple(self.hops),
            status=response.status_code,
            headers=headers,
            content_type=headers.get("content-type"),
            content_length=_int_header(headers, "content-length"),
            validators=_validators(headers),
            timings=Timings(ttfb_s=round(ttfb, 4)),
        )

    def failure(
        self, fetcher: HttpFetcher, outcome: Outcome, exc: BaseException | None
    ) -> FetchResult:
        return FetchResult(
            outcome=outcome,
            capability=fetcher.capability,
            requested_url=self.request.url,
            final_url=self.hops[-1].location if self.hops else None,
            redirects=tuple(self.hops),
            error=error_info(exc) if exc is not None else None,
        )
