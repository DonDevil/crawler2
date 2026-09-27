"""Media-side contracts and the identity hierarchy (plan P8).

| Level | Contract | Asserts |
|---|---|---|
| 1 | ``MediaId`` (locator) | same address |
| 2 | ``TechnicalHints`` | probably the same file — hint only |
| 3 | ``ContentKey``/``ContentId`` | same sampled bytes — candidate only |
| 4 | representation (fingerprinter) | perceptual similarity |
| 5 | verified equivalence (fingerprinter) | same underlying work |

A weaker level never asserts what only a stronger level can.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Self

from pydantic import Field, model_validator

from antipiracy_contracts.base import ContractKind, ContractModel, UtcTimestamp
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.ids import ContentId, MediaId, ObservationId, PageVersionId
from antipiracy_contracts.models.web import UrlRef

ContentKeyScheme = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}/v[1-9][0-9]*$")]
"""Versioned name of how the content digest was computed, e.g. ``sampled-bytes/v1``."""


class MediaKind(StrEnum):
    VIDEO_FILE = "video_file"
    HLS_MANIFEST = "hls_manifest"
    DASH_MANIFEST = "dash_manifest"
    AUDIO_FILE = "audio_file"
    UNKNOWN = "unknown"


class DiscoveryMethod(StrEnum):
    """Where in the page the media reference was found."""

    VIDEO_ELEMENT = "video_element"
    SOURCE_ELEMENT = "source_element"
    META_TAG = "meta_tag"
    PLAYER_CONFIG = "player_config"
    SCRIPT_LITERAL = "script_literal"
    NETWORK_CAPTURE = "network_capture"
    LINK = "link"


class MediaReference(ContractModel):
    """A media locator as seen by extraction, before the registry has examined it."""

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    locator: UrlRef
    kind: MediaKind
    method: DiscoveryMethod
    declared_type: Annotated[str, Field(max_length=200)] | None = None


class ContentKey(ContractModel):
    """Level-3 candidate content identity: a digest of sampled bytes under a named scheme.

    Equal keys mean "the sampled bytes were identical" and justify skipping
    duplicate work. They never justify merging media records irreversibly,
    and a representation-level disagreement (level 4) overrides them.
    """

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    scheme: ContentKeyScheme
    digest: ContentDigest
    content_id: ContentId

    @classmethod
    def of(cls, scheme: str, digest: ContentDigest) -> Self:
        return cls(scheme=scheme, digest=digest, content_id=ContentId.of(scheme, digest))

    @model_validator(mode="after")
    def _id_matches_key(self) -> Self:
        if self.content_id != ContentId.of(self.scheme, self.digest):
            raise ValueError("content_id is not derived from (scheme, digest)")
        return self


class TechnicalHints(ContractModel):
    """Level-2 signals from a cheap probe. Hints only: equal hints prove nothing."""

    KIND: ClassVar[ContractKind] = ContractKind.VALUE

    content_length: Annotated[int, Field(ge=0)] | None = None
    etag: Annotated[str, Field(max_length=500)] | None = None
    media_type: Annotated[str, Field(max_length=200)] | None = None
    duration_seconds: Annotated[float, Field(ge=0)] | None = None


class Media(ContractModel):
    """A media resource known to the registry, identified by its locator (level 1)."""

    KIND: ClassVar[ContractKind] = ContractKind.ENTITY

    media_id: MediaId
    locator: UrlRef
    kind: MediaKind

    @classmethod
    def of(cls, locator: UrlRef, kind: MediaKind) -> Self:
        return cls(media_id=MediaId.of_locator(locator.url), locator=locator, kind=kind)

    @model_validator(mode="after")
    def _id_matches_locator(self) -> Self:
        if self.media_id != MediaId.of_locator(self.locator.url):
            raise ValueError("media_id is not derived from the locator URL")
        return self


class ProbeStatus(StrEnum):
    NOT_PROBED = "not_probed"
    PROBED = "probed"
    UNREACHABLE = "unreachable"
    FORBIDDEN = "forbidden"
    NOT_MEDIA = "not_media"


class MediaObservation(ContractModel):
    """Media seen on one page observation, as resolved by the media registry.

    Identified by the pair (``page_observation_id``, ``media.media_id``).
    """

    KIND: ClassVar[ContractKind] = ContractKind.OBSERVATION

    media: Media
    page_observation_id: ObservationId
    page: UrlRef
    page_version_id: PageVersionId
    observed_at: UtcTimestamp
    probe_status: ProbeStatus
    content: ContentKey | None = None
    hints: TechnicalHints | None = None

    @model_validator(mode="after")
    def _content_requires_probe(self) -> Self:
        if self.content is not None and self.probe_status is not ProbeStatus.PROBED:
            raise ValueError("a content key can only come from a successful probe")
        return self
