"""Web-side contracts: addresses, domains, fetch attempts, page observations, links.

Nothing here refers to a target: what the crawler knows about the web is
target-independent (the V1 defect this phase designs out). Target
relevance lives in intelligence and in match results, never in page state.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Self

from pydantic import Field, model_validator

from antipiracy_contracts.base import ContractKind, ContractModel, ShortText, UtcTimestamp
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.ids import (
    DomainId,
    FetchAttemptId,
    ObservationId,
    PageVersionId,
    UrlId,
)
from antipiracy_contracts.models.blobs import BlobRef
from antipiracy_contracts.ownership import InstanceName
from antipiracy_contracts.urls import CanonicalUrl, canonicalize_url

HttpStatus = Annotated[int, Field(ge=100, le=599)]


class UrlRef(ContractModel):
    """A canonical URL together with its derived IDs.

    Carrying the IDs saves every consumer from re-deriving them; validation
    guarantees they match the URL, so a producer cannot send a mismatched pair.
    """

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    url: CanonicalUrl
    url_id: UrlId
    domain_id: DomainId

    @classmethod
    def of(cls, url: str) -> Self:
        canonical = canonicalize_url(url)
        return cls(url=canonical, url_id=UrlId.of(canonical), domain_id=DomainId.of_url(canonical))

    @model_validator(mode="after")
    def _ids_match_url(self) -> Self:
        if self.url_id != UrlId.of(self.url):
            raise ValueError(f"url_id {self.url_id} is not derived from {self.url!r}")
        if self.domain_id != DomainId.of_url(self.url):
            raise ValueError(f"domain_id {self.domain_id} is not derived from {self.url!r}")
        return self


class Domain(ContractModel):
    """A host known to the system. Source intelligence (P7) is keyed by ``domain_id``."""

    KIND: ClassVar[ContractKind] = ContractKind.ENTITY

    domain_id: DomainId
    host: Annotated[str, Field(min_length=1, max_length=255)]

    @classmethod
    def of_url(cls, url: CanonicalUrl) -> Self:
        return cls(domain_id=DomainId.of_url(url), host=url.host)

    @model_validator(mode="after")
    def _id_matches_host(self) -> Self:
        probe = canonicalize_url(f"http://{self.host}/")
        if probe.host != self.host:
            raise ValueError(f"host is not canonical: {self.host!r} (expected {probe.host!r})")
        if self.domain_id != DomainId.of_url(probe):
            raise ValueError(f"domain_id {self.domain_id} is not derived from {self.host!r}")
        return self


class FetchCapability(StrEnum):
    """What a fetch could do, independent of which library implemented it."""

    HTTP = "http"
    """Plain HTTP client; no script execution."""
    BROWSER = "browser"
    """Real browser engine with JavaScript."""
    TOR_HTTP = "tor_http"
    """Plain HTTP over Tor."""
    TOR_BROWSER = "tor_browser"
    """Browser over Tor."""


class FetchOutcome(StrEnum):
    """How a fetch attempt ended. Only ``RESPONSE`` produced an HTTP response."""

    RESPONSE = "response"
    TIMEOUT = "timeout"
    DNS_FAILURE = "dns_failure"
    CONNECTION_FAILURE = "connection_failure"
    TLS_FAILURE = "tls_failure"
    BLOCKED = "blocked"
    """A response arrived but was classified as a bot wall/challenge, not content."""
    TOO_LARGE = "too_large"
    CANCELLED = "cancelled"


class RedirectHop(ContractModel):
    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    location: UrlRef
    status: HttpStatus


class HttpValidators(ContractModel):
    """Conditional-request validators as sent by the server (opaque strings)."""

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    etag: Annotated[str, Field(max_length=500)] | None = None
    last_modified: Annotated[str, Field(max_length=100)] | None = None


class FetchAttempt(ContractModel):
    """One execution of fetching a URL, including failures.

    Failed attempts are first-class: fetch-profile learning (P7) needs to
    know which capability did *not* work.
    """

    KIND: ClassVar[ContractKind] = ContractKind.ATTEMPT

    fetch_attempt_id: FetchAttemptId
    requested: UrlRef
    capability: FetchCapability
    worker: InstanceName
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    outcome: FetchOutcome
    http_status: HttpStatus | None = None
    final: UrlRef | None = None
    redirects: tuple[RedirectHop, ...] = ()
    bytes_received: Annotated[int, Field(ge=0)] | None = None
    detail: ShortText | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        has_response = self.outcome in (FetchOutcome.RESPONSE, FetchOutcome.BLOCKED)
        if has_response != (self.http_status is not None):
            raise ValueError("http_status is required exactly when a response was received")
        if (self.outcome is FetchOutcome.RESPONSE) != (self.final is not None):
            raise ValueError("final URL is required exactly when outcome is 'response'")
        return self


class PageObservation(ContractModel):
    """What one successful fetch saw at a URL, at one instant. Append-only.

    Any HTTP status is an observation (a 404 is knowledge too). The page
    version is derived from the final URL and the exact body bytes, so an
    unchanged page observed again yields the same ``page_version_id``.
    """

    KIND: ClassVar[ContractKind] = ContractKind.OBSERVATION

    observation_id: ObservationId
    fetch_attempt_id: FetchAttemptId
    requested: UrlRef
    final: UrlRef
    redirects: tuple[RedirectHop, ...] = ()
    capability: FetchCapability
    observed_at: UtcTimestamp
    http_status: HttpStatus
    content_type: Annotated[str, Field(max_length=200)] | None = None
    body_digest: ContentDigest
    body_size: Annotated[int, Field(ge=0)]
    page_version_id: PageVersionId
    validators: HttpValidators | None = None
    snapshot: BlobRef | None = None
    """Raw response snapshot, once the storage layer (P2) archives it."""

    @model_validator(mode="after")
    def _version_matches_content(self) -> Self:
        if self.page_version_id != PageVersionId.of(self.final.url_id, self.body_digest):
            raise ValueError("page_version_id is not derived from (final.url_id, body_digest)")
        if self.snapshot is not None and self.snapshot.digest != self.body_digest:
            raise ValueError("snapshot digest differs from body_digest")
        return self


class LinkRelation(StrEnum):
    ANCHOR = "anchor"
    IFRAME = "iframe"
    META_REFRESH = "meta_refresh"
    CANONICAL = "canonical"
    SCRIPT_LITERAL = "script_literal"
    OTHER = "other"


class DiscoveredLink(ContractModel):
    """An outgoing link seen in one page version. Links have no ID of their own:

    a link is the edge (page version -> URL), identified by that pair.
    """

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    target: UrlRef
    relation: LinkRelation
    anchor_text: Annotated[str, Field(max_length=300)] | None = None
    nofollow: bool = False


NormalizationScheme = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}/v[1-9][0-9]*$")]
"""Versioned name of a page normalization, e.g. ``html-normalized/v1`` (contract 1.1)."""


class PageHashes(ContractModel):
    """The P5 hash set of one page version (contract 1.1, ADR-020).

    ``raw`` is the exact body digest; ``normalized`` decides revisions
    (meaningful change); the others are diagnostic signals. Each digest's
    canonical form is defined by the producing normalization scheme.
    """

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    raw: ContentDigest
    normalized: ContentDigest
    visible_text: ContentDigest
    link_set: ContentDigest
    media_set: ContentDigest
    structural: ContentDigest
