"""Shared harness for the P3 frontier benchmarks.

Mirrors V1 `tests/benchmarks/common.py` where it matters for comparability:
isolated namespace per run, run-id'd synthetic domains, Redis CPU measured
as a time-normalised delta of `used_cpu_sys + used_cpu_user` (never as a
raw cumulative counter), results written as JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import redis
from antipiracy_contracts.models.web import UrlRef

from crawler2.core.configuration import ExecutionQueue, FrontierSettings, RedisSettings
from crawler2.frontier import Admission
from crawler2.frontier.redis import RedisFrontier, connect_redis

RESULTS = Path(__file__).resolve().parent / "results"


def add_redis_args(p: argparse.ArgumentParser, *, port: int = 16380) -> None:
    p.add_argument("--redis-host", default="127.0.0.1")
    p.add_argument("--redis-port", type=int, default=port)
    p.add_argument("--redis-db", type=int, default=2)
    p.add_argument("--label", default="", help="free text stored with the result")
    p.add_argument("--output", default=None, help="JSON result path (default: results/<ts>/)")


def redis_settings(args: argparse.Namespace) -> RedisSettings:
    return RedisSettings(
        host=args.redis_host, port=args.redis_port, db=args.redis_db, socket_timeout_s=30
    )


def client(args: argparse.Namespace) -> redis.Redis:
    return connect_redis(redis_settings(args))


def frontier(args: argparse.Namespace, namespace: str, **settings: Any) -> RedisFrontier:
    return RedisFrontier(client(args), FrontierSettings(**settings), namespace=namespace)


def run_id() -> str:
    return uuid.uuid4().hex[:8]


def domains(count: int, rid: str, prefix: str = "bench") -> list[str]:
    return [f"{prefix}-{rid}-{i}.example.test" for i in range(count)]


def admissions(
    total: int,
    domain_names: Sequence[str],
    *,
    queue: ExecutionQueue = ExecutionQueue.HTTP,
    priority: int | Callable[[int], int] = 50,
    start: int = 0,
) -> Iterator[Admission]:
    """Round-robin over domains, like V1 `make_synthetic_urls`."""
    for i in range(start, start + total):
        host = domain_names[i % len(domain_names)]
        pri = priority(i) if callable(priority) else priority
        yield Admission(UrlRef.of(f"https://{host}/page/{i}"), queue=queue, priority=pri)


def admit_all(f: RedisFrontier, items: Iterator[Admission], batch: int = 5000) -> int:
    accepted = 0
    chunk: list[Admission] = []
    for item in items:
        chunk.append(item)
        if len(chunk) == batch:
            accepted += sum(r.accepted for r in f.admit_many(chunk))
            chunk = []
    if chunk:
        accepted += sum(r.accepted for r in f.admit_many(chunk))
    return accepted


def redis_cpu(r: redis.Redis) -> float:
    info = r.info("cpu")
    return float(info["used_cpu_sys"]) + float(info["used_cpu_user"])


def evalsha_stats(r: redis.Redis) -> tuple[int, float]:
    stats = r.info("commandstats").get("cmdstat_evalsha", {})
    return int(stats.get("calls", 0)), float(stats.get("usec", 0))


def percentiles(samples: Sequence[float]) -> dict[str, float | None]:
    if not samples:
        return {"p50_us": None, "p95_us": None, "p99_us": None, "max_us": None}
    s = sorted(samples)

    def pick(q: float) -> float:
        return round(s[min(len(s) - 1, int(q * len(s)))] * 1e6, 1)

    return {"p50_us": pick(0.50), "p95_us": pick(0.95), "p99_us": pick(0.99), "max_us": pick(1.0)}


def environment(r: redis.Redis) -> dict[str, Any]:
    server = r.info("server")
    persistence = r.info("persistence")
    cfg = {
        k: v
        for k, v in r.config_get("*").items()
        if k in ("appendonly", "appendfsync", "save", "maxmemory", "maxmemory-policy", "io-threads")
    }
    try:
        cpu = subprocess.run(
            ["lscpu"], capture_output=True, text=True, check=False
        ).stdout.splitlines()
        model = next((line.split(":", 1)[1].strip() for line in cpu if "Model name" in line), "")
    except OSError:
        model = ""
    return {
        "host": platform.node(),
        "cpu_model": model,
        "cpu_count": os.cpu_count(),
        "python": sys.version.split()[0],
        "redis_version": server.get("redis_version"),
        "redis_config": cfg,
        "aof_enabled": persistence.get("aof_enabled"),
    }


def write(result: dict[str, Any], name: str, output: str | None) -> Path:
    path = (
        Path(output)
        if output
        else RESULTS / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) / f"{name}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result.get("summary", result), indent=2, default=str))
    print(f"-> {path}", file=sys.stderr)
    return path
