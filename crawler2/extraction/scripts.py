"""URL literals in inline scripts and visible text (P5 design §7). Nothing is executed.

Supported syntax, exactly:

1. absolute ``http(s)://`` literals, also with JSON-escaped slashes
   (``https:\\/\\/``) or ``\\u002F``;
2. quoted protocol-relative literals (``"//host/path"``);
3. quoted relative or absolute-path literals ending in a media extension
   (``"/v/x.m3u8"``) — reported as media candidates only.

Values of the JSON-LD keywords ``@context``, ``@vocab`` and ``@type`` are
vocabulary identifiers, not links, and are skipped.

String concatenation, template expressions, encoded/obfuscated strings and
external script files are not supported.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

_UNESCAPES: Final = (("\\/", "/"), ("\\u002F", "/"), ("\\u002f", "/"), ("\\x2F", "/"))
_TRAILING: Final = ")]},;.'\"\\!?"
_ABSOLUTE = re.compile(r"https?://[^\s\"'<>`\\^{}|]+", re.I)
_PROTOCOL_RELATIVE = re.compile(
    r"""["'](//[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,}(?:[/?:][^"'\s<>]*)?)["']"""
)
_MEDIA_RELATIVE = re.compile(
    r"""["']((?:\.{0,2}/)?[^"'\s<>:?]*\."""
    r"""(?:m3u8|mpd|mp4|webm|mkv|m4v|mov|mp3|m4a|aac|ogg|ogv|opus|flac|wav)"""
    r"""(?:\?[^"'\s<>]*)?)["']""",
    re.I,
)
_PLAYER_KEY = re.compile(
    r"""(?:file|src|source|sources|hls|dash|url|stream|video|contentUrl|embedUrl)["']?\s*[:=]\s*[\[{(]?\s*["']?$""",
    re.I,
)
_VOCABULARY_KEY = re.compile(r""""@(?:context|vocab|type)"\s*:\s*\[?\s*"$""")
_KEY_WINDOW = 32


@dataclass(frozen=True, slots=True)
class Literal:
    value: str
    player_key: bool
    """Directly preceded by a player-config key such as ``file:`` or ``"src":``."""
    media_only: bool
    """Found by rule 3: usable as a media reference, never as a page link."""


def unescape(text: str) -> str:
    if "\\" not in text:
        return text
    for old, new in _UNESCAPES:
        text = text.replace(old, new)
    return text


def _trim(value: str) -> str:
    return value.rstrip(_TRAILING)


def _keyed(text: str, start: int) -> bool:
    return _PLAYER_KEY.search(text[max(0, start - _KEY_WINDOW) : start]) is not None


def _vocabulary(text: str, start: int) -> bool:
    return _VOCABULARY_KEY.search(text[max(0, start - _KEY_WINDOW) : start]) is not None


def script_literals(text: str) -> Iterator[Literal]:
    """URL literals of one script body (already bounded by the caller), in text order."""
    text = unescape(text)
    found: list[tuple[int, Literal]] = []
    for match in _ABSOLUTE.finditer(text):
        if _vocabulary(text, match.start()):
            continue
        found.append(
            (match.start(), Literal(_trim(match.group(0)), _keyed(text, match.start()), False))
        )
    for match in _PROTOCOL_RELATIVE.finditer(text):
        start = match.start(1)
        found.append((start, Literal(_trim(match.group(1)), _keyed(text, start - 1), False)))
    for match in _MEDIA_RELATIVE.finditer(text):
        value = match.group(1)
        if value.startswith("//"):
            continue  # already covered by the protocol-relative rule
        start = match.start(1)
        found.append((start, Literal(value, _keyed(text, start - 1), True)))
    found.sort(key=lambda item: item[0])
    for _, literal in found:
        if literal.value:
            yield literal


def text_urls(text: str) -> Iterator[str]:
    """Absolute http(s) URLs written as plain text (rule 1 without escapes)."""
    for match in _ABSOLUTE.finditer(text):
        value = _trim(match.group(0))
        if value:
            yield value
