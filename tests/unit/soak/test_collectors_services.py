"""Collectors that talk to services: health endpoints, Redis, nvidia-smi - against local fakes."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from crawler2.soak.collectors import (
    GpuCollector,
    HttpHealthCollector,
    PrometheusCollector,
    RedisCollector,
    build_collector,
)
from crawler2.soak.config import Collector


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/boom":
            self.send_response(500)
            self.end_headers()
            return
        body = b"# TYPE depth gauge\ndepth 7\nprocess_start_time_seconds 1.7e9\n"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_http_health_up_down_and_bad_status(server: str) -> None:
    ok = HttpHealthCollector({"url": server + "/health"}).collect()
    assert ok.values["up"] == 1.0
    assert (ok.values["latency_ms"] or 0) >= 0
    bad = HttpHealthCollector({"url": server + "/boom"}).collect()
    assert bad.values["up"] == 0.0, "an error status is down"
    down = HttpHealthCollector({"url": "http://127.0.0.1:1/", "timeout_s": 1}).collect()
    assert down.values["up"] == 0.0
    assert down.values["latency_ms"] is None
    assert HttpHealthCollector({"url": "file:///etc/passwd"}).collect().values["up"] == 0.0


def test_prometheus_collector_reads_series_and_marks_absent_ones(server: str) -> None:
    r = PrometheusCollector(
        {"url": server, "metrics": {"depth": "depth", "ghost": "ghost_metric", "bad": "x{"}}
    ).collect()
    assert r.values["up"] == 1.0
    assert r.values["depth"] == 7.0
    assert r.values["ghost"] is None
    assert r.errors["ghost"] == "series not exposed"
    assert "bad selector" in r.errors["bad"]
    assert r.identities["process"] == ["1700000000"]
    gone = PrometheusCollector(
        {"url": "http://127.0.0.1:1/", "timeout_s": 1, "metrics": {"d": "depth"}}
    )
    out = gone.collect()
    assert out.values["up"] == 0.0
    assert out.values["d"] is None


class FakeRedis:
    def __init__(self) -> None:
        self.fail = False

    def info(self) -> dict[str, Any]:
        if self.fail:
            raise ConnectionError("down")
        return {
            "used_memory": 2**20 * 100,
            "maxmemory": 2**20 * 400,
            "connected_clients": 3,
            "evicted_keys": 0,
            "rejected_connections": 0,
        }

    def xlen(self, key: str) -> int:
        return 12

    def xinfo_groups(self, key: str) -> list[dict[str, Any]]:
        return [{"name": "g", "pending": 2, "lag": 5}]

    def xpending(self, key: str, group: str) -> dict[str, Any]:
        return {"min": "1000-0"}


def test_redis_collector_reports_memory_streams_and_outage() -> None:
    c = RedisCollector({"streams": {"page_observed": "events:page.observed.v1"}})
    fake = FakeRedis()
    c._client = fake
    r = c.collect()
    assert r.values["up"] == 1.0
    assert r.values["used_memory_mb"] == 100.0
    assert r.values["used_memory_ratio"] == 0.25
    assert r.values["stream_page_observed_len"] == 12.0
    assert r.values["group_page_observed_g_pending"] == 2.0
    assert r.values["group_page_observed_g_lag"] == 5.0
    assert (r.values["group_page_observed_g_pending_age_s"] or 0) > 1000
    fake.fail = True
    down = c.collect()
    assert down.values["up"] == 0.0
    assert down.values["used_memory_mb"] is None
    assert c._client is None, "a failed connection is rebuilt on the next round"


def _fake_smi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    exe = tmp_path / "nvidia-smi"
    exe.write_text(f"#!/bin/sh\n{script}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_gpu_collector_parses_nvidia_smi_and_handles_its_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_smi(tmp_path, monkeypatch, "echo '37, 1024, 4096, 61'")
    r = GpuCollector({}).collect()
    assert r.values == {
        "gpu_util_percent": 37.0,
        "gpu_mem_used_mb": 1024.0,
        "gpu_mem_total_mb": 4096.0,
        "gpu_temp_c": 61.0,
    }
    _fake_smi(tmp_path, monkeypatch, "echo garbage; exit 0")
    bad = GpuCollector({}).collect()
    assert bad.values["gpu_util_percent"] is None
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    missing = GpuCollector({}).collect()
    assert missing.errors["gpu_util_percent"] == "nvidia-smi not found"


def test_build_collector_covers_every_declared_type() -> None:
    for kind, options in (
        ("host", {}),
        ("process", {"match": {"x": "y"}}),
        ("gpu", {}),
        ("prometheus", {"url": "http://127.0.0.1:1/"}),
        ("redis", {}),
        ("http_health", {"url": "http://127.0.0.1:1/"}),
        ("synthetic", {}),
    ):
        assert build_collector(Collector(name="c", type=kind, options=options)) is not None  # type: ignore[arg-type]


def test_command_evidence_verifier_classifies_validator_output(tmp_path: Path) -> None:
    import sys

    from crawler2.soak.config import EvidenceCheck
    from crawler2.soak.reconcile import CommandEvidenceVerifier

    script = (
        "import json,sys\n"
        "i=sys.argv[1]\n"
        "docs={'ok':{'outcome':'complete','artifacts':{'verified':3}},"
        "'bad':{'outcome':'rejected','artifacts':{'corrupted':1}},"
        "'gone':{'outcome':'partial','artifacts':{'missing':1,'verified':2}},"
        "'unv':{'outcome':'partial','artifacts':{'unverifiable':1}},"
        "'err':{'error':'not found'}}\n"
        "if i=='junk': print('not json')\n"
        "else: print(json.dumps(docs[i]))\n"
    )
    cfg = EvidenceCheck(command=[sys.executable, "-c", script, "{id}"], sample=10, timeout_s=20)
    res = CommandEvidenceVerifier(cfg).verify(["ok", "bad", "gone", "unv", "err", "junk"])
    assert res.checked == 5, "unparseable output is an error, not a verified item"
    assert res.mismatched == ["bad"]
    assert res.missing == ["gone"]
    assert res.unverifiable == ["unv"]
    assert len(res.errors) == 2
    assert CommandEvidenceVerifier(cfg).verify([]).checked == 0
    capped = CommandEvidenceVerifier(cfg.model_copy(update={"sample": 1})).verify(["ok", "bad"])
    assert (capped.checked, capped.mismatched) == (1, [])


def test_exporter_gpu_reader_parses_nvidia_smi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crawler2.diagnostics.exporter import _read_gpu

    _fake_smi(tmp_path, monkeypatch, "echo '41, 2048, 4096'")
    assert _read_gpu() == [41.0, 2048.0, 4096.0]
    _fake_smi(tmp_path, monkeypatch, "echo nonsense")
    assert _read_gpu() is None
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert _read_gpu() is None
