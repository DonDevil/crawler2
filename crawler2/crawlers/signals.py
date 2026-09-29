"""Pure response inspection: page markers, media and manifest detection.

Marker sets come from V1 (``core/crawler_router.py``), split by meaning:
a captcha wall and a bot challenge are *access* facts (``captcha``,
``blocked``), a script-built page is a *rendering* fact (``needs_js``).
V1 treated every marker as "try a browser"; here they only describe the
response (P4 design §9, audit §4).

Framework markers (``__next_data__``, ``id="root"``, …) and "enable
JavaScript" notices also occur in server-rendered pages that carry full
content; they count only on anchor-poor pages (``< _MIN_ANCHORS``), the
same shape as V1's own ``<script> ≥ 6 and <a> ≤ 1`` heuristic.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from crawler2.crawlers.model import BodyKind, Outcome

_MIN_ANCHORS = 3

_JS_TEXT = (
    "enable javascript",
    "javascript required",
    "please turn javascript on",
    "javascript is disabled",
    "requires javascript",
)
_JS_FRAMEWORK = ("__next_data__", "data-reactroot", 'id="root"', 'id="app"', "ng-app", "id=root")
_CAPTCHA = (
    "g-recaptcha",
    "h-captcha",
    "cf-turnstile",
    "are you not a robot",
    "verify you are human",
    "i'm not a robot",
    "/showcaptcha",
)
_CHALLENGE = (
    "cf-browser-verification",
    "cf-chl-",
    "just a moment...",
    "attention required! | cloudflare",
    "ddos-guard",
    "checking your browser",
)

_MANIFEST_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/dash+xml",
)
_MANIFEST_EXT = (".m3u8", ".mpd")
_MEDIA_EXT = (
    ".mp4",
    ".m4v",
    ".mkv",
    ".webm",
    ".avi",
    ".mov",
    ".flv",
    ".ts",
    ".m4s",
    ".mp3",
    ".aac",
    ".m4a",
    ".wmv",
    ".3gp",
    ".ogg",
    ".ogv",
)
_GENERIC_BINARY = ("application/octet-stream", "binary/octet-stream", "application/mp4")


def _media_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _path(url: str) -> str:
    return urlsplit(url).path.lower()


def is_manifest(url: str, content_type: str | None) -> bool:
    return _media_type(content_type) in _MANIFEST_TYPES or _path(url).endswith(_MANIFEST_EXT)


def has_media_extension(url: str) -> bool:
    return _path(url).endswith(_MEDIA_EXT)


def is_media(url: str, content_type: str | None) -> bool:
    """Decided from headers and URL only, before any body byte is read (D2)."""
    if is_manifest(url, content_type):
        return False
    kind = _media_type(content_type)
    if kind.startswith(("video/", "audio/")):
        return True
    return kind in _GENERIC_BINARY and has_media_extension(url)


def body_kind(url: str, content_type: str | None) -> BodyKind:
    if is_manifest(url, content_type):
        return BodyKind.MANIFEST
    kind = _media_type(content_type)
    if not kind or "html" in kind or kind in ("text/plain", "application/xml", "text/xml"):
        return BodyKind.HTML
    return BodyKind.OTHER


def sniff_container(prefix: bytes) -> str | None:
    if len(prefix) >= 8 and prefix[4:8] == b"ftyp":
        return "mp4"
    if prefix.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if prefix.startswith(b"FLV"):
        return "flv"
    if prefix.startswith(b"ID3") or (len(prefix) > 1 and prefix[0] == 0xFF and prefix[1] >= 0xE0):
        return "mp3"
    if prefix[:1] == b"\x47" and (len(prefix) < 189 or prefix[188:189] == b"\x47"):
        return "mpegts"
    return None


def inspect_page(status: int, final_url: str, prefix: bytes) -> tuple[Outcome, tuple[str, ...]]:
    """Classify a page response from its status and the first ``sniff_bytes`` of its body."""
    text = prefix.decode("latin-1").lower()
    anchors = text.count("<a ") + text.count("<a\n") + text.count("<a\t")
    sparse = anchors < _MIN_ANCHORS
    captcha = tuple(m for m in _CAPTCHA if m in text)
    if "/showcaptcha" in final_url.lower():
        captcha = (*captcha, "url:showcaptcha")
    challenge = tuple(m for m in _CHALLENGE if m in text)

    if status == 429:
        return Outcome.BLOCKED, ("status:429", *challenge)
    if captcha and (sparse or status in (403, 429, 503)):
        return Outcome.CAPTCHA, captcha
    if challenge and (sparse or status in (403, 503)):
        return Outcome.BLOCKED, challenge
    if status >= 400:
        return Outcome.HTTP_ERROR, ()
    if not 200 <= status < 300:
        return Outcome.OK, ()
    if sparse:
        js = tuple(m for m in (*_JS_TEXT, *_JS_FRAMEWORK) if m in text)
        if js:
            return Outcome.NEEDS_JS, js
        if text.count("<script") >= 6:
            return Outcome.NEEDS_JS, ("scripts>=6,anchors<3",)
    return Outcome.OK, ()
