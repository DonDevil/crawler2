"""Typed identifiers (ADR-008).

Wire form: ``<prefix>_<32 lowercase hex>``, where the hex is an RFC 9562
UUID. The prefix makes IDs self-describing in logs and lets validation
reject an ID of the wrong type (a ``MediaId`` passed where a ``UrlId`` is
expected). Storage may keep just the 16-byte UUID; the prefix is implied
by the column.

Two families:

- **Derived** (UUIDv8): SHA-256 over a versioned, length-prefixed
  derivation input, truncated to 122 bits. Every host computes the same ID
  for the same input, so independent writers converge without coordination.
- **Allocated** (UUIDv7): unique per allocation. The embedded millisecond
  timestamp only gives storage locality and rough ordering; it is *not*
  data — read time from the model's timestamp fields, never from an ID.

An ID never changes after it is issued, and IDs are never reused.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from typing import Any, ClassVar, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.urls import CanonicalUrl

_UUID_VERSION_SHIFT = 76
_VARIANT_SHIFT = 62


class InvalidIdError(ValueError):
    """A string is not a valid identifier of the expected type."""


class TypedId(str):
    """Common validation and pydantic integration for all ID types."""

    PREFIX: ClassVar[str]
    UUID_VERSION: ClassVar[int]
    PATTERN: ClassVar[str]
    _regex: ClassVar[re.Pattern[str]]

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "PREFIX" in cls.__dict__:
            cls.PATTERN = (
                rf"^{cls.PREFIX}_[0-9a-f]{{12}}{cls.UUID_VERSION}[0-9a-f]{{3}}"
                r"[89ab][0-9a-f]{15}$"
            )
            cls._regex = re.compile(cls.PATTERN)

    def __new__(cls, value: str) -> Self:
        if not cls._regex.fullmatch(value):
            raise InvalidIdError(
                f"not a {cls.__name__} (expected '{cls.PREFIX}_' + UUIDv{cls.UUID_VERSION} hex): "
                f"{value!r}"
            )
        return super().__new__(cls, value)

    @classmethod
    def from_uuid(cls, value: uuid.UUID) -> Self:
        return cls(f"{cls.PREFIX}_{value.hex}")

    @property
    def uuid(self) -> uuid.UUID:
        return uuid.UUID(hex=self.partition("_")[2])

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(pattern=cls.PATTERN),
            serialization=core_schema.to_string_ser_schema(),
        )


def _set_version(value: int, version: int) -> uuid.UUID:
    value &= ~(0xF << _UUID_VERSION_SHIFT)
    value |= version << _UUID_VERSION_SHIFT
    value &= ~(0x3 << _VARIANT_SHIFT)
    value |= 0x2 << _VARIANT_SHIFT
    return uuid.UUID(int=value)


class AllocatedId(TypedId):
    """Unique per allocation (UUIDv7)."""

    UUID_VERSION = 7

    @classmethod
    def new(cls) -> Self:
        millis = time.time_ns() // 1_000_000
        value = (millis & ((1 << 48) - 1)) << 80 | int.from_bytes(os.urandom(10), "big")
        return cls.from_uuid(_set_version(value, 7))


class DerivedId(TypedId):
    """Deterministic function of its derivation input (UUIDv8 over SHA-256)."""

    UUID_VERSION = 8
    DERIVATION_TAG: ClassVar[str]

    @classmethod
    def _derive(cls, *parts: str) -> Self:
        digest = hashlib.sha256()
        # Length-prefixing makes the encoding injective: ("ab","c") != ("a","bc").
        for part in (cls.DERIVATION_TAG, *parts):
            encoded = part.encode("utf-8")
            digest.update(f"{len(encoded)}:".encode("ascii"))
            digest.update(encoded)
        value = int.from_bytes(digest.digest()[:16], "big")
        return cls.from_uuid(_set_version(value, 8))


# --- Derived identities ------------------------------------------------------


class DomainId(DerivedId):
    """A host name as it appears in canonical URLs (not the registrable domain).

    Grouping hosts into sites/registrable domains depends on the public
    suffix list, which changes over time, so it is an attribute derived by
    intelligence, never part of identity.
    """

    PREFIX = "dom"
    DERIVATION_TAG = "antipiracy/domain/v1"

    @classmethod
    def of_url(cls, url: CanonicalUrl) -> Self:
        return cls._derive(url.host)


class UrlId(DerivedId):
    """A web address: the canonical v1 URL. Page knowledge is keyed by this ID.

    There is no separate page ID: a page *is* the resource at a canonical
    address, and a second name for the same input would only invite drift.
    """

    PREFIX = "url"
    DERIVATION_TAG = "antipiracy/url/v1"

    @classmethod
    def of(cls, url: CanonicalUrl) -> Self:
        return cls._derive(url)


class PageVersionId(DerivedId):
    """One distinct content state of a page: (final URL, exact body digest).

    Two hosts fetching an unchanged page derive the same version ID.
    """

    PREFIX = "pgv"
    DERIVATION_TAG = "antipiracy/page-version/v1"

    @classmethod
    def of(cls, url_id: UrlId, body_digest: ContentDigest) -> Self:
        return cls._derive(url_id, body_digest)


class PageRevisionId(DerivedId):
    """One meaningful content state of a page: (final URL, normalization, normalized digest).

    Added in contract 1.1 (P5, ADR-020). ``PageVersionId`` keeps its meaning
    (exact bytes); a revision groups every exact-bytes version whose
    normalized content is equal, so ad rotation or volatile markup does not
    create a new revision. Two hosts seeing the same normalized content derive
    the same revision ID.
    """

    PREFIX = "pgr"
    DERIVATION_TAG = "antipiracy/page-revision/v1"

    @classmethod
    def of(cls, url_id: UrlId, normalization: str, normalized_digest: ContentDigest) -> Self:
        return cls._derive(url_id, normalization, normalized_digest)


class MediaId(DerivedId):
    """A media resource as addressed by its locator (identity level 1: "same address").

    The locator is a canonical URL chosen by the media registry; it asserts
    nothing about the bytes behind it.
    """

    PREFIX = "med"
    DERIVATION_TAG = "antipiracy/media/v1"

    @classmethod
    def of_locator(cls, locator: CanonicalUrl) -> Self:
        return cls._derive(locator)


class ContentId(DerivedId):
    """Candidate content identity (level 3): same bytes under one sampling scheme.

    Used only to avoid redundant work (e.g. encoding byte-identical files
    once). It never proves two media are the same underlying work, and two
    encodings of the same work have different content IDs.
    """

    PREFIX = "cnt"
    DERIVATION_TAG = "antipiracy/content/v1"

    @classmethod
    def of(cls, scheme: str, digest: ContentDigest) -> Self:
        return cls._derive(scheme, digest)


class RepresentationId(DerivedId):
    """A representation of one subject under one representation spec.

    Deterministic so that re-encoding the same subject with the same spec
    is idempotent, whichever host does it.
    """

    PREFIX = "rep"
    DERIVATION_TAG = "antipiracy/representation/v1"

    @classmethod
    def for_content(
        cls, content_id: ContentId, spec_name: str, spec_version: str, config: ContentDigest
    ) -> Self:
        return cls._derive("content", content_id, spec_name, spec_version, config)

    @classmethod
    def for_target(
        cls,
        target_id: TargetId,
        target_version: int,
        spec_name: str,
        spec_version: str,
        config: ContentDigest,
    ) -> Self:
        return cls._derive(
            "target", target_id, str(target_version), spec_name, spec_version, config
        )


class MatchId(DerivedId):
    """One comparison outcome: media representation x target representation x technique."""

    PREFIX = "mat"
    DERIVATION_TAG = "antipiracy/match/v1"

    @classmethod
    def of(
        cls,
        representation_id: RepresentationId,
        target_representation_id: RepresentationId,
        technique_name: str,
        technique_version: str,
    ) -> Self:
        return cls._derive(
            representation_id, target_representation_id, technique_name, technique_version
        )


# --- Allocated identities ----------------------------------------------------


class TargetId(AllocatedId):
    """A protected work being searched for. Stable across target versions."""

    PREFIX = "tgt"


class FetchAttemptId(AllocatedId):
    """One execution of a fetch, successful or not."""

    PREFIX = "fat"


class ObservationId(AllocatedId):
    """One page observation (what a successful fetch saw, at one instant)."""

    PREFIX = "obs"


class EvidenceId(AllocatedId):
    """One evidence item, from candidate to finalized record."""

    PREFIX = "evd"


class EventId(AllocatedId):
    """One event. Redelivery of the same event keeps its ID."""

    PREFIX = "evt"


class CorrelationId(AllocatedId):
    """Groups every event caused, directly or transitively, by one originating request."""

    PREFIX = "cor"
