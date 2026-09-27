import io
import json

import pytest

from crawler2.core.configuration import Settings, WorkerRole
from crawler2.core.observability import (
    Metrics,
    bind_correlation_id,
    configure_logging,
    get_logger,
)


def _records(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_log_carries_required_fields(clean_env: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    configure_logging(Settings(host_id="host-2"), role="http", stream=stream)

    with bind_correlation_id("cid-123"):
        get_logger("frontier").info("url_claimed", url="http://example.test/")
    get_logger("frontier").info("after_block")

    first, second = _records(stream)
    assert first["event"] == "url_claimed"
    assert first["level"] == "info"
    assert first["service"] == "crawler2"
    assert first["host_id"] == "host-2"
    assert first["role"] == "http"
    assert first["component"] == "frontier"
    assert first["correlation_id"] == "cid-123"
    assert isinstance(first["timestamp"], str)
    assert first["timestamp"].endswith("Z")
    assert "correlation_id" not in second


def test_log_level_filters(clean_env: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    clean_env.setenv("CRAWLER2_LOGGING__LEVEL", "WARNING")
    configure_logging(Settings(), stream=stream)
    log = get_logger("x")
    log.info("dropped")
    log.warning("kept")
    assert [r["event"] for r in _records(stream)] == ["kept"]


def test_generated_correlation_ids_are_unique(clean_env: pytest.MonkeyPatch) -> None:
    configure_logging(Settings(), stream=io.StringIO())
    with bind_correlation_id() as a, bind_correlation_id() as b:
        assert a != b


def test_metrics_render_counters_histograms_and_identity(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(host_id="host-9", roles=[WorkerRole.HTTP, WorkerRole.TOR])
    metrics = Metrics(settings)

    fetches = metrics.counter("fetches", "Fetch attempts", labels=["outcome"])
    fetches.labels(outcome="ok").inc(3)
    assert metrics.counter("fetches", "Fetch attempts", labels=["outcome"]) is fetches
    metrics.histogram("fetch_seconds", "Fetch latency").observe(0.2)

    text = metrics.render().decode()
    assert 'crawler2_fetches_total{outcome="ok"} 3.0' in text
    assert "crawler2_fetch_seconds_bucket" in text
    assert 'host_id="host-9"' in text
    assert 'roles="http,tor"' in text


def test_metrics_instances_are_isolated(clean_env: pytest.MonkeyPatch) -> None:
    a, b = Metrics(Settings()), Metrics(Settings())
    a.counter("events", "e").inc()
    assert "crawler2_events_total" not in b.render().decode()
