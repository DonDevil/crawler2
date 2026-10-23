"""Prometheus-style metrics scaffold.

Each process owns one ``Metrics`` instance (dependency-injected, no module
global registry), so tests and components never collide. Host attribution
follows the Prometheus info-metric idiom: one ``<ns>_process_info`` series
carries host_id/service/roles and is joined on scrape target, instead of
repeating a host_id label on every series.
"""

from __future__ import annotations

from collections.abc import Sequence

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, Info, generate_latest
from prometheus_client import start_http_server as _start_http_server

from crawler2.core.configuration.settings import Settings
from crawler2.core.observability.logging import get_logger

_log = get_logger("core.metrics")

ROLE_PORT_OFFSETS: dict[str, int] = {
    "worker-http": 1,
    "worker-browser": 2,
    "worker-tor": 3,
    "extract": 4,
    "relay": 5,
    "admit": 6,
    "resolve": 7,
    "probe": 8,
    "hash": 9,
    "representations": 10,
    "intel": 11,
    "feedback": 12,
    "evidence-collect": 13,
    "exporter": 14,
}
"""Per-role offsets from ``metrics.port`` (see docs/phases/p14-production-hardening/)."""

DEFAULT_LATENCY_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)


class Metrics:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings.metrics
        self.registry = CollectorRegistry(auto_describe=True)
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._gauges: dict[str, Gauge] = {}
        Info(
            "process",
            "Identity of the process exporting these metrics",
            namespace=self._settings.namespace,
            registry=self.registry,
        ).info(
            {
                "service": settings.service,
                "host_id": settings.host_id,
                "environment": settings.environment.value,
                "roles": ",".join(role.value for role in settings.roles),
            }
        )

    def counter(self, name: str, documentation: str, labels: Sequence[str] = ()) -> Counter:
        if name not in self._counters:
            self._counters[name] = Counter(
                name,
                documentation,
                labelnames=tuple(labels),
                namespace=self._settings.namespace,
                registry=self.registry,
            )
        return self._counters[name]

    def gauge(self, name: str, documentation: str, labels: Sequence[str] = ()) -> Gauge:
        if name not in self._gauges:
            self._gauges[name] = Gauge(
                name,
                documentation,
                labelnames=tuple(labels),
                namespace=self._settings.namespace,
                registry=self.registry,
            )
        return self._gauges[name]

    def histogram(
        self,
        name: str,
        documentation: str,
        labels: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_LATENCY_BUCKETS,
    ) -> Histogram:
        if name not in self._histograms:
            self._histograms[name] = Histogram(
                name,
                documentation,
                labelnames=tuple(labels),
                namespace=self._settings.namespace,
                registry=self.registry,
                buckets=tuple(buckets),
            )
        return self._histograms[name]

    def render(self) -> bytes:
        """Prometheus text exposition of every registered series."""
        return generate_latest(self.registry)

    def serve(self, role: str | None = None) -> int | None:
        """Expose /metrics over HTTP; returns the port, or None when disabled or unbindable.

        ``role`` selects a fixed offset from ``metrics.port`` (``ROLE_PORT_OFFSETS``) so the
        long-running processes of one host never collide; ``None`` keeps the base port.
        A bind failure is logged and does not stop the process: losing a metrics port must
        not turn into lost work (the missing scrape target alerts instead).
        """
        if not self._settings.enabled:
            return None
        port = self._settings.port + ROLE_PORT_OFFSETS.get(role or "", 0)
        try:
            _start_http_server(port, addr=self._settings.bind, registry=self.registry)
        except OSError as exc:
            _log.error("metrics_bind_failed", role=role, port=port, error=str(exc)[:160])
            return None
        _log.info("metrics_serving", role=role, port=port, bind=self._settings.bind)
        return port
