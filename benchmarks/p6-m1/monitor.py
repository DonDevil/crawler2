"""M1 resource and health monitor: one JSON sample per interval (design §21, Gate G).

Samples every M1 process (RSS, open file descriptors, threads, CPU time,
and for the browser pool its whole Chromium process tree), restarts from
the supervisor log, Redis memory/clients, the frontier's P3 stats (``audit()`` is
offline-only in P3, so it is never run against the live frontier), every M1
stream's length and per-group pending/lag, Prometheus counters of the relay
and the admission service, and every ten
minutes the on-disk size of the Scylla keyspace and the MinIO bucket.

Writes ``var/p6-m1/samples.jsonl`` (git-ignored). Environment as run.sh.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from crawler2.core.configuration import Settings
from crawler2.frontier.redis import RedisFrontier, connect_redis

ROOT = Path(__file__).resolve().parents[2]
VAR = ROOT / "var" / "p6-m1"
METRICS = {"relay": 9301, "admit": 9302}
KEEP = (
    "crawler2_outbox_published_total",
    "crawler2_discovery_",
    "crawler2_filter_",
    "crawler2_search_",
    "crawler2_events_consumed_total",
    "crawler2_discovery_link",
)


def process(pid: int) -> dict[str, Any]:
    try:
        p = psutil.Process(pid)
        tree = [p, *p.children(recursive=True)]
        cpu = p.cpu_times()
        own = {
            "rss_mb": round(p.memory_info().rss / 2**20, 1),
            "fds": p.num_fds(),
            "threads": p.num_threads(),
            "cpu_s": round(cpu.user + cpu.system, 1),
            "tree_processes": len(tree),
            "tree_rss_mb": round(sum(_rss(c) for c in tree) / 2**20, 1),
            "connections": len(p.net_connections(kind="tcp")),
        }
        chromium = [c for c in tree[1:] if "chrom" in (_name(c) or "")]
        if chromium:
            own["chromium_processes"] = len(chromium)
        return own
    except psutil.Error as exc:
        return {"error": type(exc).__name__}


def _rss(p: psutil.Process) -> int:
    try:
        return p.memory_info().rss
    except psutil.Error:
        return 0


def _name(p: psutil.Process) -> str | None:
    try:
        return p.name()
    except psutil.Error:
        return None


def scrape(port: int) -> dict[str, float]:
    try:
        text = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(KEEP):
            continue
        name, _, value = line.rpartition(" ")
        out[name] = float(value)
    return out


def du(container: str, path: str) -> int | None:
    cmd = f"docker exec {container} du -sb {path}"
    try:
        out = subprocess.run(  # noqa: S603 -- fixed command, operator tool
            ["sg", "docker", "-c", cmd],  # noqa: S607 -- sg/docker from PATH, as run.sh
            capture_output=True,
            text=True,
            timeout=120,
        )
        return int(out.stdout.split()[0])
    except (subprocess.SubprocessError, ValueError, IndexError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args()
    settings = Settings()
    redis = connect_redis(settings.redis)
    frontier = RedisFrontier(redis, settings.frontier, namespace=settings.redis.namespace)
    prefix = settings.events.stream_prefix
    last_disk = 0.0
    while True:
        started = time.monotonic()
        sample: dict[str, Any] = {"at": datetime.now(UTC).isoformat()}
        sample["processes"] = {
            f.stem: process(int(f.read_text()))
            for f in sorted((VAR / "pids").glob("*.child"))
            if f.stem != "monitor"
        }
        log = VAR / "restarts.log"
        starts = (
            [line.split()[2] for line in log.read_text().splitlines() if " start " in line]
            if log.exists()
            else []
        )
        sample["starts"] = {name: starts.count(name) for name in sorted(set(starts))}
        info = redis.info("memory")
        sample["redis"] = {
            "used_memory_mb": round(info["used_memory"] / 2**20, 1),
            "clients": redis.info("clients")["connected_clients"],
        }
        stats = frontier.stats()
        sample["frontier"] = {
            "depth": {q.value: n for q, n in stats.depth.items()},
            "eligible_domains": {q.value: n for q, n in stats.eligible_domains.items()},
            "scheduled": stats.scheduled,
            "leased": stats.leased,
            "dead_letters": stats.dead_letters,
            "saturated_domains": stats.saturated_domains,
            "counters": stats.counters,
        }
        streams = {}
        for key in sorted(redis.scan_iter(match=f"{prefix}*")):
            groups = {
                g["name"]: {"pending": g["pending"], "lag": g.get("lag")}
                for g in redis.xinfo_groups(key)
            }
            streams[key.removeprefix(prefix)] = {"length": redis.xlen(key), "groups": groups}
        sample["streams"] = streams
        sample["metrics"] = {name: scrape(port) for name, port in METRICS.items()}
        if time.monotonic() - last_disk > 600:
            sample["disk"] = {
                "scylla_keyspace_bytes": du(
                    "crawler2-scylla-1", f"/var/lib/scylla/data/{settings.scylla.keyspace}"
                ),
                "minio_bucket_bytes": du("crawler2-minio-1", f"/data/{settings.minio.bucket_raw}"),
                "host_free_gb": round(psutil.disk_usage("/").free / 2**30, 1),
            }
            last_disk = time.monotonic()
        with (VAR / "samples.jsonl").open("a") as out:
            out.write(json.dumps(sample, sort_keys=True) + "\n")
        time.sleep(max(1.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
