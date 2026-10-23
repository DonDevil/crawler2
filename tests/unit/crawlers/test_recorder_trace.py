"""TC-055: the events one recorded attempt causes start a shared correlation chain."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.models.web import FetchCapability, UrlRef

from crawler2.crawlers.model import FetchResult, Outcome
from crawler2.crawlers.recorder import StorageRecorder


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def record(self, row: Any, *, event: Any = None) -> None:
        self.events.append(event)


class _Objects:
    def put(self, data: bytes, *, media_type: str | None = None) -> BlobRef:
        from antipiracy_contracts.digests import ContentDigest

        return BlobRef(
            uri="s3://bucket/key",
            digest=ContentDigest.of_bytes(data),
            size_bytes=len(data),
            media_type=media_type or "text/html",
        )


def test_fetch_completed_and_page_observed_share_one_correlation_id() -> None:
    attempts, pages = _Sink(), _Sink()
    recorder = StorageRecorder(attempts, pages, _Objects(), worker="dev-1:http:1:" + "0" * 32)  # type: ignore[arg-type]
    url = UrlRef.of("http://example.com/")
    result = FetchResult(
        outcome=Outcome.OK,
        capability=FetchCapability.HTTP,
        requested_url=url.url,
        final_url=url.url,
        status=200,
        content_type="text/html",
        body=b"<html>hi</html>",
    )
    now = datetime.now(UTC)
    recorder.record(url, result, started_at=now, finished_at=now)
    fetch, page = attempts.events[0], pages.events[0]
    assert fetch.correlation_id is not None
    assert fetch.correlation_id == page.correlation_id


def test_each_attempt_starts_its_own_chain() -> None:
    attempts, pages = _Sink(), _Sink()
    recorder = StorageRecorder(attempts, pages, _Objects(), worker="dev-1:http:1:" + "0" * 32)  # type: ignore[arg-type]
    url = UrlRef.of("http://example.com/")
    result = FetchResult(
        outcome=Outcome.TIMEOUT, capability=FetchCapability.HTTP, requested_url=url.url
    )
    now = datetime.now(UTC)
    recorder.record(url, result, started_at=now, finished_at=now)
    recorder.record(url, result, started_at=now, finished_at=now)
    first, second = (e.correlation_id for e in attempts.events)
    assert first is not None
    assert first != second
    assert pages.events == [], "no body: no page observation"
