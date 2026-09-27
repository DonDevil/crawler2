"""Canonical URL form v1 — the input to every URL-derived ID (ADR-008).

Canonicalization v1 is purely *syntactic*: it only removes differences
that RFC 3986 defines as equivalent for http(s), plus the fragment, which
never reaches the server. It deliberately does **not** drop tracking
parameters, sort query parameters, strip ``www.`` or collapse mirrors:
those are policy judgements that can be wrong, and a wrong judgement baked
into identity would irreversibly merge distinct resources. Such semantic
equivalences are relations recorded by later phases, never identity.

Changing these rules changes IDs, so they are frozen for contract major 1.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Self
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

CANONICALIZATION_VERSION = 1

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_SUB_DELIMS = frozenset("!$&'()*+,;=")
_PATH_SAFE = _UNRESERVED | _SUB_DELIMS | frozenset(":@/")
_QUERY_SAFE = _PATH_SAFE | frozenset("?")
_HEX = frozenset("0123456789abcdefABCDEF")
_MAX_URL_LENGTH = 8192


class InvalidUrlError(ValueError):
    """The input cannot be a V2 URL identity (wrong scheme, no host, credentials, ...)."""


def canonicalize_url(raw: str) -> CanonicalUrl:
    """Return the canonical v1 form of an absolute http(s) URL.

    Raises ``InvalidUrlError`` for anything that must not become an identity.
    """
    text = raw.strip()
    if len(text) > _MAX_URL_LENGTH:
        raise InvalidUrlError(f"URL longer than {_MAX_URL_LENGTH} characters")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise InvalidUrlError(f"unparseable URL {raw!r}: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise InvalidUrlError(f"scheme must be http or https: {raw!r}")
    if parts.username is not None or parts.password is not None:
        # Credentials are secrets, not part of a resource's identity.
        raise InvalidUrlError(f"URL must not carry credentials: {raw!r}")
    host = _canonical_host(parts.hostname, raw)
    netloc = host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
    path = _remove_dot_segments(_normalize_escapes(parts.path, _PATH_SAFE)) or "/"
    query = _normalize_escapes(parts.query, _QUERY_SAFE)
    canonical = urlunsplit(SplitResult(scheme, netloc, path, query, ""))
    return str.__new__(CanonicalUrl, canonical)


def _canonical_host(hostname: str | None, raw: str) -> str:
    if not hostname:
        raise InvalidUrlError(f"URL has no host: {raw!r}")
    host = hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return f"[{address.compressed}]" if address.version == 6 else address.compressed
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidUrlError(f"invalid host name in {raw!r}: {exc}") from exc
    if not ascii_host or any(not label for label in ascii_host.split(".")):
        raise InvalidUrlError(f"invalid host name in {raw!r}")
    return ascii_host


def _normalize_escapes(component: str, safe: frozenset[str]) -> str:
    """Uppercase escapes, decode escaped unreserved characters, escape the rest."""
    out: list[str] = []
    i = 0
    while i < len(component):
        char = component[i]
        if char == "%" and _is_escape(component, i):
            decoded = chr(int(component[i + 1 : i + 3], 16))
            out.append(decoded if decoded in _UNRESERVED else component[i : i + 3].upper())
            i += 3
            continue
        if char in safe:
            out.append(char)
        else:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        i += 1
    return "".join(out)


def _is_escape(component: str, index: int) -> bool:
    return (
        index + 2 < len(component) and component[index + 1] in _HEX and component[index + 2] in _HEX
    )


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 §5.2.4 on an already-escaped absolute path."""
    output: list[str] = []
    segments = path.split("/")
    for index, segment in enumerate(segments):
        is_last = index == len(segments) - 1
        if segment == ".":
            if is_last:
                output.append("")
        elif segment == "..":
            if len(output) > 1:
                output.pop()
            if is_last:
                output.append("")
        else:
            output.append(segment)
    result = "/".join(output)
    if path.startswith("/") and not result.startswith("/"):
        result = "/" + result
    return result


class CanonicalUrl(str):
    """A URL already in canonical v1 form. Validation rejects non-canonical input."""

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        canonical = canonicalize_url(value)
        if canonical != value:
            raise InvalidUrlError(f"URL is not canonical: {value!r} (canonical: {canonical!r})")
        return super().__new__(cls, value)

    @property
    def host(self) -> str:
        """The canonical host (IDNA/ASCII, lowercase, IPv6 in brackets, no port)."""
        netloc = urlsplit(self).netloc
        if netloc.startswith("["):
            return netloc[: netloc.index("]") + 1]
        return netloc.rpartition(":")[0] if ":" in netloc else netloc

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(min_length=1, max_length=_MAX_URL_LENGTH),
            serialization=core_schema.to_string_ser_schema(),
        )
