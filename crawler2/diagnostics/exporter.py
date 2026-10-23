"""Pipeline exporter: the SLIs no worker process can report about itself (P14, TC-050/TC-053).

Workers expose what they did; nobody exposed what is *waiting*. This process reads, on
scrape (cached for ``min_interval_s`` so Prometheus cannot hammer the services):

* frontier depth per queue, scheduled/leased/gated/saturated counts and dead letters;
* every event stream's length and, per consumer group, pending entries, lag and the age of
  the oldest unacknowledged entry (event lag / stuck consumer);
* the outbox relay's checkpoint lag per shard (rows older than this may not be published);
* Redis memory (used / maxmemory) and optionally GPU utilisation and memory.

A source that cannot be read is reported ``crawler2_exporter_source_up{source}=0`` and its
series are *absent*, never zero. Metric names are documented in
docs/phases/p14-production-hardening/observability.md.

    crawler2-exporter [--no-scylla] [--gpu] [--min-interval 15]
"""

from __future__ import annotations

import argparse
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from crawler2.core.configuration import Settings
from crawler2.core.observability import Metrics, configure_logging, get_logger
from crawler2.storage.events.publisher import stream_name

_log = get_logger("diagnostics.exporter")

EVENT_TYPES: tuple[tuple[str, int], ...] = (
    ("crawl.requested", 1),
    ("fetch.completed", 1),
    ("page.observed", 1),
    ("page.changed", 1),
    ("urls.discovered", 1),
    ("media.discovered", 1),
    ("media.observed", 1),
    ("encode.requested", 1),
    ("representation.ready", 1),
    ("encode.failed", 1),
    ("match.found", 1),
    ("evidence.candidate_created", 1),
    ("evidence.finalized", 1),
)
"""Streams watched (docs: event catalog); a stream that does not exist yet is simply skipped."""


