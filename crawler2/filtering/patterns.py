"""ABP network-filter URL patterns → regular expressions and index tokens (design §7).

Supported pattern syntax: ``||`` (host anchor at a label boundary), ``|``
(start/end anchor), ``*`` (any run), ``^`` (separator: any character that
is not a letter, digit, ``_``, ``-``, ``.`` or ``%``, or the end of the URL).
Matching is case-insensitive (no ``$match-case``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

TOKEN_RE: Final = re.compile(r"[a-z0-9]+")
"""URL and pattern tokens: maximal lowercase alphanumeric runs."""

_HOST_ANCHOR: Final = r"^[a-z][a-z0-9+.\-]*://(?:[^/?#]*\.)?"
_SEPARATOR: Final = r"(?:[^a-z0-9_.%\-]|$)"
_HOST_ONLY_RE: Final = re.compile(r"^\|\|([a-z0-9\-.]+)\^?\|?$")
_HOST_CHARS_RE: Final = re.compile(r"^[a-z0-9\-]+(\.[a-z0-9\-]+)*$")


class PatternError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CompiledPattern:
    regex: re.Pattern[str]
    tokens: tuple[str, ...]
    """Tokens every matching URL must contain as whole tokens (candidates for the index)."""
    literal_length: int
    """Specificity: number of literal (non-wildcard, non-anchor) characters."""


def host_only(pattern: str) -> str | None:
    """``||example.com^`` (optionally ``|``-terminated) → ``example.com``; else None.

    Such a pattern matches exactly the host and its subdomains, so it is a
    host rule, not a URL pattern.
    """
    match = _HOST_ONLY_RE.match(pattern)
    if match is None:
        return None
    host = match.group(1).strip(".")
    if not host or not _HOST_CHARS_RE.match(host):
        return None
    # "||example.com" without "^" would also match "example.company"; not a host rule.
    return host if pattern.rstrip("|").endswith("^") else None


def compile_pattern(pattern: str) -> CompiledPattern:
    body = pattern.lower()
    if not body or body in ("*", "|", "||"):
        raise PatternError("pattern matches every URL")
    if body.startswith("/") and body.endswith("/") and len(body) > 1:
        raise PatternError("regular-expression filters are not supported")
    host_anchor = body.startswith("||")
    start_anchor = not host_anchor and body.startswith("|")
    if host_anchor:
        body = body[2:]
    elif start_anchor:
        body = body[1:]
    end_anchor = body.endswith("|")
    if end_anchor:
        body = body[:-1]
    if "|" in body:
        raise PatternError("'|' is only allowed as an anchor")
    stripped = body.strip("*")
    if not stripped or stripped == "^":
        raise PatternError("pattern matches every URL")

    parts = [_HOST_ANCHOR if host_anchor else ("^" if start_anchor else "")]
    for char in body:
        if char == "*":
            parts.append(".*")
        elif char == "^":
            parts.append(_SEPARATOR)
        else:
            parts.append(re.escape(char))
    if end_anchor:
        parts.append("$")
    regex = re.compile("".join(parts))
    literal = sum(1 for c in body if c not in "*^")
    tokens = _bounded_tokens(body, host_anchor or start_anchor, end_anchor)
    return CompiledPattern(regex, tokens, literal)


def _bounded_tokens(body: str, anchored_start: bool, anchored_end: bool) -> tuple[str, ...]:
    """Tokens whose both ends are fixed by the pattern (never extended by ``*`` or an open end)."""
    tokens = []
    for match in TOKEN_RE.finditer(body):
        start, end = match.span()
        left_ok = body[start - 1] != "*" if start > 0 else anchored_start
        right_ok = body[end] != "*" if end < len(body) else anchored_end
        if left_ok and right_ok:
            tokens.append(match.group())
    return tuple(dict.fromkeys(tokens))
