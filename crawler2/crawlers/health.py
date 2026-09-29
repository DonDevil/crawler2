"""Per-process network health (port of V1 ``core/network_health.py``, N1-N7).

HEALTHY → SUSPECT when ``trigger_threshold`` ambiguous failures (timeout,
DNS, connection) arrive with no success in between; SUSPECT starts a probe
round (HTTPS HEAD, no redirects, any status = reachable) against at least
two independent endpoints. Two failed rounds ``confirm_delay_s`` apart →
OFFLINE; ``recovery_confirm_rounds`` successful rounds → HEALTHY.
Classification alone never grants an attempt refund: only OFFLINE does
(``decide``). State is in memory and per process — one host's outage must
never pause another host, so nothing here touches Redis.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum

import httpx

from crawler2.core.configuration.settings import NetworkHealthSettings
from crawler2.core.observability import get_logger
from crawler2.crawlers.model import AMBIGUOUS_OUTCOMES, Outcome

_log = get_logger("crawlers.health")

ProbeRound = Callable[[], Awaitable[bool]]


class NetworkState(StrEnum):
    HEALTHY = "healthy"
    SUSPECT = "suspect"
    OFFLINE = "offline"


def http_probe(endpoints: Sequence[str], timeout_s: float) -> ProbeRound:
    async def probe_round() -> bool:
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=timeout_s, trust_env=False
        ) as client:

            async def one(url: str) -> bool:
                try:
                    await client.head(url)
                except httpx.HTTPError:
                    return False
                return True

            return any(await asyncio.gather(*(one(url) for url in endpoints)))

    return probe_round


class NetworkHealth:
    def __init__(self, settings: NetworkHealthSettings, probe: ProbeRound | None = None) -> None:
        self._s = settings
        self._probe = probe or http_probe(settings.probe_endpoints, settings.probe_timeout_s)
        self._state = NetworkState.HEALTHY
        self._ambiguous = 0
        self._task: asyncio.Task[None] | None = None
        self.transitions: list[NetworkState] = []

    @property
    def state(self) -> NetworkState:
        return self._state

    @property
    def offline(self) -> bool:
        return self._state is NetworkState.OFFLINE

    def _set(self, state: NetworkState) -> None:
        if state is not self._state:
            _log.warning("network_state", previous=self._state.value, state=state.value)
            self._state = state
            self.transitions.append(state)

    def observe(self, outcome: Outcome) -> None:
        """Feed one attempt outcome (called by the runtime for every attempt)."""
        if not self._s.enabled or self._state is NetworkState.OFFLINE:
            return
        if outcome in AMBIGUOUS_OUTCOMES:
            self._ambiguous += 1
            if self._ambiguous >= self._s.trigger_threshold and self._task is None:
                self._set(NetworkState.SUSPECT)
                self._task = asyncio.ensure_future(self._confirm())
        elif outcome.has_response:
            self._ambiguous = 0

    async def _confirm(self) -> None:
        try:
            if await self._probe():
                self._back_to_healthy()
                return
            await asyncio.sleep(self._s.confirm_delay_s)
            if await self._probe():
                self._back_to_healthy()
                return
            self._set(NetworkState.OFFLINE)
            successes = 0
            while successes < self._s.recovery_confirm_rounds:
                await asyncio.sleep(self._s.recovery_probe_interval_s)
                successes = successes + 1 if await self._probe() else 0
            self._back_to_healthy()
        finally:
            self._task = None

    def _back_to_healthy(self) -> None:
        self._ambiguous = 0
        self._set(NetworkState.HEALTHY)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
