"""The pipeline exporter: real SLI series when sources answer, absent (never zero) when not."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from prometheus_client.exposition import generate_latest
from prometheus_client.registry import CollectorRegistry

from crawler2.core.configuration import ExecutionQueue, Settings
from crawler2.diagnostics.exporter import PipelineCollector
from crawler2.storage.events.publisher import stream_name


class FakeRedis:
    def __init__(self, *, down: bool = False, now_ms: int = 2_000_000) -> None:
        self.down, self.now_ms = down, now_ms

    def info(self) -> dict[str, Any]:
        if self.down:
            raise ConnectionError("redis down")
        return {"used_memory": 500, "maxmemory": 1000, "evicted_keys": 0}

    def xlen(self, key: str) -> int:
        if "page.observed" in key:
            return 42
        raise KeyError(key)

    def xinfo_groups(self, key: str) -> list[dict[str, Any]]:
        return [{"name": "extraction", "pending": 3, "lag": 7}]

    def xpending(self, key: str, group: str) -> dict[str, Any]:
        return {"min": f"{self.now_ms - 90_000}-0"}


class FakeFrontier:
    def stats(self) -> Any:
        return SimpleNamespace(
            depth={ExecutionQueue.HTTP: 11, ExecutionQueue.BROWSER: 2},
            eligible_domains={ExecutionQueue.HTTP: 5, ExecutionQueue.BROWSER: 1},
            scheduled=4,
            leased=3,
            gated_domains=2,
            saturated_domains=1,
            dead_letters=6,
        )


class FakeOutbox:
    def checkpoint(self, shard: int) -> int | None:
        return None if shard else (2_000 - 5) // 60 * 1  # 5 s behind, in minute buckets


def render(c: PipelineCollector) -> str:
    reg = CollectorRegistry()
    reg.register(c)
    return generate_latest(reg).decode()


def collector(**kw: Any) -> PipelineCollector:
    defaults: dict[str, Any] = {
        "redis_client": FakeRedis(),
        "frontier": FakeFrontier(),
        "outbox": FakeOutbox(),
        "clock": lambda: 2000.0,
        "min_interval_s": 15.0,
    }
    defaults.update(kw)
    return PipelineCollector(Settings(), **defaults)


def test_exposes_queue_lag_and_backlog_series() -> None:
    text = render(collector())
    assert 'crawler2_stream_length{event_type="page.observed"} 42.0' in text
    assert (
        'crawler2_stream_group_pending{event_type="page.observed",group="extraction"} 3.0' in text
    )
    assert 'crawler2_stream_group_lag{event_type="page.observed",group="extraction"} 7.0' in text
    age = "crawler2_stream_group_oldest_pending_age_seconds"
    assert f'{age}{{event_type="page.observed",group="extraction"}} 90.0' in text
    assert 'crawler2_frontier_depth{queue="http"} 11.0' in text
    assert "crawler2_frontier_dead_letters 6.0" in text
    assert "crawler2_redis_memory_ratio 0.5" in text
    assert 'crawler2_exporter_source_up{source="redis"} 1.0' in text
    assert "crawler2_outbox_checkpoint_lag_seconds" in text
    assert 'event_type="fetch.completed"' not in text, "streams that do not exist are skipped"


def test_unreadable_sources_are_flagged_and_their_series_are_absent() -> None:
    text = render(
        collector(redis_client=FakeRedis(down=True), frontier=SimpleNamespace(stats=lambda: 1 / 0))
    )
    assert 'crawler2_exporter_source_up{source="redis"} 0.0' in text
    assert 'crawler2_exporter_source_up{source="frontier"} 0.0' in text
    assert "crawler2_stream_length" not in text
    assert "crawler2_frontier_depth" not in text
    assert "crawler2_redis_memory_used_bytes" not in text


def test_scrapes_inside_the_interval_do_not_touch_the_services_again() -> None:
    redis = FakeRedis()
    calls = {"n": 0}
    orig = redis.info

    def counting() -> dict[str, Any]:
        calls["n"] += 1
        return orig()

    redis.info = counting  # type: ignore[method-assign]
    now = [2000.0]
    c = collector(redis_client=redis, clock=lambda: now[0])
    render(c), render(c)
    assert calls["n"] == 1
    now[0] += 16
    render(c)
    assert calls["n"] == 2


def test_gpu_series_only_when_enabled_and_readable() -> None:
    assert "gpu_" not in render(collector())
    on = render(collector(gpu=True, gpu_reader=lambda: [37.0, 1024.0, 4096.0]))
    assert "crawler2_gpu_utilization_percent 37.0" in on
    assert "crawler2_gpu_memory_total_bytes 4.294967296e+09" in on
    off = render(collector(gpu=True, gpu_reader=lambda: None))
    assert 'crawler2_exporter_source_up{source="gpu"} 0.0' in off
    assert "crawler2_gpu_utilization_percent" not in off


def test_stream_names_match_the_publisher() -> None:
    assert stream_name(Settings().events, "page.observed", 1) in ("events:page.observed.v1",)
