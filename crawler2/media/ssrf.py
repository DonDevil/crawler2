"""Resolved-destination validation for P8's outbound media requests (SSRF guard).

Ported from the fingerprinter's ``acquisition/ssrf_guard.py`` (same address
rules, same conservative "any unsafe record rejects" policy), wrapped as an
httpx transport so it runs for **every request**, including every manually
followed redirect hop, on both the probe path (P4 ``HttpFetcher`` via its
``transport`` parameter) and the hash reader (design §5.3).

Rejected: loopback, private, link-local, unspecified, multicast, reserved
and 100.64.0.0/10 (RFC 6598) addresses, IPv4-mapped IPv6 included, and any
scheme other than http/https. A rejected request never reaches the inner
transport.

**DNS rebinding (TOCTOU) is not solved here** and stays deferred to P14,
as in the fingerprinter: the guard resolves the host itself, and the inner
transport resolves again when it connects. An attacker controlling DNS can
answer differently between the two lookups. Closing that needs connection
pinning to the validated address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Sequence
from typing import Any

import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Sequence[tuple[Any, ...]]]
"""``socket.getaddrinfo``-shaped: (family, type, proto, canonname, sockaddr)."""

_EXTRA_UNSAFE_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"),)
_SCHEMES = frozenset({"http", "https"})


class UnsafeDestinationError(httpx.ConnectError):
    """The destination resolves to an address that must never be contacted."""


def default_resolver(hostname: str, port: int) -> Sequence[tuple[Any, ...]]:
    return socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)


def is_unsafe_address(addr: IPAddress) -> bool:
    """True if ``addr`` must not be connected to."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_multicast
        or addr.is_reserved
    ):
        return True
    return any(addr in net for net in _EXTRA_UNSAFE_NETWORKS if addr.version == net.version)


def resolve_addresses(
    hostname: str, port: int, resolver: Resolver = default_resolver
) -> list[IPAddress]:
    """All addresses of ``hostname``. ``socket.gaierror`` propagates unchanged."""
    addresses: list[IPAddress] = []
    for _family, _type, _proto, _canonname, sockaddr in resolver(hostname, port):
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    if not addresses:
        raise socket.gaierror(f"no usable addresses resolved for {hostname!r}")
    return addresses


def validate_destination(
    hostname: str,
    port: int,
    resolver: Resolver = default_resolver,
    *,
    allowed: Sequence[Network] = (),
) -> list[IPAddress]:
    """Resolve ``hostname``; return its addresses if every one is safe (or allowed).

    Conservative: one unsafe record among several rejects the host, since the
    connection may use any of them.
    """
    addresses = resolve_addresses(hostname, port, resolver)
    unsafe = [
        a
        for a in addresses
        if is_unsafe_address(a) and not any(a in net for net in allowed if a.version == net.version)
    ]
    if unsafe:
        raise UnsafeDestinationError(
            f"{hostname!r} resolves to a disallowed internal/reserved address: {unsafe[0]}"
        )
    return addresses


def parse_networks(cidrs: Sequence[str]) -> tuple[Network, ...]:
    return tuple(ipaddress.ip_network(c, strict=False) for c in cidrs)


class GuardedTransport(httpx.AsyncBaseTransport):
    """Checks every outgoing request's destination before the inner transport sees it."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None = None,
        *,
        resolver: Resolver = default_resolver,
        allowed: Sequence[Network] = (),
        on_reject: Callable[[str], None] | None = None,
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._resolver = resolver
        self._allowed = tuple(allowed)
        self._on_reject = on_reject

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        try:
            if url.scheme not in _SCHEMES:
                raise UnsafeDestinationError(f"scheme {url.scheme!r} is not allowed")
            host = url.host
            if not host:
                raise UnsafeDestinationError("request has no host")
            port = url.port or (443 if url.scheme == "https" else 80)
            try:
                await asyncio.to_thread(
                    validate_destination, host, port, self._resolver, allowed=self._allowed
                )
            except socket.gaierror as exc:
                raise httpx.ConnectError(
                    f"cannot resolve {host!r}: {exc}", request=request
                ) from exc
        except UnsafeDestinationError:
            if self._on_reject is not None:
                self._on_reject(str(url.host))
            raise
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