class PipelineCollector(Collector):
    def __init__(
        self,
        settings: Settings,
        *,
        redis_client: Any,
        frontier: Any,
        outbox: Any,
        gpu: bool = False,
        min_interval_s: float = 15.0,
        clock: Callable[[], float] = time.time,
        gpu_reader: Callable[[], list[float] | None] | None = None,
    ) -> None:
        self._s = settings
        self._redis = redis_client
        self._frontier = frontier
        self._outbox = outbox
        self._gpu = gpu
        self._gpu_reader = gpu_reader or _read_gpu
        self._min = min_interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: list[Any] = []
        self._at = 0.0

    def collect(self) -> Iterable[Any]:
        with self._lock:
            now = self._clock()
            if not self._cached or now - self._at >= self._min:
                self._cached = self._gather(now)
                self._at = now
            return list(self._cached)

    # -- gathering ------------------------------------------------------------------------
    def _gather(self, now: float) -> list[Any]:
        fams: dict[str, GaugeMetricFamily] = {}

        def gauge(name: str, doc: str, labels: tuple[str, ...] = ()) -> GaugeMetricFamily:
            if name not in fams:
                fams[name] = GaugeMetricFamily(f"crawler2_{name}", doc, labels=labels)
            return fams[name]

        up = gauge("exporter_source_up", "1 if the source could be read this round", ("source",))

        redis_ok = self._redis_metrics(gauge, now)
        up.add_metric(["redis"], 1.0 if redis_ok else 0.0)
        if self._frontier is not None:
            up.add_metric(["frontier"], 1.0 if self._frontier_metrics(gauge) else 0.0)
        if self._outbox is not None:
            up.add_metric(["outbox"], 1.0 if self._outbox_metrics(gauge, now) else 0.0)
        if self._gpu:
            up.add_metric(["gpu"], 1.0 if self._gpu_metrics(gauge) else 0.0)
        gauge(
            "exporter_last_refresh_timestamp_seconds", "Unix time of the last refresh"
        ).add_metric([], now)
        return list(fams.values())

    def _redis_metrics(self, gauge: Callable[..., GaugeMetricFamily], now: float) -> bool:
        try:
            info = self._redis.info()
        except Exception as exc:  # noqa: BLE001 -- unreadable source = absent series
            _log.warning("exporter_redis_failed", error=str(exc)[:120])
            return False
        used, maxmem = info.get("used_memory", 0), info.get("maxmemory", 0)
        gauge("redis_memory_used_bytes", "Redis used_memory").add_metric([], float(used))
        if maxmem:
            gauge("redis_memory_ratio", "used_memory / maxmemory").add_metric([], used / maxmem)
        gauge("redis_evicted_keys_total", "Redis evicted_keys (should stay 0)").add_metric(
            [], float(info.get("evicted_keys", 0))
        )
        for etype, major in EVENT_TYPES:
            key = stream_name(self._s.events, etype, major)
            try:
                length = self._redis.xlen(key)
            except Exception as exc:  # noqa: BLE001 -- not created yet
                _log.debug("exporter_stream_missing", stream=key, error=str(exc)[:80])
                continue
            gauge(
                "stream_length", "Entries retained in an event stream", ("event_type",)
            ).add_metric([etype], float(length))
            try:
                groups = self._redis.xinfo_groups(key)
            except Exception as exc:  # noqa: BLE001
                _log.debug("exporter_groups_failed", stream=key, error=str(exc)[:80])
                continue
            for g in groups:
                name = g["name"] if isinstance(g["name"], str) else g["name"].decode()
                labels = [etype, name]
                gauge(
                    "stream_group_pending",
                    "Delivered, unacknowledged entries",
                    ("event_type", "group"),
                ).add_metric(labels, float(g.get("pending", 0)))
                if g.get("lag") is not None:
                    gauge(
                        "stream_group_lag",
                        "Entries not yet delivered to the group",
                        ("event_type", "group"),
                    ).add_metric(labels, float(g["lag"]))
                age = 0.0
                try:
                    summary = self._redis.xpending(key, name)
                    oldest = summary.get("min") if isinstance(summary, dict) else None
                    if oldest:
                        age = max(0.0, now - int(str(oldest).split("-")[0]) / 1000)
                except Exception as exc:  # noqa: BLE001
                    _log.debug("exporter_pending_failed", stream=key, error=str(exc)[:80])
                    continue
                gauge(
                    "stream_group_oldest_pending_age_seconds",
                    "Age of the oldest unacknowledged entry",
                    ("event_type", "group"),
                ).add_metric(labels, age)
        return True

    def _frontier_metrics(self, gauge: Callable[..., GaugeMetricFamily]) -> bool:
        try:
            st = self._frontier.stats()
        except Exception as exc:  # noqa: BLE001
            _log.warning("exporter_frontier_failed", error=str(exc)[:120])
            return False
        for q, depth in st.depth.items():
            gauge("frontier_depth", "Tasks per execution queue", ("queue",)).add_metric(
                [q.value], float(depth)
            )
        for q, n in st.eligible_domains.items():
            gauge(
                "frontier_eligible_domains", "Domains with a claimable task", ("queue",)
            ).add_metric([q.value], float(n))
        gauge("frontier_scheduled", "Tasks waiting for their time").add_metric(
            [], float(st.scheduled)
        )
        gauge("frontier_leased", "Tasks currently leased by workers").add_metric(
            [], float(st.leased)
        )
        gauge("frontier_gated_domains", "Domains inside their politeness gap").add_metric(
            [], float(st.gated_domains)
        )
        gauge("frontier_saturated_domains", "Domains at their in-flight limit").add_metric(
            [], float(st.saturated_domains)
        )
        gauge("frontier_dead_letters", "Tasks in the dead-letter set").add_metric(
            [], float(st.dead_letters)
        )
        return True

    def _outbox_metrics(self, gauge: Callable[..., GaugeMetricFamily], now: float) -> bool:
        from crawler2.storage.layout import OUTBOX_BUCKET_SECONDS, OUTBOX_SHARDS

        try:
            worst = 0.0
            for shard in range(OUTBOX_SHARDS):
                checkpoint = self._outbox.checkpoint(shard)
                if checkpoint is None:
                    continue
                worst = max(worst, now - checkpoint * OUTBOX_BUCKET_SECONDS)
        except Exception as exc:  # noqa: BLE001
            _log.warning("exporter_outbox_failed", error=str(exc)[:120])
            return False
        gauge(
            "outbox_checkpoint_lag_seconds",
            "Worst relay checkpoint age over all shards (includes the settle window)",
        ).add_metric([], max(0.0, worst))
        return True

    def _gpu_metrics(self, gauge: Callable[..., GaugeMetricFamily]) -> bool:
        reading = self._gpu_reader()
        if reading is None:
            return False
        util, used, total = reading
        gauge("gpu_utilization_percent", "GPU utilisation").add_metric([], util)
        gauge("gpu_memory_used_bytes", "GPU memory in use").add_metric([], used * 2**20)
        gauge("gpu_memory_total_bytes", "GPU memory capacity").add_metric([], total * 2**20)
        return True


def _read_gpu() -> list[float] | None:
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 -- fixed argv
            [
                binary,
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        values = [float(x) for x in out.strip().splitlines()[0].split(",")]
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return None
    return values if len(values) == 3 else None


def build(
    settings: Settings, *, scylla: bool, gpu: bool, min_interval_s: float
) -> tuple[Metrics, PipelineCollector]:
    from crawler2.frontier.redis import RedisFrontier, connect_redis

    client = connect_redis(settings.redis)
    frontier = RedisFrontier(client, settings.frontier, namespace=settings.redis.namespace)
    outbox = None
    if scylla:
        from crawler2.storage.scylla import ScyllaStorage

        outbox = ScyllaStorage.open(settings.scylla, instance="exporter").outbox
    metrics = Metrics(settings)
    collector = PipelineCollector(
        settings,
        redis_client=client,
        frontier=frontier,
        outbox=outbox,
        gpu=gpu,
        min_interval_s=min_interval_s,
    )
    metrics.registry.register(collector)
    return metrics, collector


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-exporter", description=__doc__)
    parser.add_argument("--no-scylla", action="store_true", help="skip the outbox checkpoint lag")
    parser.add_argument(
        "--gpu", action="store_true", help="export nvidia-smi GPU metrics (GPU hosts)"
    )
    parser.add_argument(
        "--min-interval", type=float, default=15.0, help="seconds between source reads"
    )
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings)
    metrics, _ = build(
        settings, scylla=not args.no_scylla, gpu=args.gpu, min_interval_s=args.min_interval
    )
    port = metrics.serve("exporter")
    if port is None:
        _log.error("exporter_not_serving")
        return 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
