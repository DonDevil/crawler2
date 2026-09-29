"""Canonical forms and digests of the P5 hash set (P5 design §9).

Every digest is ``sha256(tag "\\n" line₁ "\\n" line₂ "\\n" …)`` over UTF-8,
with a versioned tag per hash; any change to a canonical form must bump
its tag. Python's ``hash()`` is never used (it is randomized per process).
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable
from typing import Final

from antipiracy_contracts.digests import ContentDigest

VISIBLE_TEXT_TAG: Final = "antipiracy/page-hash/visible-text/v1"
LINK_SET_TAG: Final = "antipiracy/page-hash/link-set/v1"
MEDIA_SET_TAG: Final = "antipiracy/page-hash/media-set/v1"
STRUCTURAL_TAG: Final = "antipiracy/page-hash/structural/v1"
NORMALIZED_TAG_PREFIX: Final = "antipiracy/page-hash/"

# Invisible format characters that vary without changing what a reader sees:
# soft hyphen, zero-width space/joiners, bidi marks, word joiner, BOM.
_INVISIBLE: Final = dict.fromkeys(
    (0x00AD, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF), None
)


def digest_lines(tag: str, lines: Iterable[str]) -> ContentDigest:
    h = hashlib.sha256()
    h.update(tag.encode("utf-8"))
    h.update(b"\n")
    for line in lines:
        h.update(line.encode("utf-8", "replace"))
        h.update(b"\n")
    return ContentDigest("sha256:" + h.hexdigest())


def collapse(text: str) -> str:
    """Runs of Unicode whitespace become one space; leading/trailing removed."""
    return " ".join(text.split())


def visible_text_form(chunks: Iterable[str]) -> str:
    """NFC, whitespace collapsed, case kept (a case change is visible)."""
    return unicodedata.normalize("NFC", collapse(" ".join(chunks)))


def normalized_text_form(chunks: Iterable[str]) -> str:
    """NFKC, invisible format characters removed, casefolded, whitespace collapsed."""
    text = unicodedata.normalize("NFKC", " ".join(chunks)).translate(_INVISIBLE)
    return collapse(text.casefold())


def visible_text_digest(text: str) -> ContentDigest:
    return digest_lines(VISIBLE_TEXT_TAG, (text,))


def link_set_digest(lines: Iterable[tuple[str, str]]) -> ContentDigest:
    """``(relation, canonical_url)`` pairs; sorted and deduplicated here."""
    return digest_lines(LINK_SET_TAG, (f"{r}\t{u}" for r, u in sorted(set(lines))))


def media_set_lines(lines: Iterable[tuple[str, str]]) -> list[str]:
    """``(kind, canonical_locator)`` pairs, sorted and deduplicated."""
    return [f"{k}\t{u}" for k, u in sorted(set(lines))]


def media_set_digest(lines: list[str]) -> ContentDigest:
    return digest_lines(MEDIA_SET_TAG, lines)


def structural_digest(shape: Iterable[str]) -> ContentDigest:
    return digest_lines(STRUCTURAL_TAG, shape)


def normalized_digest(scheme: str, text: str, media_lines: list[str]) -> ContentDigest:
    """The revision-deciding digest: normalized text plus the media set."""
    return digest_lines(
        NORMALIZED_TAG_PREFIX + scheme, ("text\t" + text, *("media\t" + m for m in media_lines))
    )
