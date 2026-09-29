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

    def serve(self) -> None:
        """Expose /metrics over HTTP on the configured port (no-op when disabled)."""
        if self._settings.enabled:
            _start_http_server(self._settings.port, registry=self.registry)
