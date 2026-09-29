"""Pure URL functions for extraction (P5 design §4, audit V-3..V-9).

Identity is P1 canonical form v1 (``canonicalize_url``) and nothing
stronger: no tracking-parameter removal, no query reordering, no ``www``
stripping. Crawl policy (blacklists, ad/tracker rules, traps, budgets) is
not decided here; it plugs in through ``LinkPolicy`` (owned by P6).
"""

from __future__ import annotations

import re
from typing import Final, Protocol
from urllib.parse import urljoin, urlsplit

from antipiracy_contracts.ids import DomainId, UrlId
from antipiracy_contracts.models.media import MediaKind
from antipiracy_contracts.models.web import LinkRelation, UrlRef
from antipiracy_contracts.urls import InvalidUrlError, canonicalize_url

MAX_REFERENCE_LENGTH: Final = 8192
WEB_SCHEMES: Final = frozenset({"http", "https"})

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
# WHATWG URL parsing: strip leading/trailing C0 controls and space, drop tab/LF/CR.
_EDGE = "".join(chr(c) for c in range(0x21))
_INNER = str.maketrans("", "", "\t\n\r")

HLS_EXTENSIONS: Final = frozenset({".m3u8", ".m3u"})
DASH_EXTENSIONS: Final = frozenset({".mpd"})
VIDEO_EXTENSIONS: Final = frozenset(
    {".mp4", ".avi", ".mkv", ".mov", ".webm", ".m4v", ".mpeg", ".mpg", ".ogv", ".ts", ".m4s"}
)
AUDIO_EXTENSIONS: Final = frozenset({".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".opus"})
ASSET_EXTENSIONS: Final = frozenset(
    {".js", ".mjs", ".css", ".map", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}
    | {".svg", ".ico", ".bmp", ".woff", ".woff2", ".ttf", ".otf", ".eot"}
)
"""Static sub-resources: never page links when found as script or text literals."""


def clean_reference(raw: str | None) -> str | None:
    """An attribute value as a URL reference, or None when empty or oversized."""
    if not raw:
        return None
    ref = raw.strip(_EDGE).translate(_INNER)
    if not ref or len(ref) > MAX_REFERENCE_LENGTH:
        return None
    return ref


def scheme_of(ref: str) -> str | None:
    """The lowercase scheme of an absolute reference, None for a relative one."""
    match = _SCHEME_RE.match(ref)
    return match.group(1).lower() if match else None


def resolve(base: str, ref: str) -> str | None:
    """RFC 3986 resolution of a cleaned reference against an absolute http(s) base.

    Returns an absolute http(s) URL without fragment, or None for non-web
    schemes (mailto, javascript, data, tel, ...), fragment-only references
    and unparseable input.
    """
    if ref.startswith("#"):
        return None
    scheme = scheme_of(ref)
    if scheme is not None and scheme not in WEB_SCHEMES:
        return None
    try:
        absolute = urljoin(base, ref)
    except ValueError:
        return None
    absolute = absolute.partition("#")[0]
    return absolute if scheme_of(absolute) in WEB_SCHEMES else None


def to_url_ref(absolute: str) -> UrlRef | None:
    """P1 canonical ``UrlRef``, or None when the URL cannot be an identity."""
    try:
        canonical = canonicalize_url(absolute)
    except (InvalidUrlError, ValueError):
        return None
    # Built once here; ``UrlRef.of`` would canonicalize and derive the IDs a second
    # time in its validators. The IDs are derived exactly as P1 prescribes.
    return UrlRef.model_construct(
        url=canonical, url_id=UrlId.of(canonical), domain_id=DomainId.of_url(canonical)
    )


def path_extension(url: str) -> str:
    """Lowercase extension of the URL path (``""`` when none); the query is ignored."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    name = path.rpartition("/")[2]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def media_kind(url: str, declared_type: str | None = None) -> MediaKind | None:
    """V1's extension/MIME classification mapped to P1 ``MediaKind``; None = not media.

    Unlike V1, the extension is read from the path only, so ``x.mp4?token=1``
    is a video file.
    """
    if declared_type:
        media_type = declared_type.split(";", 1)[0].strip().lower()
        if "mpegurl" in media_type:
            return MediaKind.HLS_MANIFEST
        if "dash+xml" in media_type:
            return MediaKind.DASH_MANIFEST
        if media_type.startswith("video/"):
            return MediaKind.VIDEO_FILE
        if media_type.startswith("audio/"):
            return MediaKind.AUDIO_FILE
    ext = path_extension(url)
    if ext in HLS_EXTENSIONS:
        return MediaKind.HLS_MANIFEST
    if ext in DASH_EXTENSIONS:
        return MediaKind.DASH_MANIFEST
    if ext in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO_FILE
    if ext in AUDIO_EXTENSIONS:
        return MediaKind.AUDIO_FILE
    return None


class LinkPolicy(Protocol):
    """Admission of an extracted link into the page's facts (P6 hook)."""

    def allow(self, source: UrlRef, target: UrlRef, relation: LinkRelation) -> bool: ...


class AllowAll:
    """P5 default: every syntactically valid http(s) link is a fact."""

    def allow(self, source: UrlRef, target: UrlRef, relation: LinkRelation) -> bool:
        return True


ALLOW_ALL: Final[LinkPolicy] = AllowAll()
