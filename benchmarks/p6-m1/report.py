"""Summarise an M1 run (Gate F/G): resources, leaks, volumes, V1-comparable metrics.

Reads ``var/p6-m1/samples.jsonl`` (monitor) and ``restarts.log``, and the
M1 keyspace for fetch outcomes (W2 rows of every domain M1 admitted). A
"leak" is judged numerically: the least-squares slope of each resource over
the last ``--window-h`` hours, expressed per hour and relative to the mean.

    env/bin/python benchmarks/p6-m1/report.py --out benchmarks/p6-m1/results/m1-24h.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from antipiracy_contracts.ids import DomainId
from antipiracy_contracts.models.web import FetchCapability, FetchOutcome, UrlRef

from crawler2.core.configuration import Settings
from crawler2.storage.scylla import ScyllaStorage
from crawler2.storage.scylla.session import Consistency

ROOT = Path(__file__).resolve().parents[2]
VAR = ROOT / "var" / "p6-m1"
TRACKED = ("rss_mb", "fds", "threads", "tree_processes", "tree_rss_mb", "connections")


def slope_per_hour(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 3:
        return None
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    var = sum((x - mx) ** 2 for x, _ in points)
    if var == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in points) / var * 3600


def trend(samples: list[dict[str, Any]], path: list[str], window_h: float) -> dict[str, Any]:
    end = datetime.fromisoformat(samples[-1]["at"])
    points = []
    for s in samples:
        value: Any = s
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, int | float):
            at = datetime.fromisoformat(s["at"])
            if end - at <= timedelta(hours=window_h):
                points.append((at.timestamp(), float(value)))
    if not points:
        return {}
    values = [v for _, v in points]
    mean = sum(values) / len(values)
    slope = slope_per_hour(points)
    return {
        "min": min(values),
        "max": max(values),
        "mean": round(mean, 2),
        "last": values[-1],
        "slope_per_h": round(slope, 4) if slope is not None else None,
        "slope_pct_of_mean_per_h": round(100 * slope / mean, 3) if slope and mean else 0.0,
    }


def counter_delta(samples: list[dict[str, Any]], source: str) -> dict[str, float]:
    first: dict[str, float] = {}
    last: dict[str, float] = {}
    for s in samples:
        for name, value in (s.get("metrics", {}).get(source) or {}).items():
            first.setdefault(name, value)
            last[name] = value
    return {k: last[k] - first.get(k, 0.0) for k in sorted(last)}


def fetch_metrics(settings: Settings, start: datetime, end: datetime) -> dict[str, Any]:
    storage = ScyllaStorage.open(settings.scylla, instance="m1-report")
    domains: set[DomainId] = set()
    scan = storage.session.bind("SELECT url FROM {ks}.url_admission", Consistency.EVENTUAL_READ, ())
    for row in storage.session.stream(scan, fetch_size=200):
        if row.url:
            domains.add(UrlRef.of(row.url).domain_id)
    outcomes: Counter[str] = Counter()
    capability: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    attempts = 0
    day = start
    while day.date() <= end.date():
        for domain in domains:
            for a in storage.fetch_attempts.for_domain_day(domain, day):
                if not start <= a.finished_at <= end:
                    continue
                attempts += 1
                outcomes[a.outcome.value] += 1
                capability[a.capability.value] += 1
                if a.outcome is FetchOutcome.RESPONSE and a.http_status is not None:
                    statuses[f"{a.http_status // 100}xx"] += 1
        day += timedelta(days=1)
    responses = outcomes[FetchOutcome.RESPONSE.value]
    browser = capability[FetchCapability.BROWSER.value]
    return {
        "domains_admitted": len(domains),
        "attempts": attempts,
        "outcomes": dict(outcomes),
        "http_status_classes": dict(statuses),
        "response_rate": round(responses / attempts, 4) if attempts else None,
        "response_2xx_rate": round(statuses["2xx"] / attempts, 4) if attempts else None,
        "browser_attempt_share": round(browser / attempts, 4) if attempts else None,
        "attempts_per_s": round(attempts / (end - start).total_seconds(), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-h", type=float, default=12.0)
    parser.add_argument("--no-scylla", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    samples = [json.loads(line) for line in (VAR / "samples.jsonl").read_text().splitlines()]
    start = datetime.fromisoformat(samples[0]["at"])
    end = datetime.fromisoformat(samples[-1]["at"])
    restarts = (VAR / "restarts.log").read_text().splitlines()
    exits = [r for r in restarts if " exit " in r]
    processes = sorted({p for s in samples for p in s["processes"]})
    resources = {
        p: {m: trend(samples, ["processes", p, m], args.window_h) for m in TRACKED}
        for p in processes
    }
    disks = [s["disk"] for s in samples if "disk" in s]
    result = {
        "started_at": (VAR / "started_at").read_text().strip(),
        "first_sample": samples[0]["at"],
        "last_sample": samples[-1]["at"],
        "duration_h": round((end - start).total_seconds() / 3600, 2),
        "samples": len(samples),
        "config": (VAR / "config.env").read_text().splitlines(),
        "process_exits": exits,
        "starts": samples[-1].get("starts"),
        "resources_last_window": resources,
        "redis_used_memory_mb": trend(samples, ["redis", "used_memory_mb"], args.window_h),
        "redis_clients": trend(samples, ["redis", "clients"], args.window_h),
        "frontier_depth_http": trend(samples, ["frontier", "depth", "http"], 1e9),
        "frontier_depth_browser": trend(samples, ["frontier", "depth", "browser"], 1e9),
        "frontier_dead_letters_last": samples[-1]["frontier"]["dead_letters"],
        "frontier_counters_last": samples[-1]["frontier"]["counters"],
        "frontier_audit_problems": [
            s["frontier"]["audit_problems"] for s in samples if "audit_problems" in s["frontier"]
        ],
        "stream_lag_max": {
            f"{name}/{group}": max(
                (
                    (s["streams"].get(name, {}).get("groups", {}).get(group, {}) or {}).get("lag")
                    or 0
                )
                for s in samples
            )
            for name in samples[-1]["streams"]
            for group in samples[-1]["streams"][name]["groups"]
        },
        "stream_pending_last": {
            f"{name}/{group}": g["pending"]
            for name, st in samples[-1]["streams"].items()
            for group, g in st["groups"].items()
        },
        "events_published": counter_delta(samples, "relay"),
        "discovery_metrics": counter_delta(samples, "admit"),
        "disk_first": disks[0] if disks else None,
        "disk_last": disks[-1] if disks else None,
    }
    if not args.no_scylla:
        result["fetch"] = fetch_metrics(Settings(), start, end)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps({k: result[k] for k in ("duration_h", "samples", "process_exits")}, indent=1))


if __name__ == "__main__":
    main()
