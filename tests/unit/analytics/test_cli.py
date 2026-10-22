"""``crawler2-analytics`` read-side commands against a SYNTHETIC export (no store)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from crawler2.analytics import cli
from tests.unit.analytics.conftest import Exported


@pytest.fixture(autouse=True)
def _no_global_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` configures process-wide logging; keep it off pytest's captured streams."""
    monkeypatch.setattr(cli, "configure_logging", lambda *_a, **_k: None)


def _run(capsys: pytest.CaptureFixture[str], root: Path, *argv: str) -> tuple[int, str]:
    code = cli.main(["--root", str(root), *argv])
    return code, capsys.readouterr().out


def test_status_validate_and_sql(exported: Exported, capsys: pytest.CaptureFixture[str]) -> None:
    key = exported.result.export_key
    code, out = _run(capsys, exported.root, "status")
    assert code == 0
    assert json.loads(out)["exports"][0]["current"] == 1
    code, out = _run(capsys, exported.root, "validate", key)
    assert code == 0
    assert json.loads(out)["ok"]
    code, out = _run(capsys, exported.root, "sql", key, "SELECT count(*) AS n FROM media")
    assert code == 0
    assert out.splitlines() == ["n", "3"]
    code, _ = _run(capsys, exported.root, "sql", key, "SELECT nope FROM media")
    assert code == cli.EXIT_INVALID


def test_reports_graph_cluster(
    exported: Exported, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    key = exported.result.export_key
    out_file = tmp_path / "campaign.json"
    code, _ = _run(
        capsys,
        exported.root,
        "report",
        "campaign",
        key,
        "--campaign",
        "synthetic-case",
        "--out",
        str(out_file),
        "--verify",
        "--require-complete",
    )
    assert code == 0
    assert json.loads(out_file.read_text())["matches"]["reported"] == 2
    code, out = _run(capsys, exported.root, "report", "source", key, "--format", "markdown")
    assert code == 0
    assert "| site-a.example |" in out
    code, _ = _run(capsys, exported.root, "report", "campaign", key, "--campaign", "nope")
    assert code == cli.EXIT_NOT_FOUND
    code, out = _run(capsys, exported.root, "graph", "build", key)
    assert code == 0
    assert json.loads(out)["outcome"] == "built"
    code, out = _run(
        capsys, exported.root, "cluster", "run", key, "--min-pages", "1", "--min-samples", "2"
    )
    assert code == 0
    assert json.loads(out)["bootstrap_status"] == "unreviewed"


def test_unknown_export_is_not_found(
    exported: Exported, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run(capsys, exported.root, "validate", "0000000000000000")
    assert code == cli.EXIT_NOT_FOUND


def test_gc_removes_only_failed_unpublished_runs(
    exported: Exported, capsys: pytest.CaptureFixture[str]
) -> None:
    staging = exported.root / "_staging"
    for name, status in (("old-failed", "failed"), ("old-running", "running")):
        (staging / name).mkdir()
        (staging / name / "run.json").write_text(json.dumps({"status": status}))
        os.utime(staging / name, (0, 0))
    code, out = _run(capsys, exported.root, "gc", "--older-than-hours", "1")
    doc = json.loads(out)
    assert code == 0
    assert [r["run_id"] for r in doc["removed"]] == ["old-failed"]
    assert (staging / "old-running").exists()
    assert exported.revision_dir.exists()
    code, out = _run(capsys, exported.root, "gc", "--older-than-hours", "1", "--force")
    assert [r["run_id"] for r in json.loads(out)["removed"]] == ["old-running"]


def test_datasets_catalog(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out = _run(capsys, tmp_path, "datasets")
    doc = json.loads(out)
    assert code == 0
    assert doc["datasets"]["page_links"]["source_table"] == "page_extracts"
    assert "fingerprinter keyspace" in doc["excluded"]


def test_shipped_analyst_queries_run(
    exported: Exported, capsys: pytest.CaptureFixture[str]
) -> None:
    sql_dir = Path(cli.__file__).parent / "sql"
    files = sorted(sql_dir.glob("*.sql"))
    assert len(files) >= 4
    for f in files:
        code, out = _run(capsys, exported.root, "sql", exported.result.export_key, "--file", str(f))
        assert code == 0, f.name
        assert out.strip(), f.name
