"""P5 extraction results and knobs (crawler2-internal; not P1 contracts)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from antipiracy_contracts.ids import PageRevisionId, PageVersionId
from antipiracy_contracts.models.media import MediaReference
from antipiracy_contracts.models.web import DiscoveredLink, PageHashes, UrlRef
from pydantic import BaseModel, ConfigDict

EXTRACTOR_VERSION: Final = "p5-extract/v1"
"""Bumped whenever an extractor changes its output for the same bytes."""

BASELINE_SCHEME: Final = "html-normalized/v1"


@dataclass(frozen=True, slots=True)
class NormalizationRules:
    """What the normalized hash ignores beyond invisible content (P5 design §9).

    The baseline removes landmark boilerplate only; it is not an ad blocker.
    P6 may inject more selectors, and then must name a new ``scheme``: the
    scheme is part of the revision identity (ADR-020).
    """

    scheme: str = BASELINE_SCHEME
    boilerplate_selectors: tuple[str, ...] = (
        "nav",
        "footer",
        "aside",
        '[role="navigation"]',
        '[role="contentinfo"]',
        '[role="complementary"]',
        '[role="banner"]',
    )
    extra_selectors: tuple[str, ...] = ()


BASELINE_RULES: Final = NormalizationRules()


@dataclass(frozen=True, slots=True)
class Limits:
    """Bounds on untrusted input (P5 design §16)."""

    max_parse_bytes: int = 8 * 1024 * 1024
    max_elements: int = 200_000
    max_depth: int = 256
    max_script_bytes: int = 1024 * 1024
    max_page_script_bytes: int = 4 * 1024 * 1024
    max_links: int = 10_000
    max_media: int = 1_000


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class PageMetadata(_Frozen):
    """Only fields with a documented consumer (P5 design §8)."""

    title: str | None = None
    lang: str | None = None
    canonical: str | None = None
    og_title: str | None = None
    og_type: str | None = None
    published_at: datetime | None = None
    modified_at: datetime | None = None
    robots: str | None = None


class ExtractStats(_Frozen):
    elements: int = 0
    text_chars: int = 0
    dropped: dict[str, int] = {}
    """Discarded references by reason (scheme, invalid, self, policy, asset, cap)."""
    truncated: tuple[str, ...] = ()
    """Limits that were hit: body, elements, scripts, links, media."""


class PageExtract(_Frozen):
    """Everything P5 derives from one exact body at one final URL (deterministic)."""

    extractor: str = EXTRACTOR_VERSION
    page: UrlRef
    page_version_id: PageVersionId
    normalization: str
    revision_id: PageRevisionId
    hashes: PageHashes
    links: tuple[DiscoveredLink, ...]
    media: tuple[MediaReference, ...]
    metadata: PageMetadata
    stats: ExtractStats
