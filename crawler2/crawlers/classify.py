"""Exception → outcome, by type first (port of V1 ``core/failure_classifier.py``).

V1 classified ``str(exc)`` after every engine had flattened its exception,
so a bare ``TimeoutError`` became "unknown" and aiohttp's ``ssl:default``
boilerplate once looked like TLS (V1 N3 limitations). Here the fetchers
pass the exception itself; only Chromium's ``net::ERR_*`` codes — stable
error identifiers, not prose — are matched in browser messages.
"""

from __future__ import annotations

import socket
import ssl

import httpx

from crawler2.crawlers.model import ErrorInfo, Outcome

_NET_DNS = ("net::err_name_not_resolved", "net::err_name_resolution_failed")
_NET_TLS = ("net::err_ssl", "net::err_cert", "net::err_bad_ssl")
_NET_TIMEOUT = ("net::err_timed_out", "net::err_connection_timed_out")
_NET_PROXY = ("net::err_proxy_connection_failed", "net::err_tunnel_connection_failed")
_NET_REDIRECT = ("net::err_too_many_redirects", "net::err_unsafe_redirect")
_NET_INVALID = (
    "net::err_invalid_response",
    "net::err_empty_response",
    "net::err_content_length_mismatch",
    "net::err_incomplete_chunked_encoding",
    "net::err_invalid_http_response",
    "net::err_response_headers_",
)
_NET_CONNECTION = ("net::err_connection", "net::err_address_unreachable", "net::err_network_")


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen and len(seen) < 8:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def error_info(exc: BaseException, limit: int = 160) -> ErrorInfo:
    message = str(exc).splitlines()[0] if str(exc) else ""
    return ErrorInfo(kind=type(exc).__name__, message=message[:limit])


def classify_exception(exc: BaseException) -> Outcome:
    """Map a transport exception onto an outcome; unknown shapes are ``network_error``."""
    for item in _chain(exc):
        if isinstance(item, httpx.TooManyRedirects):
            return Outcome.REDIRECT_ERROR
        if isinstance(item, httpx.TimeoutException | TimeoutError):
            return Outcome.TIMEOUT
        if isinstance(item, ssl.SSLError | ssl.CertificateError):
            return Outcome.TLS_ERROR
        if isinstance(item, socket.gaierror):
            return Outcome.DNS_ERROR
        if isinstance(item, httpx.RemoteProtocolError | httpx.DecodingError):
            return Outcome.INVALID_RESPONSE
        if isinstance(item, httpx.ProxyError):
            return Outcome.NETWORK_ERROR
    text = str(exc).lower()
    for codes, outcome in (
        (_NET_DNS, Outcome.DNS_ERROR),
        (_NET_TLS, Outcome.TLS_ERROR),
        (_NET_TIMEOUT, Outcome.TIMEOUT),
        (_NET_PROXY, Outcome.PROXY_UNAVAILABLE),
        (_NET_REDIRECT, Outcome.REDIRECT_ERROR),
        (_NET_INVALID, Outcome.INVALID_RESPONSE),
        (_NET_CONNECTION, Outcome.NETWORK_ERROR),
    ):
        if any(code in text for code in codes):
            return outcome
    for item in _chain(exc):
        if isinstance(item, httpx.ConnectError | ConnectionError | OSError):
            return Outcome.NETWORK_ERROR
    return Outcome.NETWORK_ERROR
