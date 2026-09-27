"""Media discovery/observation event payloads (inside crawler2)."""

from __future__ import annotations

from typing import Annotated, ClassVar

from pydantic import Field

from antipiracy_contracts.events.envelope import EventPayload
from antipiracy_contracts.ids import ObservationId, PageVersionId
from antipiracy_contracts.models.media import MediaObservation, MediaReference
from antipiracy_contracts.models.web import UrlRef


class MediaDiscovered(EventPayload):
    """Media references extracted from one page observation, as one batch."""

    EVENT_TYPE: ClassVar[str] = "media.discovered"
    SCHEMA_MAJOR: ClassVar[int] = 1

    page_observation_id: ObservationId
    page: UrlRef
    page_version_id: PageVersionId
    references: Annotated[tuple[MediaReference, ...], Field(min_length=1, max_length=1_000)]


class MediaObserved(EventPayload):
    """The media registry resolved (and possibly probed) one media reference."""

    EVENT_TYPE: ClassVar[str] = "media.observed"
    SCHEMA_MAJOR: ClassVar[int] = 1

    observation: MediaObservation
