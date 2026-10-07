"""P7 → P6 source advice, ``source-advice/v1`` (P7 design §9.4).

``SourceAdvisor.advise`` never raises and never does I/O: it reads an
in-memory snapshot of the projection that a background thread refreshes.
Whenever the projection cannot be trusted (no snapshot, snapshot too old,
checkpoint lagging, other policy), the advice is ``FALLBACK``: admit on http,
which is what admission does without P7 (B.5 #1).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from crawler2.core.observability import Metrics, get_logger
from crawler2.intelligence.projection import ProjectionState, SourceProfileStore
from crawler2.intelligence.sources import DEFAULT_POLICY, SourcePolicy, SourceProfile, advise

ADVICE_VERSION: Final = "source-advice/v1"

_log = get_logger("intelligence.advice")


@dataclass(frozen=True, slots=True)
class SourceAdvice:
    """What P6 may do with a URL of one host. Every field has a consumer (§9.4)."""

    admit: bool
    capability: str
    state: str
    probe: bool = False
    reason: str = ""
    policy: str = ""
    fallback: str | None = None
    """Why no informed advice was given; None when the projection was used."""


def fallback(why: str) -> SourceAdvice:
    """Today's behaviour: admit, start on http."""
    return SourceAdvice(True, "http", "unknown", reason=f"no p7 advice: {why}", fallback=why)


class SourceAdvisor(Protocol):
    def advise(self, domain_id: str, now: datetime) -> SourceAdvice:
        """Must not raise or block (P6 still guards the call)."""
        ...


@dataclass(frozen=True, slots=True)
class ProfileSnapshot:
    state: ProjectionState | None
    profiles: Mapping[str, SourceProfile]
    loaded_at: datetime


def load_snapshot(
    store: SourceProfileStore, projection: str, *, now: datetime, policy: SourcePolicy
) -> ProfileSnapshot:
    """Current-generation, current-policy rows only (others are dead or foreign)."""
    state = store.state(projection)
    profiles: dict[str, SourceProfile] = {}
    if state is not None and state.policy == policy.version:
        for row in store.scan():
            if row.generation == state.generation and row.policy == state.policy:
                profiles[row.profile.domain_id] = row.profile
    return ProfileSnapshot(state, profiles, now)


class SnapshotAdvisor:
    def __init__(
        self,
        load: Callable[[], ProfileSnapshot],
        *,
        max_lag: timedelta,
        max_snapshot_age: timedelta,
        refresh_s: float,
        policy: SourcePolicy = DEFAULT_POLICY,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        metrics: Metrics | None = None,
    ) -> None:
        self._load = load
        self._max_lag = max_lag
        self._max_age = max_snapshot_age
        self._refresh_s = refresh_s
        self._policy = policy
        self._clock = clock
        self._snapshot: ProfileSnapshot | None = None
        self._granted: dict[str, datetime] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._m: dict[str, Any] = {}
        if metrics is not None:
            self._m = {
                "advice": metrics.counter(
                    "intel_source_advice_total",
                    "Source advice given at admission: informed state or fallback reason",
                    ["result"],
                ),
                "age": metrics.gauge(
                    "intel_source_snapshot_age_seconds", "Age of the admission snapshot"
                ),
                "failures": metrics.counter(
                    "intel_source_snapshot_failures_total", "Snapshot refreshes that failed"
                ),
            }

    # --- snapshot ---------------------------------------------------------------

    def refresh(self) -> bool:
        """Swap in a new snapshot; on failure keep the old one (it ages into fallback)."""
        try:
            snapshot = self._load()
        except Exception as exc:  # noqa: BLE001 -- P7 storage must never reach admission
            if self._m:
                self._m["failures"].inc()
            _log.warning("source_snapshot_failed", error=str(exc)[:300])
            return False
        self._snapshot = snapshot
        return True

    def start(self) -> None:
        """Refresh in a daemon thread; the first load is not awaited."""

        def loop() -> None:
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(self._refresh_s)

        self._thread = threading.Thread(target=loop, name="p7-source-snapshot", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    # --- advice -----------------------------------------------------------------

    def advise(self, domain_id: str, now: datetime) -> SourceAdvice:
        try:
            advice = self._advise(domain_id, now)
        except Exception as exc:  # noqa: BLE001 -- a bug here must not block admission
            _log.warning("source_advice_failed", error=str(exc)[:300])
            advice = fallback("error")
        if self._m:
            self._m["advice"].labels(advice.fallback or f"informed:{advice.state}").inc()
        return advice

    def _advise(self, domain_id: str, now: datetime) -> SourceAdvice:
        snapshot = self._snapshot
        if snapshot is None:
            return fallback("unavailable")
        age = now - snapshot.loaded_at
        if self._m:
            self._m["age"].set(age.total_seconds())
        if age > self._max_age:
            return fallback("snapshot_stale")
        state = snapshot.state
        if state is None:
            return fallback("unavailable")
        if state.policy != self._policy.version:
            return fallback("policy_mismatch")
        if state.lag(now) > self._max_lag:
            return fallback("projection_stale")
        p = advise(snapshot.profiles.get(domain_id), now, self._policy)
        advice = SourceAdvice(
            admit=p.admit,
            capability=p.capability,
            state=p.state.value,
            probe=p.probe,
            reason=p.reason,
            policy=p.policy,
        )
        if advice.probe:
            advice = self._take_probe(domain_id, now, advice)
        return advice

    def _take_probe(self, domain_id: str, now: datetime, advice: SourceAdvice) -> SourceAdvice:
        """One probe per host per ``probe_min`` in this process (design §9.4)."""
        with self._lock:
            granted = self._granted.get(domain_id)
            if granted is not None and now - granted < self._policy.probe_min:
                return replace(
                    advice,
                    admit=False,
                    probe=False,
                    reason=f"probe already granted at {granted:%FT%TZ}; {advice.reason}",
                )
            self._granted[domain_id] = now
            if len(self._granted) > 100_000:  # bounded: forget the oldest grants
                for key, _ in sorted(self._granted.items(), key=lambda kv: kv[1])[:50_000]:
                    del self._granted[key]
        return advice
