"""Durable record of an attempt through the P2 repositories (design §2, §17).

``crawler_worker`` owns ``fetch.completed`` (W1) and ``page.observed``
(W4) in the P1 catalog, and reads W5 for conditional-request validators.
The raw body goes to the object store first, so a ``PageObservation``
never points at a missing snapshot; the rows and their events are written
together through the outbox (ADR-013). Deferred attempts are not recorded:
nothing was tried against the target.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Protocol

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events import new_event
from antipiracy_contracts.events.web import FetchCompleted, PageObserved
from antipiracy_contracts.ids import FetchAttemptId, ObservationId, PageVersionId, UrlId
from antipiracy_contracts.models.web import (
    FetchAttempt,
    FetchOutcome,
    HttpValidators,
    PageObservation,
    RedirectHop,
    UrlRef,
)
from antipiracy_contracts.ownership import Component, Producer

from crawler2.crawlers.model import FetchResult, Outcome
from crawler2.storage.errors import StorageError
from crawler2.storage.objectstore.base import ObjectStore
from crawler2.storage.repositories import FetchAttemptRepository, PageObservationRepository

_P1_OUTCOME = {
    Outcome.OK: FetchOutcome.RESPONSE,
    Outcome.NOT_MODIFIED: FetchOutcome.RESPONSE,
    Outcome.HTTP_ERROR: FetchOutcome.RESPONSE,
    Outcome.NEEDS_JS: FetchOutcome.RESPONSE,
    Outcome.MEDIA: FetchOutcome.RESPONSE,
    Outcome.BLOCKED: FetchOutcome.BLOCKED,
    Outcome.CAPTCHA: FetchOutcome.BLOCKED,
    Outcome.TIMEOUT: FetchOutcome.TIMEOUT,
    Outcome.DNS_ERROR: FetchOutcome.DNS_FAILURE,
    Outcome.NETWORK_ERROR: FetchOutcome.CONNECTION_FAILURE,
    Outcome.REDIRECT_ERROR: FetchOutcome.CONNECTION_FAILURE,
    Outcome.INVALID_RESPONSE: FetchOutcome.CONNECTION_FAILURE,
    Outcome.TLS_ERROR: FetchOutcome.TLS_FAILURE,
    Outcome.TOO_LARGE: FetchOutcome.TOO_LARGE,
    Outcome.FETCHER_CRASH: FetchOutcome.CANCELLED,
}
"""P4 outcome → P1 ``FetchOutcome``; the fine-grained code travels in ``detail`` (ADR-017)."""


def p1_outcome(outcome: Outcome) -> FetchOutcome | None:
    """None: not an attempt against the target (deferred), so not recorded."""
    return _P1_OUTCOME.get(outcome)


def _ref(url: str | None) -> UrlRef | None:
    if not url:
        return None
    with contextlib.suppress(ValueError):
        return UrlRef.of(url)
    return None


class Recorder(Protocol):
    def latest_validators(self, url_id: UrlId) -> HttpValidators | None: ...

    def record(
        self,
        requested: UrlRef,
        result: FetchResult,
        *,
        started_at: datetime,
        finished_at: datetime,
    ) -> FetchAttempt | None: ...


def project_attempt(
    requested: UrlRef,
    result: FetchResult,
    *,
    worker: str,
    started_at: datetime,
    finished_at: datetime,
) -> FetchAttempt | None:
    outcome = p1_outcome(result.outcome)
    if outcome is None:
        return None
    hops = tuple(
        RedirectHop(location=ref, status=hop.status)
        for hop in result.redirects
        if (ref := _ref(hop.location)) is not None
    )
    response = outcome is FetchOutcome.RESPONSE
    final = _ref(result.final_url) or requested if response else None
    return FetchAttempt(
        fetch_attempt_id=FetchAttemptId.new(),
        requested=requested,
        capability=result.capability,
        worker=worker,
        started_at=started_at,
        finished_at=max(finished_at, started_at),
        outcome=outcome,
        http_status=result.status
        if outcome in (FetchOutcome.RESPONSE, FetchOutcome.BLOCKED)
        else None,
        final=final,
        redirects=hops,
        bytes_received=result.bytes_read,
        detail=result.detail,
    )


class StorageRecorder:
    def __init__(
        self,
        attempts: FetchAttemptRepository,
        pages: PageObservationRepository,
        objects: ObjectStore,
        *,
        worker: str,
    ) -> None:
        self._attempts = attempts
        self._pages = pages
        self._objects = objects
        self._worker = worker
        self._producer = Producer(
            service=Component.CRAWLER_WORKER.service,
            component=Component.CRAWLER_WORKER,
            instance=worker,
        )

    def latest_validators(self, url_id: UrlId) -> HttpValidators | None:
        """W5; an unavailable store means an unconditional request, never a failed attempt."""
        try:
            latest = self._pages.latest(url_id)
        except StorageError:
            return None
        return latest.validators if latest is not None else None

    def record(
        self,
        requested: UrlRef,
        result: FetchResult,
        *,
        started_at: datetime,
        finished_at: datetime,
    ) -> FetchAttempt | None:
        attempt = project_attempt(
            requested, result, worker=self._worker, started_at=started_at, finished_at=finished_at
        )
        if attempt is None:
            return None
        if (
            result.body is not None
            and result.status is not None
            and attempt.http_status is not None
            and result.outcome is not Outcome.NOT_MODIFIED
        ):
            self._observe(attempt, result)
        self._attempts.record(
            attempt,
            event=new_event(
                FetchCompleted(attempt=attempt), producer=self._producer, occurred_at=finished_at
            ),
        )
        return attempt

    def _observe(self, attempt: FetchAttempt, result: FetchResult) -> None:
        assert result.body is not None  # noqa: S101 -- guarded by record()
        assert result.status is not None  # noqa: S101
        final = attempt.final or _ref(result.final_url) or attempt.requested
        media_type = (result.content_type or "").split(";", 1)[0].strip() or None
        snapshot = self._objects.put(result.body, media_type=media_type)
        digest = ContentDigest.of_bytes(result.body)
        observation = PageObservation(
            observation_id=ObservationId.new(),
            fetch_attempt_id=attempt.fetch_attempt_id,
            requested=attempt.requested,
            final=final,
            redirects=attempt.redirects,
            capability=attempt.capability,
            observed_at=attempt.finished_at,
            http_status=result.status,
            content_type=result.content_type[:200] if result.content_type else None,
            body_digest=digest,
            body_size=len(result.body),
            page_version_id=PageVersionId.of(final.url_id, digest),
            validators=result.validators,
            snapshot=snapshot,
        )
        self._pages.record(
            observation,
            event=new_event(
                PageObserved(observation=observation),
                producer=self._producer,
                occurred_at=attempt.finished_at,
            ),
        )


def utcnow() -> datetime:
    return datetime.now(UTC)
