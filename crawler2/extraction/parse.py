"""The single parse of a page body (P5 design §2).

``parse_page`` is the only place in crawler2 that constructs an HTML
parser. Every extractor works on the ``ParsedPage`` it returns; none
re-parses and none mutates the tree.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from antipiracy_contracts.models.web import UrlRef
from selectolax.lexbor import LexborHTMLParser

from crawler2.extraction.urls import clean_reference, resolve

HTML_MEDIA_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
_SNIFF_BYTES = 4096
_META_CHARSET = re.compile(
    rb"""<meta[^>]{0,200}?charset\s*=\s*["']?\s*([A-Za-z0-9_.:\-]{1,40})""", re.I
)
_HTML_START = re.compile(
    rb"^\s*(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html|head|body)\b", re.I | re.S
)

ParserFactory = Callable[[str], LexborHTMLParser]


@dataclass(frozen=True, slots=True)
class ParsedPage:
    """One parsed body. Lives for one extraction and is never shared across pages."""

    tree: LexborHTMLParser
    page: UrlRef
    """The observation's final URL."""
    base: str
    """Effective base for relative references: a valid ``<base href>`` or the page URL."""
    encoding: str
    truncated: bool
    """The body exceeded the parse limit and only its prefix was parsed."""


def media_type_of(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


def is_html(content_type: str | None, body: bytes) -> bool:
    """HTML by declared type; without a declared type, by a leading HTML tag."""
    media_type = media_type_of(content_type)
    if media_type is not None:
        return media_type in HTML_MEDIA_TYPES
    return _HTML_START.match(body[:_SNIFF_BYTES]) is not None


def _codec(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return codecs.lookup(name.strip().strip("\"'")).name
    except LookupError:
        return None


def encoding_of(body: bytes, content_type: str | None) -> str:
    """BOM, then the header ``charset``, then ``<meta charset>``, else UTF-8."""
    if body.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if content_type and "charset=" in content_type.lower():
        declared = content_type.lower().split("charset=", 1)[1].split(";", 1)[0]
        if (name := _codec(declared)) is not None:
            return name
    match = _META_CHARSET.search(body[:_SNIFF_BYTES])
    if match and (name := _codec(match.group(1).decode("ascii"))) is not None:
        return name
    return "utf-8"


def parse_page(
    body: bytes,
    *,
    page: UrlRef,
    content_type: str | None,
    max_bytes: int,
    parser: ParserFactory = LexborHTMLParser,
) -> ParsedPage:
    truncated = len(body) > max_bytes
    data = body[:max_bytes] if truncated else body
    encoding = encoding_of(data, content_type)
    text = data.decode(encoding, errors="replace")
    tree = parser(text)
    base: str = page.url
    base_node = tree.css_first("base[href]")
    if base_node is not None:
        ref = clean_reference(base_node.attributes.get("href"))
        resolved = resolve(page.url, ref) if ref else None
        if resolved is not None:
            base = resolved
    return ParsedPage(tree=tree, page=page, base=base, encoding=encoding, truncated=truncated)
