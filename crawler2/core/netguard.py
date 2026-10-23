"""Outbound-destination validation (SSRF guard) shared by every fetch path.

Born in P8 for media probe/hash requests (ported from the fingerprinter's
``acquisition/ssrf_guard.py``), generalised in P14 (TC-060/TC-061) so page
fetching uses the same rules.

Rejected: loopback, private, link-local (incl. cloud metadata 169.254.169.254),
unspecified, multicast, reserved and 100.64.0.0/10 (RFC 6598) addresses,
IPv4-mapped IPv6 included, legacy numeric IPv4 spellings (decimal, octal, hex,
short forms), and any scheme other than http/https. A rejected request never
reaches the network.

Three layers, strongest first:

``PinnedTransport``
    Direct (no proxy) HTTP(S). The TCP connect itself resolves the host,
    validates **every** answer and connects to the validated address, so DNS
    rebinding between "check" and "connect" is impossible. The httpcore
    origin, HTTP ``Host`` and TLS SNI/certificate name stay the hostname.
``GuardedTransport``
    Wraps any httpx transport; checks every request (every manually followed
    redirect hop) before the inner transport sees it. With the default inner
    transport it is ``PinnedTransport``.
``check_url_literal`` / ``check_url_resolved``
    For paths where this process does not own the connection. Proxied
    (Tor ``socks5h``) fetching resolves the name at the proxy, so only
    literals and internal-looking names can be refused locally;
    browser requests are resolved here and checked but Chromium resolves
    again (residual TOCTOU, documented in the P14 security model).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Sequence
from typing import Any

import httpcore
import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Sequence[tuple[Any, ...]]]
"""``socket.getaddrinfo``-shaped: (family, type, proto, canonname, sockaddr)."""

_EXTRA_UNSAFE_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"),)
_SCHEMES = frozenset({"http", "https"})
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")
_INTERNAL_NAMES = frozenset(
    {"localhost", "ip6-localhost", "ip6-loopback", "metadata.google.internal"}
)


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
    literal = legacy_ip_literal(hostname)
    if literal is not None:  # never leave exotic spellings (2130706433, 0x7f.1) to the resolver
        return [literal]
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


def legacy_ip_literal(host: str) -> IPAddress | None:
    """The address ``host`` denotes if it is an IP literal in any spelling a resolver accepts.

    ``2130706433``, ``0x7f.1``, ``0177.0.0.1`` and ``127.1`` all mean 127.0.0.1 to
    ``inet_aton``; ``ipaddress`` accepts only the canonical forms.
    """
    host = host.strip("[]")
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    if not host or not all(c in "0123456789abcdefABCDEFxX." for c in host):
        return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def _is_allowed(addr: IPAddress, allowed: Sequence[Network]) -> bool:
    return any(addr in net for net in allowed if addr.version == net.version)


_LOOPBACK = ipaddress.ip_address("127.0.0.1")


def check_url_literal(
    url: str, *, allowed: Sequence[Network] = (), internal_names: bool = True
) -> str:
    """Refuse what can be refused without DNS; return the lower-cased host.

    Used where the name is resolved elsewhere (the Tor proxy). Rejects bad schemes,
    missing hosts, unsafe IP literals in any spelling and internal-looking names.
    """
    parts = httpx.URL(url)
    if parts.scheme not in _SCHEMES:
        raise UnsafeDestinationError(f"scheme {parts.scheme!r} is not allowed")
    host = parts.host.lower().rstrip(".")
    if not host:
        raise UnsafeDestinationError("request has no host")
    literal = legacy_ip_literal(host)
    if literal is not None:
        if is_unsafe_address(literal) and not _is_allowed(literal, allowed):
            raise UnsafeDestinationError(f"{host!r} is a disallowed internal/reserved address")
    elif internal_names and (host in _INTERNAL_NAMES or host.endswith(_INTERNAL_SUFFIXES)):
        loopback_name = host == "localhost" or host.endswith(".localhost")
        if not (loopback_name and _is_allowed(_LOOPBACK, allowed)):
            raise UnsafeDestinationError(f"{host!r} is an internal-only name")
    return host


def check_url_resolved(
    url: str, resolver: Resolver = default_resolver, *, allowed: Sequence[Network] = ()
) -> list[IPAddress]:
    """``check_url_literal`` plus resolution of the name (blocking; run in a thread)."""
    host = check_url_literal(url, allowed=allowed, internal_names=False)  # resolution decides
    parts = httpx.URL(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return validate_destination(host, port, resolver, allowed=allowed)


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolves, validates and connects in one step: the connection goes to a validated address.

    httpcore calls ``connect_tcp`` with the origin hostname; TLS, SNI and ``Host`` are
    derived from the origin, not from the address connected to.
    """

    def __init__(
        self,
        resolver: Resolver = default_resolver,
        allowed: Sequence[Network] = (),
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver
        self._allowed = tuple(allowed)
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            async with asyncio.timeout(timeout):  # resolution counts against the connect budget
                addresses = await asyncio.to_thread(
                    validate_destination, host, port, self._resolver, allowed=self._allowed
                )
        except socket.gaierror as exc:
            raise httpcore.ConnectError(f"cannot resolve {host!r}: {exc}") from exc
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(f"resolving {host!r} timed out") from exc
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        assert last is not None  # noqa: S101 -- validate_destination never returns []
        raise last

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeDestinationError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connections are pinned to validated addresses (no proxy)."""

    def __init__(
        self,
        *,
        resolver: Resolver = default_resolver,
        allowed: Sequence[Network] = (),
        limits: httpx.Limits | None = None,
        on_reject: Callable[[str], None] | None = None,
    ) -> None:
        limits = limits or httpx.Limits()
        super().__init__(limits=limits)
        # httpx exposes no network_backend parameter; rebuild the pool the way it does.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            network_backend=PinnedNetworkBackend(resolver, allowed),
        )
        self._on_reject = on_reject

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await super().handle_async_request(request)
        except UnsafeDestinationError as exc:  # raised by PinnedNetworkBackend at connect time
            if self._on_reject is not None:
                self._on_reject(str(request.url.host))
            raise exc from None


class GuardedTransport(httpx.AsyncBaseTransport):
    """Checks every outgoing request's destination before the inner transport sees it.

    Without an ``inner`` transport the connection itself is pinned (``PinnedTransport``);
    with a custom one (mock transports, tests) only the pre-check applies.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None = None,
        *,
        resolver: Resolver = default_resolver,
        allowed: Sequence[Network] = (),
        on_reject: Callable[[str], None] | None = None,
        limits: httpx.Limits | None = None,
    ) -> None:
        self._inner = inner or PinnedTransport(
            resolver=resolver, allowed=allowed, limits=limits, on_reject=on_reject
        )
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
