"""Metric collectors. A collector returns what it could read and why it could not read the rest.

A value that could not be read is ``None`` with an error string; it is never ``0``. The
evaluator turns missing required series into INCONCLUSIVE, never into PASS.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

import psutil

from crawler2.soak.config import Collector

_SAFE = re.compile(r"[^a-z0-9_]+")


def slug(text: str) -> str:
    return _SAFE.sub("_", text.lower()).strip("_") or "x"


@dataclass
class Reading:
    values: dict[str, float | None] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    identities: dict[str, list[str]] = field(default_factory=dict)
    """label -> process identities (``pid:start``): a change between samples is a restart."""

    def fail(self, names: list[str], reason: str) -> None:
        for name in names:
            self.values[name] = None
            self.errors[name] = reason


class CollectorImpl(Protocol):
    def collect(self) -> Reading: ...


# --- host ------------------------------------------------------------------------------


class HostCollector:
    def __init__(self, options: dict[str, Any]) -> None:
        self._disks: dict[str, str] = options.get("disk_paths", {"root": "/"})

    def collect(self) -> Reading:
        r = Reading()
        r.values["cpu_percent"] = psutil.cpu_percent(interval=None)
        r.values["load1"] = os.getloadavg()[0]
        vm = psutil.virtual_memory()
        r.values["mem_available_mb"] = vm.available / 2**20
        r.values["swap_used_mb"] = psutil.swap_memory().used / 2**20
        try:
            with open("/proc/sys/fs/file-nr") as fh:
                r.values["fds_open_system"] = float(fh.read().split()[0])
        except (OSError, ValueError, IndexError) as exc:
            r.fail(["fds_open_system"], str(exc)[:100])
        for label, path in self._disks.items():
            key = slug(label)
            try:
                usage = shutil.disk_usage(path)
            except OSError as exc:
                r.fail([f"disk_{key}_free_fraction", f"disk_{key}_used_gb"], str(exc)[:100])
                continue
            r.values[f"disk_{key}_free_fraction"] = usage.free / usage.total
            r.values[f"disk_{key}_used_gb"] = usage.used / 2**30
        return r


# --- processes -------------------------------------------------------------------------


class ProcessCollector:
    """Per labelled process group: count, RSS (optionally whole tree), FDs, threads."""

    def __init__(self, options: dict[str, Any]) -> None:
        self._match: dict[str, str] = options["match"]
        self._tree: bool = bool(options.get("tree", False))

    def collect(self) -> Reading:
        r = Reading()
        found: dict[str, list[psutil.Process]] = {label: [] for label in self._match}
        for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
            cmd = " ".join(proc.info.get("cmdline") or [])
            for label, needle in self._match.items():
                if needle in cmd and proc.pid != os.getpid():
                    found[label].append(proc)
        for label, procs in found.items():
            key = slug(label)
            r.values[f"{key}_count"] = float(len(procs))
            r.values[f"{key}_up"] = 1.0 if procs else 0.0
            r.identities[label] = sorted(f"{p.pid}:{p.info['create_time']:.0f}" for p in procs)
            rss = fds = threads = 0.0
            for proc in procs:
                try:
                    group = [proc, *proc.children(recursive=True)] if self._tree else [proc]
                    for p in group:
                        rss += p.memory_info().rss
                        threads += p.num_threads()
                    fds += proc.num_fds()
                except psutil.Error:
                    continue
            r.values[f"{key}_rss_mb"] = rss / 2**20 if procs else None
            r.values[f"{key}_fds"] = fds if procs else None
            r.values[f"{key}_threads"] = threads if procs else None
            if not procs:
                r.errors[f"{key}_rss_mb"] = "process not running"
        return r


# --- GPU -------------------------------------------------------------------------------


class GpuCollector:
    NAMES = ("gpu_util_percent", "gpu_mem_used_mb", "gpu_mem_total_mb", "gpu_temp_c")

    def __init__(self, options: dict[str, Any]) -> None:
        self._index = int(options.get("index", 0))

    def collect(self) -> Reading:
        r = Reading()
        binary = shutil.which("nvidia-smi")
        if binary is None:
            r.fail(list(self.NAMES), "nvidia-smi not found")
            return r
        query = "utilization.gpu,memory.used,memory.total,temperature.gpu"
        try:
            out = subprocess.run(
                [
                    binary,
                    f"--id={self._index}",
                    f"--query-gpu={query}",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout
            numbers = [float(x) for x in out.strip().split(",")]
            if len(numbers) != len(self.NAMES):
                raise ValueError(f"unexpected nvidia-smi output {out!r}")
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            r.fail(list(self.NAMES), str(exc)[:100])
            return r
        r.values.update(zip(self.NAMES, numbers, strict=True))
        return r


# --- Prometheus text endpoints ----------------------------------------------------------


def parse_prometheus(text: str) -> list[tuple[str, dict[str, str], float]]:
    from prometheus_client.parser import text_string_to_metric_families

    samples: list[tuple[str, dict[str, str], float]] = []
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            samples.append((s.name, dict(s.labels), float(s.value)))
    return samples


_SELECTOR = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?$")


def select(samples: list[tuple[str, dict[str, str], float]], selector: str) -> float | None:
    """Sum of samples matching ``name{label="v",...}``; None if none match.

    ``A / B`` (two selectors) gives their ratio, e.g. a filesystem free fraction; None if either
    side is absent or the divisor is zero.
    """
    if " / " in selector:
        num, _, den = selector.partition(" / ")
        a, b = select(samples, num), select(samples, den)
        return None if a is None or not b else a / b
    m = _SELECTOR.match(selector.strip())
    if not m:
        raise ValueError(f"bad selector {selector!r}")
    name, raw = m.group(1), m.group(2) or ""
    want = dict(re.findall(r'(\w+)="([^"]*)"', raw))
    hit = [
        v for n, lab, v in samples if n == name and all(lab.get(k) == v2 for k, v2 in want.items())
    ]
    return math.fsum(hit) if hit else None


class PrometheusCollector:
    def __init__(self, options: dict[str, Any]) -> None:
        self._url: str = options["url"]
        self._timeout = float(options.get("timeout_s", 5))
        self._metrics: dict[str, str] = options.get("metrics", {})

    def collect(self) -> Reading:
        r = Reading()
        names = list(self._metrics)
        try:
            if not self._url.startswith(("http://", "https://")):
                raise ValueError("url must be http(s)")
            with urllib.request.urlopen(self._url, timeout=self._timeout) as resp:  # noqa: S310
                text = resp.read(8 * 2**20).decode("utf-8", "replace")
            samples = parse_prometheus(text)
        except Exception as exc:  # noqa: BLE001 -- any scrape failure is "down", not a crash
            r.values["up"] = 0.0
            r.fail(names, f"scrape failed: {str(exc)[:100]}")
            return r
        r.values["up"] = 1.0
        for local, selector in self._metrics.items():
            try:
                value = select(samples, selector)
            except ValueError as exc:
                r.fail([local], str(exc))
                continue
            if value is None:
                r.fail([local], "series not exposed")
            else:
                r.values[local] = value
        start = select(samples, "process_start_time_seconds")
        if start is not None:
            r.identities["process"] = [f"{start:.0f}"]
        return r


class HttpHealthCollector:
    def __init__(self, options: dict[str, Any]) -> None:
        self._url: str = options["url"]
        self._timeout = float(options.get("timeout_s", 5))
        self._ok = int(options.get("expect_status", 200))

    def collect(self) -> Reading:
        r = Reading()
        started = time.monotonic()
        try:
            if not self._url.startswith(("http://", "https://")):
                raise ValueError("url must be http(s)")
            with urllib.request.urlopen(self._url, timeout=self._timeout) as resp:  # noqa: S310
                ok = resp.status == self._ok
        except Exception as exc:  # noqa: BLE001
            r.values["up"] = 0.0
            r.fail(["latency_ms"], str(exc)[:100])
            return r
        r.values["up"] = 1.0 if ok else 0.0
        r.values["latency_ms"] = (time.monotonic() - started) * 1000
        return r


# --- Redis -----------------------------------------------------------------------------


class RedisCollector:
    """Memory, clients, eviction and per-stream/group depth, lag and oldest-pending age."""

    def __init__(self, options: dict[str, Any]) -> None:
        self._opts = options
        self._client: Any = None

    def _connect(self) -> Any:
        import redis

        o = self._opts
        user = os.environ.get(o["username_env"]) if o.get("username_env") else None
        password = os.environ.get(o["password_env"]) if o.get("password_env") else None
        return redis.Redis(
            host=o.get("host", "127.0.0.1"),
            port=int(o.get("port", 6379)),
            db=int(o.get("db", 0)),
            username=user,
            password=password,
            socket_timeout=5,
            socket_connect_timeout=5,
            decode_responses=True,
        )

    def collect(self) -> Reading:
        r = Reading()
        streams: dict[str, str] = self._opts.get("streams", {})  # alias -> stream key
        try:
            if self._client is None:
                self._client = self._connect()
            info = self._client.info()
        except Exception as exc:  # noqa: BLE001
            self._client = None
            r.values["up"] = 0.0
            r.fail(["used_memory_mb", "connected_clients", "evicted_keys"], str(exc)[:100])
            return r
        r.values["up"] = 1.0
        r.values["used_memory_mb"] = info.get("used_memory", 0) / 2**20
        maxmem = info.get("maxmemory", 0)
        r.values["used_memory_ratio"] = info["used_memory"] / maxmem if maxmem else None
        if not maxmem:
            r.errors["used_memory_ratio"] = "maxmemory not set"
        r.values["connected_clients"] = float(info.get("connected_clients", 0))
        r.values["evicted_keys"] = float(info.get("evicted_keys", 0))
        r.values["rejected_connections"] = float(info.get("rejected_connections", 0))
        now_ms = time.time() * 1000
        for alias, key in streams.items():
            a = slug(alias)
            try:
                r.values[f"stream_{a}_len"] = float(self._client.xlen(key))
                for g in self._client.xinfo_groups(key):
                    gname = slug(f"{a}_{g['name']}")
                    r.values[f"group_{gname}_pending"] = float(g.get("pending", 0))
                    lag = g.get("lag")
                    r.values[f"group_{gname}_lag"] = float(lag) if lag is not None else None
                    summary = self._client.xpending(key, g["name"])
                    oldest = summary.get("min") if isinstance(summary, dict) else None
                    age = (now_ms - int(str(oldest).split("-")[0])) / 1000 if oldest else 0.0
                    r.values[f"group_{gname}_pending_age_s"] = max(0.0, age)
            except Exception as exc:  # noqa: BLE001
                r.fail([f"stream_{a}_len"], str(exc)[:100])
        return r


# --- synthetic -------------------------------------------------------------------------


class SyntheticCollector:
    """Deterministic series for dry runs and tests: ``const``, ``linear`` (per sample), ``sine``."""

    def __init__(self, options: dict[str, Any]) -> None:
        self._spec: dict[str, dict[str, Any]] = options.get("series", {})
        self._n = 0
        self.down_at: set[int] = set(options.get("down_at", []))
        self.drop: set[str] = set(options.get("drop", []))

    def collect(self) -> Reading:
        n = self._n
        self._n += 1
        r = Reading()
        r.values["up"] = 0.0 if n in self.down_at else 1.0
        for name, spec in self._spec.items():
            if name in self.drop or n in self.down_at:
                r.fail([name], "synthetic: withheld")
                continue
            kind = spec.get("kind", "const")
            base = float(spec.get("base", 0.0))
            if kind == "const":
                r.values[name] = base
            elif kind == "linear":
                r.values[name] = base + float(spec.get("slope", 0.0)) * n
            elif kind == "sine":
                r.values[name] = base + float(spec.get("amp", 1.0)) * math.sin(n / 5)
            else:
                r.fail([name], f"unknown kind {kind!r}")
        return r


def build_collector(cfg: Collector) -> CollectorImpl:
    table: dict[str, Any] = {
        "host": HostCollector,
        "process": ProcessCollector,
        "gpu": GpuCollector,
        "prometheus": PrometheusCollector,
        "redis": RedisCollector,
        "http_health": HttpHealthCollector,
        "synthetic": SyntheticCollector,
    }
    impl: CollectorImpl = table[cfg.type](cfg.options)
    return impl
