"""Crawl and page event payloads (all produced and consumed inside crawler2)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import Field

from antipiracy_contracts.base import DEFAULT_PRIORITY, Priority, UtcTimestamp
from antipiracy_contracts.events.envelope import EventPayload
from antipiracy_contracts.ids import ObservationId, PageVersionId, TargetId
from antipiracy_contracts.models.web import (
    DiscoveredLink,
    FetchAttempt,
    FetchCapability,
    PageObservation,
    UrlRef,
)


class CrawlReason(StrEnum):
    SEED = "seed"
    DISCOVERED = "discovered"
    RECRAWL = "recrawl"
    TARGET_DISCOVERY = "target_discovery"
    FEEDBACK = "feedback"


class CrawlRequested(EventPayload):
    """Intelligence asks for a URL to be (re)crawled. The frontier decides admission."""

    EVENT_TYPE: ClassVar[str] = "crawl.requested"
    SCHEMA_MAJOR: ClassVar[int] = 1

    url: UrlRef
    reason: CrawlReason
    priority: Priority = DEFAULT_PRIORITY
    capability: FetchCapability | None = None
    """Suggested starting capability; workers fall back to their default when absent."""
    not_before: UtcTimestamp | None = None
    relevant_targets: tuple[TargetId, ...] = ()
    """Why this crawl is interesting now. A scheduling/attribution hint only: it is
    never stored as page state, so page knowledge stays target-independent."""


class FetchCompleted(EventPayload):
    """A fetch attempt ended, successfully or not."""

    EVENT_TYPE: ClassVar[str] = "fetch.completed"
    SCHEMA_MAJOR: ClassVar[int] = 1

    attempt: FetchAttempt


class PageObserved(EventPayload):
    """A page observation was durably recorded."""

    EVENT_TYPE: ClassVar[str] = "page.observed"
    SCHEMA_MAJOR: ClassVar[int] = 1

    observation: PageObservation


class UrlsDiscovered(EventPayload):
    """Outgoing links extracted from one page observation, as one batch."""

    EVENT_TYPE: ClassVar[str] = "urls.discovered"
    SCHEMA_MAJOR: ClassVar[int] = 1

    page_observation_id: ObservationId
    page: UrlRef
    page_version_id: PageVersionId
    links: Annotated[tuple[DiscoveredLink, ...], Field(min_length=1, max_length=10_000)]
