"""M1 resource and health monitor: one JSON sample per interval (design §21, Gate G).

Samples every M1 process (RSS, open file descriptors, threads, CPU time,
and for the browser pool its whole Chromium process tree), restarts from
the supervisor log, Redis memory/clients, the frontier's P3 stats (``audit()`` is
offline-only in P3, so it is never run against the live frontier), every M1
stream's length and per-group pending/lag, Prometheus counters of the relay
and the admission service, host CPU iowait / load / disk I/O, and every ten
minutes the on-disk size of the Scylla keyspace and the MinIO bucket
(measured in a background thread, so a slow ``du`` under I/O load never
delays a sample).

Every sample also tallies the ``fetch.completed`` entries published since
the previous sample (outcome, capability, HTTP status class, ``detail``):
the V1-comparable fetch metrics, read from the stream instead of a Scylla
scan that times out while the HDD compacts (X-12). The stream position is
kept in ``fetch.cursor``, so a monitor restart continues where it stopped;
if the stream was trimmed past it, the sample says so (``fetch.gap``).

A failing probe is recorded in ``errors`` and does not stop the monitor.
Writes ``var/p6-m1/samples.jsonl`` (git-ignored). Environment as run.sh.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
import urllib.request
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from crawler2.core.configuration import MinioSettings, Settings
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
        # prometheus_client exports a *_created series (creation timestamp)
        # next to every counter; it is not a count.
        if name.partition("{")[0].endswith("_created"):
            continue
        out[name] = float(value)
    return out


def docker(cmd: str, env: dict[str, str] | None = None) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 -- fixed command, operator tool
            ["sg", "docker", "-c", cmd],  # noqa: S607 -- sg/docker from PATH, as run.sh
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, **(env or {})},
        )
    except subprocess.SubprocessError:
        return None
    return out.stdout


def du(container: str, path: str) -> int | None:
    try:
        return int((docker(f"docker exec {container} du -sb {path}") or "").split()[0])
    except (ValueError, IndexError):
        return None


def bucket_usage(settings: MinioSettings) -> dict[str, int] | None:
    """MinIO's own bucket usage (its scanner's cache; may lag by minutes).

    ``du`` over the bucket walks every object on the HDD: > 7 min and extra
    I/O load at 57k objects, so it is not used for MinIO.
    """
    if settings.access_key is None or settings.secret_key is None:
        return None
    # the credential travels in the environment, never on a command line
    url = (
        f"http://{settings.access_key.get_secret_value()}:"
        f"{settings.secret_key.get_secret_value()}@127.0.0.1:9000"
    )
    text = docker(
        "docker exec -e MC_HOST_m1 crawler2-minio-1 mc admin prometheus metrics m1 bucket",
        {"MC_HOST_m1": url},
    )
    out = {}
    for line in (text or "").splitlines():
        for metric, key in (
            ("minio_bucket_usage_total_bytes", "bytes"),
            ("minio_bucket_usage_object_total", "objects"),
        ):
            if line.startswith(f'{metric}{{bucket="{settings.bucket_raw}"'):
                out[key] = int(float(line.rpartition(" ")[2]))
    return out or None


class DiskSizes:
    """Measures the dataset sizes in a background thread; samples take the latest result."""

    def __init__(self, keyspace: str, minio: MinioSettings) -> None:
        self._keyspace = keyspace
        self._minio = minio
        self._latest: dict[str, Any] | None = None
        self._running = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._measure, daemon=True).start()

    def take(self) -> dict[str, Any] | None:
        with self._lock:
            latest, self._latest = self._latest, None
        return latest

    def _measure(self) -> None:
        started = time.monotonic()
        result = {
            "scylla_keyspace_bytes": du(
                "crawler2-scylla-1", f"/var/lib/scylla/data/{self._keyspace}"
            ),
            "minio_bucket": bucket_usage(self._minio),
            "host_free_gb": round(psutil.disk_usage("/").free / 2**30, 1),
            "measure_s": round(time.monotonic() - started, 1),
        }
        with self._lock:
            self._latest = result
            self._running = False


class FetchTally:
    """Counts the fetch.completed entries published since the last call.

    ``missed`` = entries added to the stream since the previous call minus
    the entries counted: non-zero only if retention trimmed entries before
    the monitor read them.
    """

    def __init__(self, redis: Any, stream: str, cursor_file: Path) -> None:
        self._redis = redis
        self._stream = stream
        self._cursor_file = cursor_file
        if cursor_file.exists():  # restart: continue where the last monitor stopped
            self._cursor, added = cursor_file.read_text().split()
            self._added = int(added)
        else:  # first start: count from now on
            info = redis.xinfo_stream(stream)
            self._cursor = info["last-generated-id"]
            self._added = info["entries-added"]
            self._save()

    def _save(self) -> None:
        self._cursor_file.write_text(f"{self._cursor} {self._added}\n")

    def take(self) -> dict[str, Any]:
        added = self._redis.xinfo_stream(self._stream)["entries-added"]
        outcome: Counter[str] = Counter()
        capability: Counter[str] = Counter()
        status: Counter[str] = Counter()
        detail: Counter[str] = Counter()
        n = 0
        while True:
            batch = self._redis.xrange(self._stream, f"({self._cursor}", "+", count=1000)
            if not batch:
                break
            for entry_id, fields in batch:
                self._cursor = entry_id
                n += 1
                try:
                    attempt = json.loads(fields["envelope"])["payload"]["attempt"]
                except (KeyError, ValueError):
                    outcome["unparsable"] += 1
                    continue
                outcome[attempt["outcome"]] += 1
                capability[attempt["capability"]] += 1
                if attempt.get("http_status") is not None:
                    status[f"{attempt['http_status'] // 100}xx"] += 1
                    status[str(attempt["http_status"])] += 1
                detail[(attempt.get("detail") or "")[:40]] += 1
        # entries added while reading are counted now and subtracted next time
        missed = max(0, added - self._added - n)
        self._added += n + missed
        self._save()
        return {
            "attempts": n,
            "missed": missed,
            "outcome": dict(outcome),
            "capability": dict(capability),
            "status": dict(status),
            "detail": dict(detail),
        }


def host() -> dict[str, Any]:
    cpu = psutil.cpu_times_percent(interval=None)
    out: dict[str, Any] = {
        "iowait_pct": cpu.iowait,
        "user_pct": cpu.user,
        "system_pct": cpu.system,
        "load_1m": round(psutil.getloadavg()[0], 2),
        "mem_available_mb": round(psutil.virtual_memory().available / 2**20),
        "swap_used_mb": round(psutil.swap_memory().used / 2**20),
    }
    disks = psutil.disk_io_counters(perdisk=True)
    if "sda" in disks:
        d = disks["sda"]
        out["sda"] = {
            "read_mb": round(d.read_bytes / 2**20),
            "write_mb": round(d.write_bytes / 2**20),
            "busy_ms": getattr(d, "busy_time", None),
        }
    return out


def probe(sample: dict[str, Any], key: str, fn: Callable[[], Any]) -> None:
    try:
        sample[key] = fn()
    except Exception as exc:  # noqa: BLE001 -- one failing probe must not stop the monitor
        sample.setdefault("errors", {})[key] = f"{type(exc).__name__}: {exc}"[:300]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args()
    settings = Settings()
    redis = connect_redis(settings.redis)
    frontier = RedisFrontier(redis, settings.frontier, namespace=settings.redis.namespace)
    prefix = settings.events.stream_prefix
    fetches = FetchTally(redis, f"{prefix}fetch.completed.v1", VAR / "fetch.cursor")
    disk = DiskSizes(settings.scylla.keyspace, settings.minio)
    psutil.cpu_times_percent(interval=None)  # prime: the first call has no interval
    last_disk = 0.0
    while True:
        started = time.monotonic()
        sample: dict[str, Any] = {"at": datetime.now(UTC).isoformat()}
        sample["processes"] = {
            f.stem: process(int(f.read_text()))
            for f in sorted((VAR / "pids").glob("*.child"))
            if f.stem != "monitor"
        }
        probe(sample, "starts", _starts)
        probe(sample, "host", host)
        probe(sample, "redis", lambda: _redis(redis))
        probe(sample, "frontier", lambda: _frontier(frontier))
        probe(sample, "streams", lambda: _streams(redis, prefix))
        probe(sample, "fetch", fetches.take)
        sample["metrics"] = {name: scrape(port) for name, port in METRICS.items()}
        measured = disk.take()
        if measured is not None:
            sample["disk"] = measured
        if time.monotonic() - last_disk > 600:
            disk.start()
            last_disk = time.monotonic()
        with (VAR / "samples.jsonl").open("a") as out:
            out.write(json.dumps(sample, sort_keys=True) + "\n")
        time.sleep(max(1.0, args.interval - (time.monotonic() - started)))


def _starts() -> dict[str, int]:
    log = VAR / "restarts.log"
    if not log.exists():
        return {}
    starts = [line.split()[2] for line in log.read_text().splitlines() if " start " in line]
    return {name: starts.count(name) for name in sorted(set(starts))}


def _redis(redis: Any) -> dict[str, Any]:
    return {
        "used_memory_mb": round(redis.info("memory")["used_memory"] / 2**20, 1),
        "clients": redis.info("clients")["connected_clients"],
    }


def _frontier(frontier: RedisFrontier) -> dict[str, Any]:
    stats = frontier.stats()
    return {
        "depth": {q.value: n for q, n in stats.depth.items()},
        "eligible_domains": {q.value: n for q, n in stats.eligible_domains.items()},
        "scheduled": stats.scheduled,
        "leased": stats.leased,
        "dead_letters": stats.dead_letters,
        "saturated_domains": stats.saturated_domains,
        "counters": stats.counters,
    }


def _streams(redis: Any, prefix: str) -> dict[str, Any]:
    streams = {}
    for key in sorted(redis.scan_iter(match=f"{prefix}*")):
        groups = {
            g["name"]: {
                "pending": g["pending"],
                "lag": g.get("lag"),
                "entries_read": g.get("entries-read"),
            }
            for g in redis.xinfo_groups(key)
        }
        info = redis.xinfo_stream(key)
        streams[key.removeprefix(prefix)] = {
            "length": info["length"],
            "entries_added": info.get("entries-added"),
            "groups": groups,
        }
    return streams


if __name__ == "__main__":
    main()
