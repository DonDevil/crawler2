"""The committed Prometheus/Grafana configuration agrees with the code that feeds it.

Structural checks only (``promtool``/Grafana are exercised separately, see validation.md); what
matters here is that no alert or panel waits for a ``crawler2_*`` series nothing produces.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "deploy" / "observability"
DOCS = ROOT / "docs" / "phases" / "p14-production-hardening"

_DEF = re.compile(
    r"(?:metrics\.|ctx\.metrics\.|\bgauge\(|\bcounter\(|\bhistogram\()"
    r"(counter|gauge|histogram)?\(?\s*\n?\s*\"([a-z0-9_]+)\""
)


def produced_metrics() -> set[str]:
    names: set[str] = set()
    pattern = re.compile(r"\b(counter|gauge|histogram)\(\s*\n?\s*\"([a-z0-9_]+)\"")
    for path in (ROOT / "crawler2").rglob("*.py"):
        for kind, name in pattern.findall(path.read_text()):
            full = f"crawler2_{name}"
            if kind == "counter":
                names.add(full if full.endswith("_total") else full + "_total")
            elif kind == "histogram":
                names.update({f"{full}_bucket", f"{full}_sum", f"{full}_count"})
            else:
                names.add(full)
    return names


def referenced(text: str) -> set[str]:
    return set(re.findall(r"\bcrawler2_[a-z0-9_]+", text))


def alert_rules() -> list[dict[str, Any]]:
    doc = yaml.safe_load((OBS / "alerts" / "crawler2.rules.yml").read_text())
    return [r for g in doc["groups"] for r in g["rules"]]


def test_metric_scan_finds_the_real_series() -> None:
    found = produced_metrics()
    for expected in (
        "crawler2_fetch_attempts_total",
        "crawler2_fetch_duration_seconds_bucket",
        "crawler2_worker_state",
        "crawler2_stream_group_lag",
        "crawler2_outbox_checkpoint_lag_seconds",
        "crawler2_frontier_dead_letters",
    ):
        assert expected in found, expected


def test_every_crawler2_series_in_rules_and_dashboards_is_produced_by_code() -> None:
    produced = produced_metrics()
    unknown: dict[str, set[str]] = {}
    sources = {
        "alerts": (OBS / "alerts" / "crawler2.rules.yml").read_text(),
        **{p.name: p.read_text() for p in (OBS / "grafana" / "dashboards").glob("*.json")},
    }
    for name, text in sources.items():
        missing = referenced(text) - produced - {"crawler2_workers", "crawler2_exporter"}
        # process_* come from prometheus_client itself; crawler2_ prefixed names must exist.
        if missing:
            unknown[name] = missing
    assert unknown == {}


def test_alert_rules_are_complete_and_documented() -> None:
    rules = alert_rules()
    names = [r["alert"] for r in rules]
    assert len(names) == len(set(names)), "duplicate alert names"
    assert len(names) >= 20
    runbook = (DOCS / "runbook.md").read_text() if (DOCS / "runbook.md").exists() else ""
    recovery = (DOCS / "recovery.md").read_text() if (DOCS / "recovery.md").exists() else ""
    for r in rules:
        assert r["labels"]["severity"] in {"info", "warning", "critical"}, r["alert"]
        assert "expr" in r
        doc_path, _, anchor = r["annotations"]["runbook"].partition("#")
        text = (ROOT / doc_path).read_text() if (ROOT / doc_path).exists() else ""
        assert text, f"{r['alert']}: {doc_path} does not exist"
        assert re.search(rf"^#+ .*\{{#{re.escape(anchor)}\}}", text, re.M), (
            f"{r['alert']}: no heading with {{#{anchor}}} in {doc_path}"
        )
    del runbook, recovery


def test_every_required_class_of_alert_exists() -> None:
    """The brief's list: stalled work, queue age, event lag, outbox backlog, retries/failures,
    dead-letter growth, restarts, disk, Redis memory, Scylla health, GPU memory."""
    names = {r["alert"] for r in alert_rules()}
    for required in (
        "ConsumerStalled",
        "FrontierNotDraining",
        "EventLagGrowing",
        "OutboxRelayBehind",
        "FetchFailureRatioHigh",
        "DeadLettersGrowing",
        "ProcessRestarting",
        "DiskSpaceLow",
        "RedisMemoryHigh",
        "ScyllaNodeDown",
        "GpuMemoryHigh",
        "MinioDown",
    ):
        assert required in names, required


def test_prometheus_config_points_at_example_target_files() -> None:
    cfg = yaml.safe_load((OBS / "prometheus" / "prometheus.yml").read_text())
    jobs = {j["job_name"] for j in cfg["scrape_configs"]}
    assert {"crawler2-workers", "crawler2-services", "crawler2-exporter", "redis", "scylla"} <= jobs
    for job in cfg["scrape_configs"]:
        for sd in job["file_sd_configs"]:
            for f in sd["files"]:
                example = OBS / "targets" / (Path(f).stem + ".example.yml")
                assert example.exists(), f"{job['job_name']}: no example for {f}"


def test_committed_dashboards_equal_the_generator_output() -> None:
    spec = importlib.util.spec_from_file_location("gen_dashboards", OBS / "generate_dashboards.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, text in mod.render().items():
        assert (OBS / "grafana" / "dashboards" / name).read_text() == text, (
            f"{name} is stale: run deploy/observability/generate_dashboards.py"
        )


@pytest.mark.parametrize("path", sorted((OBS / "grafana" / "dashboards").glob("*.json")))
def test_dashboards_are_valid_grafana_json(path: Path) -> None:
    d = json.loads(path.read_text())
    assert d["uid"]
    assert d["title"]
    ids = [p["id"] for p in d["panels"]]
    assert len(ids) == len(set(ids))
    for p in d["panels"]:
        assert p["targets"], p["title"]
        assert all(t["expr"].strip() for t in p["targets"])


def test_alertmanager_example_names_no_endpoint_or_secret() -> None:
    text = (OBS / "alertmanager" / "alertmanager.example.yml").read_text()
    assert "url_file" in text
    assert not re.search(r"https?://[^\s]*hooks|api_key|password", text)
    doc = yaml.safe_load(text)
    assert doc["route"]["receiver"] == "operator"


def test_every_alert_has_a_promtool_firing_test() -> None:
    """`promtool test rules` (CI job `observability`) must fire each alert once."""
    doc = yaml.safe_load((OBS / "rules-tests" / "crawler2.rules.test.yml").read_text())
    tested = {
        c["alertname"] for t in doc["tests"] for c in t["alert_rule_test"] if c.get("exp_alerts")
    }
    missing = {r["alert"] for r in alert_rules()} - tested
    assert missing == set(), f"alerts never fired in a rule test: {sorted(missing)}"
