"""Summarise an M1 run (Gate F/G): resources, leaks, volumes, V1-comparable metrics.

Reads ``var/p6-m1/samples.jsonl`` (monitor) and ``restarts.log`` from the
Gate G window start (``var/p6-m1/window_start``, written by ``run.sh start``;
``--since`` overrides) on. Fetch metrics come from the monitor's per-sample
``fetch.completed`` tallies; ``--scylla`` adds the W2 scan of every admitted
domain (slow; times out while the HDD compacts, X-12). A "leak" is judged
numerically: the least-squares slope of each resource over the last
``--window-h`` hours, expressed per hour and relative to the mean.

    env/bin/python benchmarks/p6-m1/report.py --window-h 24 \
        --out benchmarks/p6-m1/results/m1-run2.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from itertools import pairwise
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
            if name.partition("{")[0].endswith("_created"):  # a timestamp, not a count
                continue
            first.setdefault(name, value)
            last[name] = value
    return {k: last[k] - first.get(k, 0.0) for k in sorted(last)}


def _lag(sample: dict[str, Any], stream: str, group: str) -> int:
    groups = sample.get("streams", {}).get(stream, {}).get("groups", {})
    return (groups.get(group) or {}).get("lag") or 0


def fetch_tallies(samples: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any]:
    """Sums the monitor's fetch.completed tallies (V1-comparable fetch metrics)."""
    tally: dict[str, Counter[str]] = {
        k: Counter() for k in ("outcome", "capability", "status", "detail")
    }
    attempts = missed = 0
    for s in samples:
        f = s.get("fetch")
        if not f:
            continue
        attempts += f["attempts"]
        missed += f["missed"]
        for k, counter in tally.items():
            counter.update(f[k])
    if not attempts:
        return {"attempts": 0}
    responses = tally["outcome"]["response"]
    ok_2xx = tally["status"]["2xx"]
    return {
        "attempts": attempts,
        "missed_by_monitor": missed,
        "attempts_per_s": round(attempts / (end - start).total_seconds(), 3),
        "outcomes": dict(tally["outcome"].most_common()),
        "capability": dict(tally["capability"].most_common()),
        "http_status": dict(tally["status"].most_common(30)),
        "detail_top": dict(tally["detail"].most_common(20)),
        "response_rate": round(responses / attempts, 4),
        "response_2xx_rate": round(ok_2xx / attempts, 4),
        # V1 counted every received response as a success, 403/404 included (P4)
        "v1_comparable_success_rate": round(
            (responses + tally["outcome"]["blocked"]) / attempts, 4
        ),
        "browser_attempt_share": round(tally["capability"]["browser"] / attempts, 4),
    }


def sampling_gaps(samples: list[dict[str, Any]], over_s: float = 300) -> list[dict[str, Any]]:
    gaps = []
    for a, b in pairwise(samples):
        t0, t1 = datetime.fromisoformat(a["at"]), datetime.fromisoformat(b["at"])
        if (t1 - t0).total_seconds() > over_s:
            gaps.append({"from": a["at"], "to": b["at"], "s": (t1 - t0).total_seconds()})
    return gaps


def hourly(samples: list[dict[str, Any]], counter: str) -> list[float]:
    """Per-hour deltas of a frontier counter across the window."""
    out = []
    start = datetime.fromisoformat(samples[0]["at"])
    base = samples[0].get("frontier", {}).get("counters", {}).get(counter)
    hour = 1
    for s in samples:
        at = datetime.fromisoformat(s["at"])
        value = s.get("frontier", {}).get("counters", {}).get(counter)
        if value is None or base is None:
            continue
        if at - start >= timedelta(hours=hour):
            out.append(value - base)
            base = value
            hour += 1
    return out


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
    parser.add_argument("--since", help="window start (ISO); default var/p6-m1/window_start")
    parser.add_argument("--scylla", action="store_true", help="also scan W2 fetch rows")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    since_file = VAR / "window_start"
    since = args.since or (since_file.read_text().strip() if since_file.exists() else None)
    samples = [json.loads(line) for line in (VAR / "samples.jsonl").read_text().splitlines()]
    if since:
        cut = datetime.fromisoformat(since.replace("Z", "+00:00"))
        samples = [s for s in samples if datetime.fromisoformat(s["at"]) >= cut]
    start = datetime.fromisoformat(samples[0]["at"])
    end = datetime.fromisoformat(samples[-1]["at"])
    restarts = (VAR / "restarts.log").read_text().splitlines()
    exits = [r for r in restarts if " exit " in r and r.split()[0] >= samples[0]["at"][:19]]
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
        "window_start": since,
        "samples": len(samples),
        "sampling_gaps_over_5min": sampling_gaps(samples),
        "exits_by_process": dict(Counter(r.split()[2] for r in exits)),
        "completions_per_hour": hourly(samples, "completed"),
        "fetch": fetch_tallies(samples, start, end),
        "host_iowait_pct": trend(samples, ["host", "iowait_pct"], 1e9),
        "monitor_errors": Counter(k for s in samples for k in s.get("errors", {})),
        "config": (VAR / "config.env").read_text().splitlines(),
        "process_exits": exits,
        "starts": samples[-1].get("starts"),
        "resources_last_window": resources,
        "redis_used_memory_mb": trend(samples, ["redis", "used_memory_mb"], args.window_h),
        "redis_clients": trend(samples, ["redis", "clients"], args.window_h),
        "frontier_depth_http": trend(samples, ["frontier", "depth", "http"], 1e9),
        "frontier_depth_browser": trend(samples, ["frontier", "depth", "browser"], 1e9),
        "frontier_dead_letters_last": samples[-1].get("frontier", {}).get("dead_letters"),
        "frontier_counters_last": samples[-1].get("frontier", {}).get("counters"),
        "stream_lag_max": {
            f"{name}/{group}": max(_lag(s, name, group) for s in samples)
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
    if args.scylla:
        result["fetch_scylla"] = fetch_metrics(Settings(), start, end)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
    keys = ("window_start", "duration_h", "samples", "sampling_gaps_over_5min", "exits_by_process")
    print(json.dumps({k: result[k] for k in keys}, indent=1, default=str))


if __name__ == "__main__":
    main()
