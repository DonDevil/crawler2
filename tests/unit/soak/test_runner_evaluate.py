"""End-to-end behaviour of the runner and the evaluator on a fake clock.

Each test drives a whole (simulated) run through the same code the 168 h run uses and checks
the verdict and the criterion that must explain it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from crawler2.soak import runner
from crawler2.soak.config import Series
from crawler2.soak.evaluate import Status, Verdict
from crawler2.soak.report import build_report, to_html
from crawler2.soak.store import LockHeldError, RunStore
from crawler2.soak.synthetic import Faults, SyntheticVerifier
from tests.unit.soak.helpers import (
    Clock,
    make_config,
    make_runtime,
    series_options,
    start_run,
)


def run_to_end(tmp: Path, clock: Clock | None = None, **kw: Any) -> tuple[RunStore, Any]:
    clock = clock or Clock()
    faults = kw.pop("faults", None)
    verifier = kw.pop("verifier", None)
    cfg = make_config(tmp, **kw)
    store, run = start_run(cfg, tmp, clock, faults, verifier)
    assert run.run() == "completed"
    return store, runner.evaluate_run(store, now=clock.time())


def crit(ev: Any, id_: str) -> Any:
    return next(c for c in ev.criteria if c.id == id_)


def test_a_clean_synthetic_run_passes_but_is_never_m6_eligible(tmp_path: Path) -> None:
    store, ev = run_to_end(tmp_path)
    assert ev.verdict is Verdict.PASS, [
        (c.id, c.status.value, c.detail) for c in ev.criteria if c.status is not Status.PASS
    ]
    assert not ev.m6_eligible
    assert "NOT evidence for the M6" in ev.banner
    assert store.read_jsonl("samples.jsonl").rows[0]["values"]["svc.up"] == 1.0
    assert ev.summary["samples"] >= 30


def test_lost_work_fails_the_run_and_names_the_keys(tmp_path: Path) -> None:
    _, ev = run_to_end(tmp_path, faults=Faults(lose_every=7, finalize_every=3))
    c = crit(ev, "LOST_WORK")
    assert ev.verdict is Verdict.FAIL
    assert c.status is Status.FAIL
    assert c.evidence["examples"]["encode"], "unaccounted keys are listed for the runbook"


def test_duplicate_business_effect_fails_but_redelivery_does_not(tmp_path: Path) -> None:
    _, ev = run_to_end(tmp_path, faults=Faults(redeliver_every=2, finalize_every=3))
    assert ev.verdict is Verdict.PASS
    assert sum(ev.summary["redeliveries"].values()) > 0
    _, ev = run_to_end(tmp_path, faults=Faults(duplicate_effect_every=5, finalize_every=3))
    assert crit(ev, "DUPLICATES").status is Status.FAIL
    assert ev.verdict is Verdict.FAIL


def test_missing_metric_never_passes(tmp_path: Path) -> None:
    opts = series_options()
    opts["drop"] = ["queue_age"]
    _, ev = run_to_end(tmp_path, options=opts)
    assert crit(ev, "METRICS").status is Status.INCONCLUSIVE
    assert "queue_age" in crit(ev, "METRICS").detail
    assert crit(ev, "QUEUE_AGE").status is Status.INCONCLUSIVE
    assert ev.verdict is Verdict.INCONCLUSIVE


def test_undecided_limits_make_their_criteria_inconclusive(tmp_path: Path) -> None:
    from crawler2.soak.config import Thresholds

    _, ev = run_to_end(tmp_path, thresholds=Thresholds())
    undecided = {c.id for c in ev.criteria if c.status is Status.INCONCLUSIVE}
    assert {"QUEUE_AGE", "EVENT_LAG", "DISK", "GROWTH", "RETRIES"} <= undecided
    assert ev.verdict is Verdict.INCONCLUSIVE


def test_threshold_violations_fail(tmp_path: Path) -> None:
    opts = series_options(
        rss_mb={"kind": "linear", "base": 500, "slope": 20},
        queue_age={"kind": "const", "base": 9000},
        disk_free={"kind": "const", "base": 0.02},
    )
    _, ev = run_to_end(tmp_path, options=opts)
    assert crit(ev, "GROWTH").status is Status.FAIL
    assert crit(ev, "QUEUE_AGE").status is Status.FAIL
    assert crit(ev, "DISK").status is Status.FAIL
    assert ev.verdict is Verdict.FAIL


def test_a_leak_below_the_noise_floor_is_not_flagged(tmp_path: Path) -> None:
    opts = series_options(rss_mb={"kind": "linear", "base": 500, "slope": 0.01})
    _, ev = run_to_end(tmp_path, options=opts)
    assert crit(ev, "GROWTH").status is Status.PASS


def test_dependency_outage_longer_than_the_limit_fails(tmp_path: Path) -> None:
    opts = series_options()
    opts["down_at"] = list(range(10, 20))  # 10 samples x 60 s = 600 s > 300 s
    _, ev = run_to_end(tmp_path, options=opts)
    c = crit(ev, "OUTAGE")
    assert c.status is Status.FAIL
    assert c.category == "environment"


def test_short_outage_within_the_limit_passes_the_outage_criterion(tmp_path: Path) -> None:
    opts = series_options()
    opts["down_at"] = [10, 11]
    _, ev = run_to_end(tmp_path, options=opts)
    assert crit(ev, "OUTAGE").status is Status.PASS
    assert crit(ev, "METRICS").status is Status.INCONCLUSIVE, "the outage also left metric gaps"


def test_required_stage_never_seen_is_inconclusive_not_pass(tmp_path: Path) -> None:
    _, ev = run_to_end(tmp_path, faults=Faults(skip_types={"match.found"}, finalize_every=3))
    c = crit(ev, "STAGES")
    assert c.status is Status.INCONCLUSIVE
    assert "match.found" in c.detail
    assert "held" in c.action
    assert ev.verdict is Verdict.INCONCLUSIVE


def test_stream_gap_makes_reconciliation_inconclusive(tmp_path: Path) -> None:
    _, ev = run_to_end(tmp_path, faults=Faults(gap_at_poll=5, finalize_every=3))
    assert crit(ev, "LOST_WORK").status is Status.INCONCLUSIVE


def test_evidence_hash_mismatch_or_missing_artifact_fails(tmp_path: Path) -> None:
    probe = run_to_end(tmp_path)[0]
    ids = runner_ids(probe)
    _, ev = run_to_end(tmp_path, verifier=SyntheticVerifier(bad={"evd_3"}))
    assert crit(ev, "EVIDENCE").status is Status.FAIL
    assert ids, "synthetic workload finalizes evidence"
    _, ev = run_to_end(tmp_path, verifier=SyntheticVerifier(missing={"evd_6"}))
    assert crit(ev, "EVIDENCE").status is Status.FAIL
    assert crit(ev, "EVIDENCE").category == "evidence"


def runner_ids(store: RunStore) -> list[str]:
    from crawler2.soak.reconcile import Ledger

    led = Ledger(store.path / "ledger.sqlite3", [])
    try:
        return led.finalized_evidence()
    finally:
        led.close()


# --- interruption, resume, stop, corruption ------------------------------------------------------


def test_stopped_run_is_interrupted_and_a_resume_records_the_gap(tmp_path: Path) -> None:
    clock = Clock()
    cfg = make_config(tmp_path)
    store, run = start_run(cfg, tmp_path, clock)
    for _ in range(10):
        run.round()
        clock.sleep(60)
    runner.request_stop(store.path)
    assert run.run() == "stopped"
    ev = runner.evaluate_run(store, now=clock.time())
    assert ev.verdict is Verdict.INTERRUPTED

    runner.clear_stop(store.path)
    clock.sleep(600)  # the runner was away for 10 minutes
    run2 = runner.SoakRunner(cfg, store, run.rt)  # the event streams outlive the runner
    run2.resume_state()
    assert run2.run() == "completed"
    events = store.read_jsonl("events.jsonl").rows
    gaps = [e for e in events if e["kind"] == "interruption"]
    assert len(gaps) == 1
    assert 500 < gaps[0]["gap_s"] < 800
    ev = runner.evaluate_run(store, now=clock.time())
    assert crit(ev, "CONTINUITY").status is Status.PASS, (
        "1 gap of ~10 min is within max_interruptions=1 / 900 s"
    )
    assert [c.id for c in ev.criteria if c.status is not Status.PASS] == []
    assert ev.verdict is Verdict.PASS


def test_interruption_without_an_approved_continuity_rule_is_inconclusive(tmp_path: Path) -> None:
    from crawler2.soak.config import Thresholds

    clock = Clock()
    th = make_config(tmp_path).thresholds.model_copy(update={"max_interruptions": None})
    cfg = make_config(tmp_path, thresholds=th)
    store, run = start_run(cfg, tmp_path, clock)
    run.round()
    clock.sleep(3000)
    run2 = runner.SoakRunner(cfg, store, make_runtime(cfg, store, clock))
    run2.resume_state()
    run2.run()
    ev = runner.evaluate_run(store, now=clock.time())
    assert crit(ev, "CONTINUITY").status is Status.INCONCLUSIVE
    assert "not decided" in crit(ev, "CONTINUITY").detail
    del Thresholds


def test_too_many_or_too_long_interruptions_fail_continuity(tmp_path: Path) -> None:
    clock = Clock()
    cfg = make_config(tmp_path)
    store, run = start_run(cfg, tmp_path, clock)
    run.round()
    clock.sleep(2000)  # > 900 s limit
    run2 = runner.SoakRunner(cfg, store, make_runtime(cfg, store, clock))
    run2.resume_state()
    run2.run()
    assert crit(runner.evaluate_run(store, now=clock.time()), "CONTINUITY").status is Status.FAIL


def test_resume_survives_a_corrupt_checkpoint_and_says_so(tmp_path: Path) -> None:
    clock = Clock()
    cfg = make_config(tmp_path)
    store, run = start_run(cfg, tmp_path, clock)
    for _ in range(5):
        run.round()
        clock.sleep(60)
    (store.path / "checkpoint.json").write_text("{ truncated")
    (store.path / "checkpoint.prev.json").write_text("garbage")
    run2 = runner.SoakRunner(cfg, store, make_runtime(cfg, store, clock))
    run2.resume_state()
    assert run2.run() == "completed"
    kinds = [e["kind"] for e in store.read_jsonl("events.jsonl").rows]
    assert "checkpoint_recovered" in kinds
    ev = runner.evaluate_run(store, now=clock.time())
    assert crit(ev, "LOST_WORK").status is Status.INCONCLUSIVE, "stream positions were lost"


def test_resume_from_the_previous_checkpoint_generation(tmp_path: Path) -> None:
    clock = Clock()
    cfg = make_config(tmp_path)
    store, run = start_run(cfg, tmp_path, clock)
    for _ in range(3):
        run.round()
        clock.sleep(60)
    (store.path / "checkpoint.json").write_text("{ torn write")
    run2 = runner.SoakRunner(cfg, store, make_runtime(cfg, store, clock))
    run2.resume_state()
    assert run2.state.seq >= 1
    assert any(e["kind"] == "checkpoint_recovered" for e in store.read_jsonl("events.jsonl").rows)


def test_a_completed_run_cannot_be_resumed(tmp_path: Path) -> None:
    clock = Clock()
    cfg = make_config(tmp_path)
    store, run = start_run(cfg, tmp_path, clock)
    run.run()
    run2 = runner.SoakRunner(cfg, store, make_runtime(cfg, store, clock))
    with pytest.raises(RuntimeError, match="completed"):
        run2.resume_state()


def test_two_runners_cannot_own_one_run(tmp_path: Path) -> None:
    store = RunStore.create(tmp_path, "x")
    with store.locked(), pytest.raises(LockHeldError):
        RunStore(store.path).acquire()


def test_clock_going_backwards_is_recorded_and_blocks_a_pass(tmp_path: Path) -> None:
    clock = Clock()
    cfg = make_config(tmp_path)
    store, run = start_run(cfg, tmp_path, clock)
    run.round()
    clock.now -= 3600  # NTP step / VM restore
    run.round()
    clock.sleep(60)
    run.run()
    events = store.read_jsonl("events.jsonl").rows
    assert any(e["kind"] == "clock_anomaly" for e in events)
    ev = runner.evaluate_run(store, now=clock.time())
    assert crit(ev, "CLOCK").status is Status.INCONCLUSIVE
    assert ev.verdict in (Verdict.INCONCLUSIVE, Verdict.INTERRUPTED)


def test_restart_of_a_watched_process_is_counted(tmp_path: Path) -> None:
    from crawler2.soak.collectors import Reading

    clock = Clock()
    cfg = make_config(tmp_path)
    store, run = start_run(cfg, tmp_path, clock)
    run.state.identities.clear()
    for i, ids in enumerate((["1:100"], ["1:100"], ["2:200"], ["2:200"], ["3:300"])):
        run._restarts("svc", Reading(identities={"worker": ids}), clock.time() + i)
    restarts = [e for e in store.read_jsonl("events.jsonl").rows if e["kind"] == "restart"]
    assert [e["service"] for e in restarts] == ["svc.worker", "svc.worker"]
    run.run()
    ev = runner.evaluate_run(store, now=clock.time())
    assert crit(ev, "RESTARTS").status is Status.FAIL, "2 restarts > max_restarts_per_service=1"


# --- invalidation ---------------------------------------------------------------------------------


def test_changed_code_or_config_invalidates_the_run(tmp_path: Path) -> None:
    clock = Clock()
    cfg = make_config(tmp_path)
    store, run = start_run(cfg, tmp_path, clock)
    run.run()
    ckpt, _ = store.load_checkpoint()
    assert ckpt is not None
    ckpt["fingerprints_now"] = {**ckpt["fingerprints_now"], "git:/repo:commit": "deadbeef"}
    run_doc = store.read_json("run.json")
    assert run_doc is not None
    run_doc["fingerprints"]["git:/repo:commit"] = "cafe1234"
    store.write_json("run.json", run_doc)
    store.save_checkpoint(ckpt)
    ev = runner.evaluate_run(store, now=clock.time())
    assert ev.verdict is Verdict.INVALIDATED
    assert "git:/repo:commit" in crit(ev, "FREEZE").detail


def test_watched_file_change_invalidates(tmp_path: Path) -> None:
    watched = tmp_path / "settings.env"
    watched.write_text("A=1")
    clock = Clock()
    cfg = make_config(tmp_path, watch_paths=[str(watched)])
    store, run = start_run(cfg, tmp_path, clock)
    for _ in range(9):
        run.round()
    watched.write_text("A=2")
    for _ in range(11):  # the content hash is re-checked every 10th round
        run.round()
    run.state.phase = "completed"
    store.save_checkpoint(run.state.dump())
    assert runner.evaluate_run(store, now=clock.time()).verdict is Verdict.INVALIDATED


# --- status / reports / hygiene -------------------------------------------------------------------


def test_status_reports_liveness_and_needs_resume(tmp_path: Path) -> None:
    clock = Clock()
    cfg = make_config(tmp_path)
    store, run = start_run(cfg, tmp_path, clock)
    run.round()
    st = runner.status(store, now=clock.time())
    assert st["phase"] == "running"
    assert st["runner_alive"] is False
    assert st["needs_resume"] is True, "phase running but nobody holds the lock"
    assert st["samples"] == 1


def test_html_report_escapes_hostile_strings_and_json_report_has_no_secrets(tmp_path: Path) -> None:
    hostile = '<script>alert("x")</script>'
    opts = series_options()
    cfg = make_config(tmp_path, options=opts, topology={hostile: "<img src=x onerror=1>"})
    clock = Clock()
    store, run = start_run(cfg, tmp_path, clock)
    store.event("note_x", detail=hostile)
    run.run()
    ev = runner.evaluate_run(store, now=clock.time())
    rep = build_report(store, ev)
    page = to_html(rep)
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "<img src=x" not in page
    assert "http" not in page.split("<style>")[0].lower().replace("charset", ""), (
        "no external resources"
    )
    blob = json.dumps(rep)
    assert "password" not in blob.lower() or "***redacted***" in blob or "_env" in blob


def test_secret_values_in_the_config_never_reach_disk(tmp_path: Path) -> None:
    from crawler2.soak.config import Redis

    cfg = make_config(tmp_path, redis=Redis(password_env="SOAK_REDIS_PASSWORD"))
    clock = Clock()
    store, _run = start_run(cfg, tmp_path, clock)
    stored = (store.path / "config.json").read_text()
    assert "SOAK_REDIS_PASSWORD" in stored, "only the variable name is stored"
    assert "hunter2" not in stored


def test_invalid_series_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="pattern"):
        Series(name="Bad Name")


def test_duration_is_the_runs_elapsed_time_not_the_span_between_samples(tmp_path: Path) -> None:
    """A slow first/last round must not turn a completed run into INTERRUPTED."""
    store, _ = run_to_end(tmp_path)
    rows = store.read_jsonl("samples.jsonl").rows
    lines = [json.dumps({**r, "ts": r["ts"]}) for r in rows[3:-3]]  # shrink the sample span a lot
    (store.path / "samples.jsonl").write_text("\n".join(lines) + "\n")
    ev = runner.evaluate_run(store, now=rows[-1]["ts"])
    c = crit(ev, "DURATION")
    assert c.status is Status.PASS
    assert (
        c.evidence["sample_span_s"]
        < c.evidence["required_s"] * 0.999
        <= c.evidence["elapsed_s"] + 1
    )


def test_a_completed_event_earlier_than_the_duration_fails_the_criterion(tmp_path: Path) -> None:
    store, _ = run_to_end(tmp_path)
    events = store.read_jsonl("events.jsonl").rows
    run_doc = store.read_json("run.json")
    assert run_doc is not None
    started = run_doc["started_at"]
    for e in events:
        if e["kind"] == "completed":
            e["ts"] = started + 60
    (store.path / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    ev = runner.evaluate_run(store, now=started + 99999)
    assert crit(ev, "DURATION").status is Status.FAIL
    assert ev.verdict is Verdict.INTERRUPTED


def test_a_dead_runner_that_nobody_resumed_is_interrupted_not_running(tmp_path: Path) -> None:
    clock = Clock()
    cfg = make_config(tmp_path)
    store, run = start_run(cfg, tmp_path, clock)
    for _ in range(3):
        run.round()
        clock.sleep(60)
    soon = runner.evaluate_run(store, now=clock.time())
    assert soon.verdict is Verdict.RUNNING, "heartbeat is fresh enough: could still be resumed"
    later = runner.evaluate_run(store, now=clock.time() + cfg.heartbeat_stale_s + 1)
    assert later.verdict is Verdict.INTERRUPTED
