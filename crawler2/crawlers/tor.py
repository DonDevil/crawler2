"""Tor transport: the HTTP fetcher over ``socks5h://`` (design §13).

Proxy resolution is V1's (``tor/proxy_config.py``): explicit setting, then
``TOR_SOCKS_PROXY``, then ``TOR_SOCKS_PORT``, then the first open local
port of ``probe_ports``. Unlike V1 there is no silent default: without a
reachable proxy the fetcher is ``unavailable`` and a failure caused by the
proxy is ``proxy_unavailable`` (local infrastructure, deferred) rather
than a target failure. No bypass or circuit tricks: Tor is transport only.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from urllib.parse import urlsplit

from antipiracy_contracts.models.web import FetchCapability

from crawler2.core.configuration.settings import HttpFetchSettings, TorSettings
from crawler2.crawlers.http import HttpFetcher
from crawler2.crawlers.model import FetcherHealth, FetcherState, FetcherUnavailableError

_PROBE_TIMEOUT_S = 1.0


async def _port_open(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), _PROBE_TIMEOUT_S)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def resolve_tor_proxy(settings: TorSettings) -> str | None:
    """The SOCKS URL to use, or None when no Tor proxy can be found."""
    if settings.socks_proxy:
        return settings.socks_proxy
    if env := os.environ.get("TOR_SOCKS_PROXY"):
        return env
    if port := os.environ.get("TOR_SOCKS_PORT"):
        return f"socks5h://127.0.0.1:{int(port)}"
    for candidate in settings.probe_ports:
        if await _port_open("127.0.0.1", candidate):
            return f"socks5h://127.0.0.1:{candidate}"
    return None


class TorFetcher(HttpFetcher):
    def __init__(
        self, settings: HttpFetchSettings, tor: TorSettings, *, max_connections: int = 16
    ) -> None:
        super().__init__(
            settings, capability=FetchCapability.TOR_HTTP, max_connections=max_connections
        )
        self._tor = tor
        self._reachable = False
        self._checked_at = 0.0

    async def start(self) -> None:
        proxy = await resolve_tor_proxy(self._tor)
        if proxy is None:
            raise FetcherUnavailableError("no Tor SOCKS proxy configured or listening")
        self._proxy = proxy
        if not await self._check():
            raise FetcherUnavailableError(f"Tor proxy {proxy} is not reachable")
        await super().start()

    async def _check(self) -> bool:
        parts = urlsplit(self._proxy or "")
        self._reachable = bool(parts.hostname and parts.port) and await _port_open(
            parts.hostname or "", parts.port or 0
        )
        self._checked_at = time.monotonic()
        return self._reachable

    async def proxy_unavailable(self) -> bool:
        return not await self._check()

    async def refresh_health(self) -> None:
        """Re-probe the proxy at most every ``health_interval_s``."""
        if time.monotonic() - self._checked_at >= self._tor.health_interval_s:
            await self._check()

    def health(self) -> FetcherHealth:
        if not self._reachable:
            return FetcherHealth(FetcherState.UNAVAILABLE, "tor proxy unreachable")
        return super().health()
