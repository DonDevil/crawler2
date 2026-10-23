"""Soak configuration validation, redaction and the run directory's crash safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from crawler2.soak.config import Approval, Mode, SoakConfig, load_config, redact
from crawler2.soak.store import LockHeldError, RunStore, atomic_write
from tests.unit.soak.helpers import make_config


def test_production_run_must_last_168_hours_unless_it_is_a_dry_run() -> None:
    with pytest.raises(ValidationError, match="168"):
        SoakConfig(run_name="p", mode=Mode.PRODUCTION, duration_h=24)
    dry = SoakConfig(run_name="p", mode=Mode.PRODUCTION, duration_h=0.1, dry_run=True)
    assert not dry.m6_eligible
    full = SoakConfig(run_name="p", mode=Mode.PRODUCTION)
    assert full.m6_eligible
    assert full.duration_h == 168


def test_synthetic_runs_are_never_m6_eligible() -> None:
    assert not SoakConfig(run_name="s", mode=Mode.SYNTHETIC, duration_h=200).m6_eligible


def test_unknown_keys_and_bad_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SoakConfig(run_name="s", mode=Mode.SYNTHETIC, surprise=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        SoakConfig(run_name="Bad Name", mode=Mode.SYNTHETIC)
    with pytest.raises(ValidationError):
        SoakConfig(run_name="s", mode=Mode.SYNTHETIC, sample_interval_s=0)


def test_load_config_from_toml(tmp_path: Path) -> None:
    f = tmp_path / "c.toml"
    f.write_text(
        'run_name = "t"\nmode = "synthetic"\nduration_h = 0.01\n'
        "[thresholds]\nmax_restarts_per_service = 2\n"
    )
    cfg = load_config(f)
    assert cfg.thresholds.max_restarts_per_service == 2
    assert cfg.thresholds.max_queue_age_s is None, "undecided limits stay None"


def test_approval_needs_all_three_fields() -> None:
    assert not Approval(approved_by="a").complete
    assert Approval(approved_by="a", approved_at="2026-10-23", reference="ADR-x").complete


def test_redact_blanks_secret_looking_values_but_keeps_env_var_names() -> None:
    doc = {
        "redis": {"password": "hunter2", "password_env": "REDIS_PW", "api_token": "t"},
        "n": [{"secret": "x", "ok": 1}],
    }
    out = redact(doc)
    assert out["redis"]["password"] == "***redacted***"
    assert out["redis"]["api_token"] == "***redacted***"
    assert out["redis"]["password_env"] == "REDIS_PW"
    assert out["n"][0] == {"secret": "***redacted***", "ok": 1}
    assert "hunter2" not in json.dumps(out)


def test_config_fingerprint_changes_with_any_setting(tmp_path: Path) -> None:
    a, b = make_config(tmp_path), make_config(tmp_path, sample_interval_s=30.0)
    assert a.fingerprint() == make_config(tmp_path).fingerprint()
    assert a.fingerprint() != b.fingerprint()


# --- store ------------------------------------------------------------------------------------


def test_atomic_write_never_leaves_a_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    atomic_write(target, b'{"a": 1}')
    atomic_write(target, b'{"a": 2}')
    assert json.loads(target.read_text()) == {"a": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"], "no temp files left behind"


def test_checkpoint_roundtrip_and_fallback_to_previous_generation(tmp_path: Path) -> None:
    store = RunStore.create(tmp_path, "r")
    assert store.load_checkpoint() == (None, "none")
    store.save_checkpoint({"seq": 1})
    store.save_checkpoint({"seq": 2})
    assert store.load_checkpoint() == ({"seq": 2}, "current")
    (store.path / "checkpoint.json").write_text('{"data": {"seq": 99}, "sha256": "bad"}')
    assert store.load_checkpoint() == ({"seq": 1}, "previous")
    (store.path / "checkpoint.prev.json").write_text("not json")
    data, source = store.load_checkpoint()
    assert data is None
    assert source == "corrupt"


def test_torn_and_garbage_jsonl_lines_are_counted_not_fatal(tmp_path: Path) -> None:
    store = RunStore.create(tmp_path, "r")
    store.append("samples.jsonl", {"a": 1})
    with open(store.path / "samples.jsonl", "a") as fh:
        fh.write('{"a": 2}\n{"a": 3, "tor')  # crash mid-write
    res = store.read_jsonl("samples.jsonl")
    assert [r["a"] for r in res.rows] == [1, 2]
    assert res.corrupt_lines == 1


def test_second_runner_cannot_take_a_live_lock(tmp_path: Path) -> None:
    store = RunStore.create(tmp_path, "r")
    with store.locked():
        assert store.runner_alive()
        with pytest.raises(LockHeldError):
            RunStore(store.path).acquire()
    assert not store.runner_alive(), "released on exit (also on crash: flock dies with the process)"
    with RunStore(store.path).locked():
        pass
