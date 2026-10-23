"""Per-domain back-off on rate limiting (P6 X-14).

A domain that answers 429 (or 503 with ``Retry-After``) to every request used to be
re-claimed at full rate: 25 % of M1 run 2's fetches. On such a response the worker
widens that domain's politeness interval (the existing P3 ``set_domain_interval``
override, shared through Redis) to ``max(Retry-After, base * 2^(n-1))`` capped at
``max_s``, and clears it on the first success or when the window has passed. Only
the interval changes; the task itself is retried by the normal retry budget, so no
work is dropped and no attempt is refunded.

The consecutive-count is per process; the interval it produces is shared. A worker
that dies mid-window leaves its override in Redis until another worker's next
success or sweep for that domain clears it (operators: ``set_domain_interval(d, None)``).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from crawler2.core.configuration.settings import RateLimitBackoffSettings

_LIMIT_STATUSES = frozenset({429, 503})


def parse_retry_after(value: str | None, *, now_epoch: float | None = None) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds or HTTP-date); None if unusable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(int(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = time.time() if now_epoch is None else now_epoch
    return max(0.0, when.timestamp() - now)


@dataclass(slots=True)
class _State:
    strikes: int
    until: float


class DomainBackoff:
    """Pure bookkeeping; the caller applies the returned intervals to the frontier."""

    def __init__(
        self,
        settings: RateLimitBackoffSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_domains: int = 10_000,
    ) -> None:
        self._s = settings
        self._clock = clock
        self._max_domains = max_domains
        self._state: OrderedDict[str, _State] = OrderedDict()

    def __len__(self) -> int:
        return len(self._state)

    @staticmethod
    def is_rate_limited(status: int | None, headers: Mapping[str, str]) -> bool:
        if status == 429:
            return True
        return status in _LIMIT_STATUSES and "retry-after" in headers

    def on_rate_limited(self, domain: str, headers: Mapping[str, str]) -> float:
        """Interval (seconds) to apply to ``domain`` now."""
        state = self._state.pop(domain, None) or _State(0, 0.0)
        state.strikes = min(state.strikes + 1, 30)
        retry_after = parse_retry_after(headers.get("retry-after"))
        wait: float = self._s.base_s * 2 ** (state.strikes - 1)
        if retry_after is not None:
            wait = max(wait, min(retry_after, self._s.max_s))
        interval: float = min(wait, self._s.max_s)
        state.until = self._clock() + max(interval, self._s.min_window_s)
        self._state[domain] = state
        while len(self._state) > self._max_domains:  # forget the least recently limited
            self._state.popitem(last=False)
        return interval

    def on_success(self, domain: str) -> bool:
        """True if an override was active and should now be cleared."""
        return self._state.pop(domain, None) is not None

    def expired(self) -> list[str]:
        """Domains whose window passed without a further 429 (override to clear)."""
        now = self._clock()
        done = [d for d, st in self._state.items() if st.until <= now]
        for d in done:
            del self._state[d]
        return done
