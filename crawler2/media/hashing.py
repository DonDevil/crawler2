"""The level-3 hash reader: ``full-bytes/v1`` = SHA-256 of the entire byte sequence
served for a media locator (design §3.4, P8-D5, plan PC-1).

One streamed GET per media content version; bytes are hashed as they arrive
and discarded (nothing is stored).

- **Identity bytes only.** The request sends ``Accept-Encoding: identity``.
  A response with any other ``Content-Encoding`` is not hashed (outcome
  ``content_encoded``): hashing a decoded or re-compressed representation would
  not be the served byte sequence. Bytes are read with ``aiter_raw`` — after
  HTTP/1.1 transfer framing (chunked) is removed by the protocol layer, before
  any content decoding.
- **Bounded** by ``media.hash_max_bytes`` (declared length checked first, then
  counted while streaming), ``media.hash_timeout_s`` for the whole read and
  ``media.hash_read_timeout_s`` of inactivity; redirects are followed by hand
  (≤ ``workers.fetch.max_redirects``), each hop through the SSRF guard.
- **One version.** The response's validators (length, ETag, Last-Modified)
  must match the probe that wanted the hash, and a HEAD after the read must
  show the same validators; otherwise ``changed_during_read``. A read that
  ends short of the declared length is ``unreachable``.
- No ContentId unless the outcome is ``ok``; failures carry a reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import httpx

from crawler2.media.model import SCHEME, HashOutcome, HashRecord, ProbeRecord, Validators
from crawler2.media.ssrf import UnsafeDestinationError

_REDIRECTS = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True, slots=True)
class HashLimits:
    max_bytes: int = 8 * 1024**3
    timeout_s: float = 3600.0
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 60.0
    max_redirects: int = 10
    user_agent: str = "crawler2/0.1"


def _int(value: str | None) -> int | None:
    return int(value) if value is not None and value.strip().isdigit() else None


def response_validators(headers: Mapping[str, str]) -> Validators:
    return Validators(
        length=_int(headers.get("content-length")),
        etag=headers.get("etag"),
        last_modified=headers.get("last-modified"),
    )


def same_version(a: Validators, b: Validators) -> bool:
    """Equal wherever both sides state a validator (a missing one proves nothing)."""
    pairs = ((a.length, b.length), (a.etag, b.etag), (a.last_modified, b.last_modified))
    return all(x == y for x, y in pairs if x is not None and y is not None)


class _FailedError(Exception):
    def __init__(self, outcome: HashOutcome, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail
        self.status = status


class FullContentHasher:
    def __init__(self, transport: httpx.AsyncBaseTransport, limits: HashLimits) -> None:
        self._limits = limits
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                connect=limits.connect_timeout_s,
                read=limits.read_timeout_s,
                write=limits.read_timeout_s,
                pool=limits.connect_timeout_s,
            ),
            headers={
                "User-Agent": limits.user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def hash(
        self,
        probe: ProbeRecord,
        *,
        worker: str,
        now: datetime,
        referer: str | None = None,
    ) -> HashRecord:
        started = time.monotonic()
        state = _Read(probe.locator_url)
        try:
            async with asyncio.timeout(self._limits.timeout_s):
                digest = await self._read(probe, state, referer)
            outcome, detail, status = HashOutcome.OK, None, state.status
        except _FailedError as fail:
            digest, outcome, detail, status = None, fail.outcome, fail.detail, fail.status
        except TimeoutError:
            digest, outcome, detail, status = None, HashOutcome.TIMEOUT, "total", state.status
        except UnsafeDestinationError as exc:
            digest, outcome, status = None, HashOutcome.FORBIDDEN, state.status
            detail = f"unsafe_destination: {str(exc)[:150]}"
        except httpx.TimeoutException as exc:
            digest, outcome, status = None, HashOutcome.TIMEOUT, state.status
            detail = type(exc).__name__
        except (httpx.HTTPError, OSError) as exc:
            digest, outcome, status = None, HashOutcome.UNREACHABLE, state.status
            detail = f"{type(exc).__name__}: {str(exc)[:150]}"
        return HashRecord(
            hash_id=str(uuid.uuid4()),
            media_id=probe.media_id,
            locator_url=probe.locator_url,
            hashed_at=now,
            outcome=outcome,
            worker=worker,
            probe_id=probe.probe_id,
            validators=probe.validators,
            scheme=SCHEME,
            digest=digest,
            byte_count=state.bytes,
            final_url=state.url,
            redirect_count=state.hops,
            http_status=status,
            detail=detail,
            elapsed_s=round(time.monotonic() - started, 3),
        )

    async def _read(self, probe: ProbeRecord, state: _Read, referer: str | None) -> str:
        limits = self._limits
        seen = {state.url}
        headers = {"Referer": referer} if referer else {}
        while True:
            request = self._client.build_request("GET", state.url, headers=headers)
            response = await self._client.send(request, stream=True)
            try:
                state.status = response.status_code
                location = response.headers.get("location")
                if response.status_code in _REDIRECTS and location:
                    target = urljoin(state.url, location.strip())
                    if urlsplit(target).scheme not in ("http", "https"):
                        raise _FailedError(HashOutcome.UNREACHABLE, "redirect to a non-http scheme")
                    if target in seen or state.hops >= limits.max_redirects:
                        raise _FailedError(
                            HashOutcome.UNREACHABLE, "redirect loop or too many hops"
                        )
                    seen.add(target)
                    state.hops += 1
                    state.url = target
                    continue
                return await self._body(probe, state, response)
            finally:
                await response.aclose()

    async def _body(self, probe: ProbeRecord, state: _Read, response: httpx.Response) -> str:
        status = response.status_code
        if status in (401, 403):
            raise _FailedError(HashOutcome.FORBIDDEN, f"http {status}", status)
        if status in (404, 410):
            raise _FailedError(HashOutcome.UNREACHABLE, f"http {status}", status)
        if status != 200:
            raise _FailedError(HashOutcome.HTTP_ERROR, f"http {status}", status)
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding and encoding != "identity":
            raise _FailedError(
                HashOutcome.CONTENT_ENCODED, f"content-encoding {encoding[:40]}", status
            )
        served = response_validators(response.headers)
        if not same_version(served, probe.validators):
            raise _FailedError(
                HashOutcome.CHANGED_DURING_READ, "validators differ from the probe", status
            )
        if served.length is not None and served.length > self._limits.max_bytes:
            raise _FailedError(HashOutcome.TOO_LARGE, f"declared {served.length} bytes", status)
        sha = hashlib.sha256()
        async for chunk in response.aiter_raw():
            state.bytes += len(chunk)
            if state.bytes > self._limits.max_bytes:
                raise _FailedError(
                    HashOutcome.TOO_LARGE, f"over {self._limits.max_bytes} bytes", status
                )
            sha.update(chunk)
        if served.length is not None and state.bytes != served.length:
            raise _FailedError(
                HashOutcome.UNREACHABLE, f"read {state.bytes} of {served.length} bytes", status
            )
        await self._unchanged(state, served)
        return "sha256:" + sha.hexdigest()

    async def _unchanged(self, state: _Read, served: Validators) -> None:
        """A HEAD after the read must still describe the version that was read."""
        try:
            head = await self._client.head(state.url)
        except httpx.HTTPError:
            return  # no answer is no evidence of a change; the GET itself was complete
        if head.status_code != 200:
            return
        after = response_validators(head.headers)
        if not same_version(served, after):
            raise _FailedError(
                HashOutcome.CHANGED_DURING_READ, "validators changed during the read", 200
            )


class _Read:
    def __init__(self, url: str) -> None:
        self.url = url
        self.hops = 0
        self.bytes = 0
        self.status: int | None = None
