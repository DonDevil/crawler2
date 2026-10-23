"""The operator path: prepare -> dry-run -> status -> report through the real CLI entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crawler2.soak import cli
from crawler2.soak.store import RunStore

TOML = """
run_name = "cli"
mode = "synthetic"
duration_h = 0.0006        # ~2 s
sample_interval_s = 0.5
run_root = "{root}"
required_stages = ["match.found"]

[[collectors]]
name = "svc"
type = "synthetic"
[collectors.options.series.rss_mb]
kind = "const"
base = 100
[[collectors.series]]
name = "up"
role = "up"
[[collectors.series]]
name = "rss_mb"

[[edges]]
name = "encode"
from_type = "encode.requested"
to_types = ["representation.ready"]
from_key = "payload.content.content_id"
to_key = "payload.representation.content_id"
grace_s = 300
"""


@pytest.fixture
def config(tmp_path: Path) -> Path:
    f = tmp_path / "soak.toml"
    f.write_text(TOML.format(root=tmp_path / "runs"))
    return f


def test_prepare_reports_every_check(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["prepare", str(config)]) == 0
    out = capsys.readouterr().out
    assert "PREFLIGHT OK" in out
    assert "[OK  ] collector:svc" in out


def test_dry_run_status_and_report_end_to_end(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["dry-run", str(config)]) == 0
    out = capsys.readouterr().out
    assert "phase completed" in out
    (run_dir,) = (tmp_path / "runs").iterdir()
    for name in (
        "report.json",
        "report.md",
        "report.html",
        "samples.jsonl",
        "events.jsonl",
        "run.json",
    ):
        assert (run_dir / name).exists(), name

    assert cli.main(["status", str(run_dir)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["phase"] == "completed"
    assert status["runner_alive"] is False
    assert status["m6_eligible"] is False

    rc = cli.main(["report", str(run_dir)])
    rep = json.loads(capsys.readouterr().out)
    assert rep["m6_eligible"] is False
    assert rc == 1, "the short run lacks growth data etc.: not a PASS, so a non-zero exit"
    doc = json.loads((run_dir / "report.json").read_text())
    assert doc["verdict"] == "INCONCLUSIVE"
    assert {c["id"] for c in doc["criteria"] if c["status"] == "PASS"} >= {"FREEZE", "SAMPLES"}


def test_stop_then_resume(config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from crawler2.soak import runner
    from crawler2.soak.config import load_config

    cfg = load_config(config)
    store = RunStore.create(tmp_path / "runs2", "cli")
    rt = runner.build_runtime(cfg, store)
    run = runner.SoakRunner(cfg, store, rt)
    run.initialize()
    run.round()
    rt.ledger.close()
    assert cli.main(["status", str(store.path)]) == 0
    assert json.loads(capsys.readouterr().out)["needs_resume"] is True
    assert cli.main(["stop", str(store.path)]) == 0
    assert (store.path / "STOP").exists()
    assert cli.main(["resume", str(store.path)]) == 0, "resume clears the stop flag and finishes"
    assert not (store.path / "STOP").exists()


def test_start_refuses_when_preflight_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "prod.toml"
    f.write_text('run_name = "p"\nmode = "production"\n')
    assert cli.main(["start", str(f)]) == 2
    err = capsys.readouterr().err
    assert "refusing to start" in err
    assert "approval" in err
    assert not (tmp_path / "var").exists() or not list((tmp_path / "var").glob("*"))


def test_dry_run_command_refuses_an_m6_eligible_config(tmp_path: Path) -> None:
    f = tmp_path / "prod.toml"
    f.write_text('run_name = "p"\nmode = "production"\n')
    assert cli.main(["dry-run", str(f)]) == 2


def test_invalid_config_is_a_clear_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.toml"
    f.write_text('run_name = "p"\nmode = "production"\nduration_h = 5\n')
    with pytest.raises(Exception, match="168"):
        cli.main(["prepare", str(f)])


def test_the_shipped_example_configs_parse_and_the_production_one_is_not_startable() -> None:
    from crawler2.soak import runner
    from crawler2.soak.config import load_config

    root = Path(__file__).resolve().parents[3] / "scripts" / "soak"
    for name in ("dryrun-synthetic.toml", "dryrun-local-stack.toml"):
        cfg = load_config(root / name)
        assert not cfg.m6_eligible, name
    prod = load_config(root / "production.example.toml")
    assert prod.m6_eligible
    assert prod.duration_h == 168
    checks = {c.name: c for c in runner.preflight(prod)}
    assert checks["approval"].status == "fail", "no one has approved any limit yet"
    assert checks["thresholds"].status == "warn"
    assert "match.found" in prod.required_stages
