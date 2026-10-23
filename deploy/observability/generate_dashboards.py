"""Grafana dashboards as code: ``python deploy/observability/generate_dashboards.py`` rewrites
``grafana/dashboards/*.json``; a unit test fails if the committed JSON differs from this
generator or if a panel queries a ``crawler2_*`` series no code produces.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent / "grafana" / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}


def panel(
    pid: int,
    title: str,
    exprs: list[tuple[str, str]],
    x: int,
    y: int,
    *,
    unit: str = "short",
    kind: str = "timeseries",
    w: int = 12,
    h: int = 8,
    desc: str = "",
) -> dict[str, Any]:
    return {
        "id": pid,
        "type": kind,
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [
            {"refId": chr(65 + i), "expr": e, "legendFormat": legend, "datasource": DS}
            for i, (e, legend) in enumerate(exprs)
        ],
    }


def dashboard(uid: str, title: str, panels: list[dict[str, Any]], desc: str) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "description": desc,
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "tags": ["crawler2"],
        "panels": panels,
        "templating": {"list": []},
    }


def pipeline() -> dict[str, Any]:
    p = [
        panel(
            1,
            "Frontier depth per queue",
            [("crawler2_frontier_depth", "{{queue}}")],
            0,
            0,
            desc="Tasks waiting. A queue that only grows while attempts/s is flat is a stalled crawl.",
        ),
        panel(
            2,
            "Fetch attempts per second by outcome",
            [("sum by (outcome) (rate(crawler2_fetch_attempts_total[5m]))", "{{outcome}}")],
            12,
            0,
            unit="ops",
        ),
        panel(
            3,
            "Event lag (entries not yet delivered)",
            [("crawler2_stream_group_lag", "{{event_type}} / {{group}}")],
            0,
            8,
            desc="Per consumer group. Growth over 30 min alerts (EventLagGrowing).",
        ),
        panel(
            4,
            "Oldest unacknowledged entry age",
            [("crawler2_stream_group_oldest_pending_age_seconds", "{{event_type}} / {{group}}")],
            12,
            8,
            unit="s",
            desc="A consumer that reads but never acknowledges. Above 600 s for 10 min alerts (ConsumerStalled).",
        ),
        panel(
            5,
            "Outbox relay checkpoint lag",
            [("crawler2_outbox_checkpoint_lag_seconds", "max over shards")],
            0,
            16,
            unit="s",
            desc="Includes the settle window. Above 900 s alerts (OutboxRelayBehind).",
        ),
        panel(6, "Dead letters", [("crawler2_frontier_dead_letters", "dead letters")], 12, 16),
        panel(
            7,
            "Stream length (retained entries)",
            [("crawler2_stream_length", "{{event_type}}")],
            0,
            24,
        ),
        panel(
            8,
            "Exporter sources",
            [("crawler2_exporter_source_up", "{{source}}")],
            12,
            24,
            kind="stat",
            h=8,
            desc="0 = the exporter could not read that source; its series are absent, not zero.",
        ),
    ]
    return dashboard(
        "c2-pipeline", "crawler2 / Pipeline", p, "Queues, event lag, outbox and dead letters."
    )


def workers() -> dict[str, Any]:
    p = [
        panel(
            1,
            "Attempts per second by pool",
            [("sum by (pool) (rate(crawler2_fetch_attempts_total[5m]))", "{{pool}}")],
            0,
            0,
            unit="ops",
        ),
        panel(
            2,
            "Attempt duration p50 / p95",
            [
                (
                    "histogram_quantile(0.5, sum by (le, pool) (rate(crawler2_fetch_duration_seconds_bucket[5m])))",
                    "p50 {{pool}}",
                ),
                (
                    "histogram_quantile(0.95, sum by (le, pool) (rate(crawler2_fetch_duration_seconds_bucket[5m])))",
                    "p95 {{pool}}",
                ),
            ],
            12,
            0,
            unit="s",
        ),
        panel(3, "Active attempts", [("crawler2_worker_active_attempts", "{{pool}}")], 0, 8),
        panel(
            4,
            "Rate-limited responses (domain back-off, X-14)",
            [("sum by (pool) (rate(crawler2_fetch_rate_limited_total[15m]))", "{{pool}}")],
            12,
            8,
            unit="ops",
        ),
        panel(
            5,
            "Refused destinations (SSRF guard)",
            [
                (
                    'sum(increase(crawler2_fetch_attempts_total{outcome="unsafe_destination"}[1h]))',
                    "page fetch",
                ),
                ("sum(increase(crawler2_media_ssrf_rejections_total[1h]))", "media"),
            ],
            0,
            16,
        ),
        panel(
            6,
            "Lost claims / storage record errors",
            [
                (
                    "sum by (pool) (rate(crawler2_fetch_lost_claims_total[15m]))",
                    "lost claims {{pool}}",
                ),
                (
                    "sum by (pool) (rate(crawler2_fetch_record_errors_total[15m]))",
                    "record errors {{pool}}",
                ),
            ],
            12,
            16,
        ),
        panel(
            7,
            "Process restarts (1h)",
            [
                (
                    'changes(process_start_time_seconds{job=~"crawler2-.*"}[1h])',
                    "{{job}} {{instance}}",
                )
            ],
            0,
            24,
        ),
        panel(
            8,
            "Worker state",
            [("crawler2_worker_state", "{{pool}} {{state}}")],
            12,
            24,
            kind="stat",
        ),
    ]
    return dashboard(
        "c2-workers", "crawler2 / Workers", p, "Fetch pools: throughput, latency, failures, guards."
    )


def infra() -> dict[str, Any]:
    p = [
        panel(
            1,
            "Redis memory ratio",
            [("redis_memory_used_bytes / redis_memory_max_bytes", "used / maxmemory")],
            0,
            0,
            unit="percentunit",
            desc="M1 run 2 stalled when Redis filled (X-16).",
        ),
        panel(
            2,
            "Redis evictions",
            [("increase(redis_evicted_keys_total[10m])", "evicted keys")],
            12,
            0,
        ),
        panel(3, "Scylla nodes up", [('up{job="scylla"}', "{{instance}}")], 0, 8, kind="stat"),
        panel(4, "MinIO up", [('up{job="minio"}', "{{instance}}")], 12, 8, kind="stat"),
        panel(
            5,
            "Disk free fraction",
            [
                (
                    'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"}',
                    "{{instance}} {{mountpoint}}",
                )
            ],
            0,
            16,
            unit="percentunit",
        ),
        panel(
            6,
            "Host memory available",
            [("node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes", "{{instance}}")],
            12,
            16,
            unit="percentunit",
        ),
        panel(
            7,
            "GPU memory used",
            [("crawler2_gpu_memory_used_bytes / crawler2_gpu_memory_total_bytes", "{{instance}}")],
            0,
            24,
            unit="percentunit",
        ),
        panel(
            8,
            "GPU utilisation",
            [("crawler2_gpu_utilization_percent", "{{instance}}")],
            12,
            24,
            unit="percent",
        ),
        panel(
            9,
            "Object store errors",
            [
                (
                    "sum by (operation) (increase(crawler2_object_store_errors_total[15m]))",
                    "{{operation}}",
                )
            ],
            0,
            32,
        ),
        panel(
            10,
            "Scylla pending compactions (verify metric name)",
            [("sum by (instance) (scylla_compaction_manager_pending_compactions)", "{{instance}}")],
            12,
            32,
        ),
    ]
    return dashboard(
        "c2-infra", "crawler2 / Infrastructure", p, "Redis, Scylla, MinIO, hosts and GPU."
    )


DASHBOARDS = {"pipeline.json": pipeline, "workers.json": workers, "infrastructure.json": infra}


def render() -> dict[str, str]:
    return {
        name: json.dumps(fn(), indent=2, sort_keys=True) + "\n" for name, fn in DASHBOARDS.items()
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in render().items():
        (OUT / name).write_text(text)
        print(f"wrote {OUT / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
