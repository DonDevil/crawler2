"""Check that this process can reach Redis, ScyllaDB and MinIO.

Used as the app container health check and by the P0 stack validation.
Hosts are addressed by configuration (Compose service names in dev), never
by localhost. Exit code 0 only when every backend answered.

    crawler2-check [--wait SECONDS]
"""

from __future__ import annotations

import argparse
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

import redis
from cassandra import DriverException
from cassandra.cluster import EXEC_PROFILE_DEFAULT, Cluster, ExecutionProfile, NoHostAvailable
from cassandra.policies import DCAwareRoundRobinPolicy

from crawler2.core.configuration import Settings
from crawler2.core.observability import configure_logging, get_logger


@dataclass(frozen=True, slots=True)
class CheckResult:
    backend: str
    ok: bool
    latency_ms: float
    detail: str


def check_redis(settings: Settings) -> str:
    cfg = settings.redis
    client = redis.Redis(
        host=cfg.host,
        port=cfg.port,
        db=cfg.db,
        password=cfg.password.get_secret_value() if cfg.password else None,
        socket_timeout=cfg.socket_timeout_s,
        socket_connect_timeout=cfg.socket_timeout_s,
    )
    try:
        client.ping()
        # Redis TIME is the shared clock for leases/schedules (ADR-006).
        # redis-py types sync/async responses as one union; this client is sync.
        seconds, micros = cast(tuple[int, int], client.time())
        return f"redis_time={seconds}.{micros:06d}"
    finally:
        client.close()


def check_scylla(settings: Settings) -> str:
    cfg = settings.scylla
    cluster = Cluster(
        contact_points=cfg.contact_points,
        port=cfg.port,
        execution_profiles={
            EXEC_PROFILE_DEFAULT: ExecutionProfile(
                load_balancing_policy=DCAwareRoundRobinPolicy(local_dc=cfg.local_dc)
            )
        },
        connect_timeout=cfg.connect_timeout_s,
        protocol_version=4,
    )
    try:
        session = cluster.connect()
        row = session.execute("SELECT release_version FROM system.local").one()
        return f"release_version={row.release_version}"
    finally:
        cluster.shutdown()


def check_minio(settings: Settings) -> str:
    url = f"{settings.minio.base_url}/minio/health/ready"
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing non-http MinIO URL: {url}")
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 — scheme checked above
        return f"http_status={response.status}"


CHECKS: dict[str, Callable[[Settings], str]] = {
    "redis": check_redis,
    "scylla": check_scylla,
    "minio": check_minio,
}

# Failures we expect from an unreachable/unready backend. Anything else is a
# bug and propagates.
BACKEND_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    urllib.error.URLError,
    redis.RedisError,
    NoHostAvailable,
    DriverException,
)


def run_checks(
    settings: Settings, checks: dict[str, Callable[[Settings], str]] = CHECKS
) -> list[CheckResult]:
    results = []
    for backend, check in checks.items():
        started = time.perf_counter()
        try:
            detail, ok = check(settings), True
        except BACKEND_ERRORS as exc:
            detail, ok = f"{type(exc).__name__}: {exc}", False
        latency_ms = (time.perf_counter() - started) * 1000
        results.append(CheckResult(backend, ok, round(latency_ms, 1), detail))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wait", type=float, default=0.0, help="retry for up to SECONDS until all backends are up"
    )
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings, role="diagnostics")
    log = get_logger("diagnostics.connectivity")

    deadline = time.monotonic() + args.wait
    while True:
        results = run_checks(settings)
        if all(r.ok for r in results) or time.monotonic() >= deadline:
            break
        log.info("backends_not_ready", pending=[r.backend for r in results if not r.ok])
        time.sleep(2)

    for r in results:
        log.log(
            logging.INFO if r.ok else logging.ERROR,
            "backend_check",
            backend=r.backend,
            ok=r.ok,
            latency_ms=r.latency_ms,
            detail=r.detail,
        )
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
