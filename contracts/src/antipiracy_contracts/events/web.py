"""Crawl and page event payloads (all produced and consumed inside crawler2)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Self

from pydantic import Field, model_validator

from antipiracy_contracts.base import DEFAULT_PRIORITY, Priority, UtcTimestamp
from antipiracy_contracts.events.envelope import EventPayload
from antipiracy_contracts.ids import ObservationId, PageRevisionId, PageVersionId, TargetId
from antipiracy_contracts.models.web import (
    DiscoveredLink,
    FetchAttempt,
    FetchCapability,
    NormalizationScheme,
    PageHashes,
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


class PageChanged(EventPayload):
    """A page observation showed a normalized content state never seen before at its URL.

    Added in contract 1.1 (P5, ADR-020). Re-sightings of a known revision,
    raw-only changes (ad rotation, volatile markup) and non-HTML bodies emit
    nothing. A concurrent first sighting may be emitted twice with the same
    ``revision_id``; consumers deduplicate on it.
    """

    EVENT_TYPE: ClassVar[str] = "page.changed"
    SCHEMA_MAJOR: ClassVar[int] = 1

    page_observation_id: ObservationId
    page: UrlRef
    """The final URL of the observation (revisions are keyed by it, like page versions)."""
    page_version_id: PageVersionId
    revision_id: PageRevisionId
    normalization: NormalizationScheme
    hashes: PageHashes
    observed_at: UtcTimestamp

    @model_validator(mode="after")
    def _ids_match_hashes(self) -> Self:
        if self.page_version_id != PageVersionId.of(self.page.url_id, self.hashes.raw):
            raise ValueError("page_version_id is not derived from (page.url_id, hashes.raw)")
        expected = PageRevisionId.of(self.page.url_id, self.normalization, self.hashes.normalized)
        if self.revision_id != expected:
            raise ValueError("revision_id is not derived from (page, normalization, normalized)")
        return self
