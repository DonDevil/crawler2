"""Summarize a V1 baseline run into the P0 regression-bar metrics.

Inputs (in the run directory): V1's own run report (``v1_report.json``,
from ``main.py --output``) and its log (``crawl.log``), plus the media
evidence namespace in Redis. Stdlib only, so it runs with any python3.

Per-engine numbers come from V1's per-page completion line:
``Processed (N): <url> [<status>] via <engine> chain=<e1> -> <e2>``.
Each engine in ``chain`` is one fetch attempt; an attempt succeeded when it
is the last engine of the chain and the page status is ``visited``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

PROCESSED = re.compile(
    r"Processed \(\d+\): (?P<url>\S+) \[(?P<status>\w+)\] via (?P<engine>\S+) chain=(?P<chain>.*)$"
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
BROWSER_ENGINES = {"playwright", "selenium", "scrapling"}


def parse_log(path: Path) -> dict[str, object]:
    attempts: Counter[str] = Counter()
    successes: Counter[str] = Counter()
    final_engine_visited: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for line in path.read_text(errors="replace").splitlines():
        m = PROCESSED.search(ANSI.sub("", line))
        if not m:
            continue
        chain = [e.strip() for e in m["chain"].split("->") if e.strip()]
        statuses[m["status"]] += 1
        attempts.update(chain)
        if m["status"] == "visited" and chain:
            successes[chain[-1]] += 1
            final_engine_visited[chain[-1]] += 1
    visited = statuses["visited"]
    total_attempts = sum(attempts.values())
    browser_visited = sum(n for e, n in final_engine_visited.items() if e in BROWSER_ENGINES)
    browser_attempts = sum(n for e, n in attempts.items() if e in BROWSER_ENGINES)
    return {
        "pages_processed": sum(statuses.values()),
        "status_counts": dict(statuses),
        "engine_attempts": dict(attempts),
        "engine_successes": dict(successes),
        "engine_success_rate": {e: round(successes[e] / n, 4) for e, n in attempts.items()},
        "visited_pages_by_final_engine": dict(final_engine_visited),
        "browser_share_of_visited_pages": round(browser_visited / visited, 4) if visited else None,
        "browser_share_of_fetch_attempts": (
            round(browser_attempts / total_attempts, 4) if total_attempts else None
        ),
    }


def parse_time(path: Path) -> dict[str, float]:
    """GNU ``/usr/bin/time -v`` output: whole-run CPU and peak RSS."""
    fields = {}
    for line in path.read_text().splitlines():
        key, _, value = line.strip().rpartition(": ")
        fields[key] = value
    cpu_s = float(fields["User time (seconds)"]) + float(fields["System time (seconds)"])
    wall_s = 0.0
    for part in fields["Elapsed (wall clock) time (h:mm:ss or m:ss)"].split(":"):
        wall_s = wall_s * 60 + float(part)
    return {
        "process_wall_seconds": round(wall_s, 1),
        "cpu_seconds_total": round(cpu_s, 1),
        "max_rss_mb_largest_process": round(
            int(fields["Maximum resident set size (kbytes)"]) / 1024, 1
        ),
    }


def zcard(port: int, db: int, key: str) -> int:
    out = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["redis-cli", "-p", str(port), "-n", str(db), "ZCARD", key],  # noqa: S607 — PATH tool
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--redis-port", type=int, default=16379)
    parser.add_argument("--redis-db", type=int, default=2)
    parser.add_argument("--namespace", default="bench_v1_baseline")
    args = parser.parse_args()

    report = json.loads((args.run_dir / "v1_report.json").read_text())
    log = parse_log(args.run_dir / "crawl.log")
    timing = parse_time(args.run_dir / "time.txt")
    media_assets = zcard(args.redis_port, args.redis_db, f"{args.namespace}_evidence:assets:all")

    duration = report["timing"]["duration_seconds"]
    local = report["local_work"]
    visited = local["visited"]
    res = report.get("resources", {})
    summary = {
        "duration_seconds": round(duration, 1),
        "workers": report["metadata"]["worker_count"],
        "pages_visited": visited,
        "pages_processed": local["processed"],
        "pages_per_sec": round(visited / duration, 3),
        "processed_per_sec": round(local["processed"] / duration, 3),
        "fetch_success_rate_overall": round(visited / local["processed"], 4)
        if local["processed"]
        else None,
        **log,
        "bytes_per_page": None,
        "bytes_per_page_note": (
            "not measurable: V1 fetch() returns only (html, error) and logs no sizes (plan A.2 D1)"
        ),
        # Whole run incl. reaped children (browsers), from /usr/bin/time.
        **timing,
        "cpu_percent_avg_whole_run": round(
            100 * timing["cpu_seconds_total"] / timing["process_wall_seconds"], 1
        ),
        # V1 ResourceMonitor: main crawler process only, sampled.
        "v1_monitor_process_cpu_percent_avg": res.get("process_cpu_percent_avg"),
        "v1_monitor_process_rss_mb_avg": res.get("process_rss_mb_avg"),
        "v1_monitor_process_rss_mb_peak": res.get("process_rss_mb_peak"),
        "resources_raw": res,
        "media_assets_discovered": media_assets,
        "media_per_1000_pages": round(1000 * media_assets / visited, 2) if visited else None,
        "discovered_urls": report["this_run"]["discovered_unique"],
    }
    (args.run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "resources_raw"}, indent=2))


if __name__ == "__main__":
    main()
