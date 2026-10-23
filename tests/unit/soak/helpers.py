"""Shared builders for the soak harness tests: fake clock, synthetic config, in-memory runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crawler2.soak.collectors import build_collector
from crawler2.soak.config import Collector, Edge, Mode, Series, SoakConfig, Thresholds
from crawler2.soak.reconcile import Ledger
from crawler2.soak.runner import Runtime, SoakRunner
from crawler2.soak.store import RunStore
from crawler2.soak.synthetic import Faults, SyntheticVerifier, SyntheticWorkload


class Clock:
    """Wall and monotonic clock that only moves when the runner sleeps (or a test says so)."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start
        self.mono = 1000.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.mono += seconds


ENCODE = Edge(
    name="encode",
    from_type="encode.requested",
    to_types=["representation.ready"],
    from_key="payload.content.content_id",
    to_key="payload.representation.content_id",
    effect_path="payload.representation.representation_id",
    grace_s=300,
)

LIMITS = Thresholds(
    max_interruptions=1,
    max_interruption_gap_s=900,
    min_series_coverage=0.99,
    max_dependency_outage_s=300,
    max_restarts_per_service=1,
    min_disk_free_fraction=0.1,
    max_queue_age_s=600,
    max_event_lag_s=600,
    max_retry_ratio=0.5,
    growth={"svc.rss_mb": {"max_slope_per_h": 5.0, "min_growth_ratio": 0.1}},  # type: ignore[dict-item]
)


def series_options(**overrides: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "rss_mb": {"kind": "const", "base": 500},
        "queue_age": {"kind": "const", "base": 20},
        "lag": {"kind": "const", "base": 5},
        "disk_free": {"kind": "const", "base": 0.6},
        "retries": {"kind": "linear", "base": 0, "slope": 0.1},
        "done": {"kind": "linear", "base": 0, "slope": 5},
    }
    spec.update(overrides)
    return {"series": spec}


def make_config(tmp: Path, **over: Any) -> SoakConfig:
    options = over.pop("options", series_options())
    series = [
        Series(name="up", role="up"),
        Series(name="rss_mb", growth_watch=True),
        Series(name="queue_age", role="queue_age"),
        Series(name="lag", role="event_lag"),
        Series(name="disk_free", role="disk_free"),
        Series(name="retries", role="retry", kind="counter"),
        Series(name="done", role="work", kind="counter"),
    ]
    base: dict[str, Any] = {
        "run_name": "t",
        "mode": Mode.SYNTHETIC,
        "duration_h": 0.5,
        "sample_interval_s": 60.0,
        "run_root": tmp,
        "collectors": [Collector(name="svc", type="synthetic", options=options, series=series)],
        "edges": [ENCODE],
        "required_stages": ["match.found"],
        "thresholds": LIMITS,
    }
    base.update(over)
    return SoakConfig(**base)


def make_runtime(
    cfg: SoakConfig,
    store: RunStore,
    clock: Clock,
    faults: Faults | None = None,
    verifier: SyntheticVerifier | None = None,
) -> Runtime:
    ledger = Ledger(store.path / "ledger.sqlite3", cfg.edges)
    return Runtime(
        collectors={c.name: build_collector(c) for c in cfg.collectors},
        source=SyntheticWorkload(ledger, faults=faults or Faults(finalize_every=3), now=clock.time),
        ledger=ledger,
        verifier=verifier or SyntheticVerifier(),
        clock=clock.time,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def start_run(
    cfg: SoakConfig,
    tmp: Path,
    clock: Clock,
    faults: Faults | None = None,
    verifier: SyntheticVerifier | None = None,
) -> tuple[RunStore, SoakRunner]:
    store = RunStore.create(tmp, cfg.run_name)
    runner = SoakRunner(cfg, store, make_runtime(cfg, store, clock, faults, verifier))
    runner.initialize()
    return store, runner
