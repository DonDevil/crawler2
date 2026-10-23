"""Preflight, fingerprints, and the real (non-simulated-clock) entry points of the runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from crawler2.soak import runner
from crawler2.soak.config import Approval, Mode, Redis, SoakConfig
from crawler2.soak.store import RunStore
from tests.unit.soak.helpers import make_config


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    (r / "a.txt").write_text("one")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "init")
    return r


def checks(cfg: SoakConfig, tmp: Path) -> dict[str, runner.Check]:
    return {c.name: c for c in runner.preflight(cfg, tmp / "runs")}


def test_git_state_reports_commit_dirtiness_and_a_diff_hash(repo: Path) -> None:
    clean = runner.git_state(str(repo), deep=True)
    assert clean["dirty"] is False
    assert clean["diff_sha256"] == ""
    assert len(str(clean["commit"])) == 40
    (repo / "a.txt").write_text("two")
    dirty = runner.git_state(str(repo), deep=True)
    assert dirty["dirty"] is True
    assert dirty["diff_sha256"] != ""
    assert runner.git_state(str(repo / "nowhere"), deep=False)["commit"] is None


def test_hash_path_changes_with_content_and_reports_missing(tmp_path: Path) -> None:
    f = tmp_path / "f.env"
    f.write_text("A=1")
    first = runner.hash_path(str(f))
    f.write_text("A=2")
    assert runner.hash_path(str(f)) != first
    d = tmp_path / "d"
    d.mkdir()
    (d / "x").write_text("1")
    h1 = runner.hash_path(str(d))
    (d / "y").write_text("2")
    assert runner.hash_path(str(d)) != h1
    assert runner.hash_path(str(tmp_path / "nope")) == "missing"


def test_fingerprints_cover_config_topology_repos_and_files(repo: Path, tmp_path: Path) -> None:
    watched = tmp_path / "w.env"
    watched.write_text("x")
    cfg = make_config(tmp_path, repos=[str(repo)], watch_paths=[str(watched)], topology={"h": "r"})
    fp = runner.fingerprints(cfg, deep=True)
    assert {
        "config",
        "topology",
        f"git:{repo}:commit",
        f"git:{repo}:dirty",
        f"git:{repo}:diff",
    } <= set(fp)
    assert fp[f"file:{watched}"] == runner.hash_path(str(watched))
    assert f"file:{watched}" not in runner.fingerprints(cfg, deep=False), (
        "files are hashed on deep rounds"
    )


def test_production_preflight_names_every_missing_prerequisite(tmp_path: Path) -> None:
    cfg = SoakConfig(run_name="p", mode=Mode.PRODUCTION, run_root=tmp_path / "runs")
    c = checks(cfg, tmp_path)
    assert c["approval"].status == "fail"
    assert c["topology"].status == "fail"
    assert c["redis"].status == "fail"
    assert c["repos"].status == "fail"
    assert c["thresholds"].status == "warn"
    assert c["stages"].status == "warn"
    assert not runner.preflight_ok(list(c.values()))


def test_preflight_wants_a_clean_pinned_tree_and_a_complete_approval(
    repo: Path, tmp_path: Path
) -> None:
    cfg = SoakConfig(
        run_name="p",
        mode=Mode.PRODUCTION,
        run_root=tmp_path / "runs",
        repos=[str(repo)],
        topology={"host-a": "crawler"},
        approval=Approval(approved_by="o", approved_at="2026-10-23", reference="ADR-x"),
        redis=Redis(host="127.0.0.1", port=1),
        required_stages=["match.found"],
    )
    c = checks(cfg, tmp_path)
    assert c["approval"].status == "ok"
    assert c[f"git:{repo}"].status == "ok"
    assert c["redis"].status == "fail", "port 1 is closed"
    (repo / "a.txt").write_text("changed")
    assert checks(cfg, tmp_path)[f"git:{repo}"].status == "fail"


def test_dry_run_preflight_does_not_demand_approval(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    c = checks(cfg, tmp_path)
    assert "approval" not in c
    assert runner.preflight_ok(list(c.values()))


def test_run_new_completes_and_a_completed_run_refuses_to_resume(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, duration_h=0.0004, sample_interval_s=0.3)
    store, phase = runner.run_new(cfg, tmp_path / "runs")
    assert phase == "completed"
    assert runner.status(store)["phase"] == "completed"
    with pytest.raises(RuntimeError, match="completed"):
        runner.resume_run(store.path)


def test_stop_then_resume_through_the_public_entry_points(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, duration_h=0.002, sample_interval_s=0.3)

    def factory(c: SoakConfig, store: RunStore) -> runner.Runtime:
        runner.request_stop(store.path)  # the operator pressed stop right after the start
        return runner.build_runtime(c, store)

    store, phase = runner.run_new(cfg, tmp_path / "runs", rt_factory=factory)
    assert phase == "stopped"
    assert runner.evaluate_run(store).verdict.value == "INTERRUPTED"
    runner.clear_stop(store.path)
    store2, phase2 = runner.resume_run(store.path)
    assert (store2.path, phase2) == (store.path, "completed")
    kinds = [e["kind"] for e in store.read_jsonl("events.jsonl").rows]
    assert kinds.count("start") == 1
    assert "resume" in kinds


def test_resume_needs_the_stored_config(tmp_path: Path) -> None:
    d = tmp_path / "empty-run"
    d.mkdir()
    with pytest.raises(RuntimeError, match=r"config\.json missing"):
        runner.resume_run(d)


def test_production_mode_without_redis_cannot_build_a_runtime(tmp_path: Path) -> None:
    cfg = SoakConfig(run_name="p", mode=Mode.PRODUCTION, dry_run=True, duration_h=0.01)
    store = RunStore.create(tmp_path, "p")
    with pytest.raises(ValueError, match="redis"):
        runner.build_runtime(cfg, store)


def test_environment_info_has_the_fields_the_report_needs(tmp_path: Path) -> None:
    info = runner.environment_info(make_config(tmp_path))
    assert {"host", "cpu_count", "mem_total_gb", "disks", "versions", "python"} <= set(info)
    assert info["cpu_count"]
