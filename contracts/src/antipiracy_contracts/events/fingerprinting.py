"""The crawler2 <-> fingerprinter boundary (ADR-011).

Work is keyed by *content identity* (``ContentId``), not by URL and not by
target: the same bytes found at many URLs are encoded once, and a new
target is matched against existing representations without re-encoding.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import Field

from antipiracy_contracts.base import DEFAULT_PRIORITY, Priority, ShortText
from antipiracy_contracts.events.envelope import EventPayload
from antipiracy_contracts.ids import ContentId, EventId
from antipiracy_contracts.models.matching import MatchResult
from antipiracy_contracts.models.media import ContentKey, Media
from antipiracy_contracts.models.representations import Representation, RepresentationSpec
from antipiracy_contracts.models.web import UrlRef


class EncodeRequested(EventPayload):
    """crawler2's media registry asks for a representation of one content identity."""

    EVENT_TYPE: ClassVar[str] = "encode.requested"
    SCHEMA_MAJOR: ClassVar[int] = 1

    content: ContentKey
    source: Media
    """Where to fetch the bytes. Any media carrying this content will do; the
    registry picks one and may re-request with another after a failure."""
    referer: UrlRef | None = None
    """Page the media was observed on; many hosts refuse media requests without it."""
    required_spec: RepresentationSpec | None = None
    """Absent: the fingerprinter's current default spec."""
    priority: Priority = DEFAULT_PRIORITY


class RepresentationReady(EventPayload):
    """A representation exists (newly created or already present) for a content identity."""

    EVENT_TYPE: ClassVar[str] = "representation.ready"
    SCHEMA_MAJOR: ClassVar[int] = 1

    representation: Representation


class EncodeFailure(StrEnum):
    SOURCE_UNREACHABLE = "source_unreachable"
    SOURCE_FORBIDDEN = "source_forbidden"
    UNSUPPORTED_FORMAT = "unsupported_format"
    TOO_LARGE = "too_large"
    DECODE_ERROR = "decode_error"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal_error"


class EncodeFailed(EventPayload):
    """An encode request could not be satisfied. Never a "no match": no decision was made."""

    EVENT_TYPE: ClassVar[str] = "encode.failed"
    SCHEMA_MAJOR: ClassVar[int] = 1

    request_id: EventId
    """The ``encode.requested`` event this answers."""
    content_id: ContentId
    failure: EncodeFailure
    retryable: bool
    attempt: Annotated[int, Field(ge=1)]
    spec: RepresentationSpec | None = None
    detail: ShortText | None = None


class MatchFound(EventPayload):
    """The matcher verified content against a target version."""

    EVENT_TYPE: ClassVar[str] = "match.found"
    SCHEMA_MAJOR: ClassVar[int] = 1

    match: MatchResult
