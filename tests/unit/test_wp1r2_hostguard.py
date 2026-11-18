"""Unit tests for the WP1 Run 2 host guard (pure evaluators; no device, Docker or disk access)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[2] / "docs/benchmarks/wp1/scripts/wp1r2_hostguard.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wp1r2_hostguard", _PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hg: Any = _load()

HEALTHY: dict[str, Any] = {
    "critical_warning": 0,
    "avail_spare": 100,
    "spare_thresh": 10,
    "percent_used": 8,
    "media_errors": 0,
    "num_err_log_entries": 0,
    "warning_temp_time": 0,
    "critical_comp_time": 0,
    "thm_temp1_trans_count": 10,
    "thm_temp2_trans_count": 1,
    "thm_temp1_total_time": 5,
    "thm_temp2_total_time": 1,
}


def _by(checks: list[dict[str, Any]]) -> dict[str, str]:
    return {c["name"]: c["status"] for c in checks}


def test_healthy_nvme_is_ok() -> None:
    c = hg.eval_nvme(dict(HEALTHY), None, 50.0)
    assert hg.overall(c) == "OK"


def test_missing_smart_is_unknown_not_ok() -> None:
    c = hg.eval_nvme(None, "permission_denied", 50.0)
    s = _by(c)
    assert s["critical_warning"] == "UNKNOWN"
    assert s["media_errors"] == "UNKNOWN"
    assert hg.overall(c) == "UNKNOWN"
    assert "permission_denied" in c[1]["detail"]


def test_absent_field_is_unknown_but_zero_is_ok() -> None:
    smart = dict(HEALTHY)
    del smart["media_errors"]
    s = _by(hg.eval_nvme(smart, None, 50.0))
    assert s["media_errors"] == "UNKNOWN"
    assert s["error_log_entries"] == "OK"


@pytest.mark.parametrize(
    ("t", "want"), [(60.0, "OK"), (75.0, "WARN"), (80.9, "WARN"), (81.0, "FAIL")]
)
def test_nvme_temperature_levels(t: float, want: str) -> None:
    assert _by(hg.eval_nvme(dict(HEALTHY), None, t))["nvme_temp_hwmon"] == want


def test_unavailable_temperature_is_unknown() -> None:
    assert _by(hg.eval_nvme(dict(HEALTHY), None, None))["nvme_temp_hwmon"] == "UNKNOWN"


def test_media_error_and_critical_warning_fail() -> None:
    smart = dict(HEALTHY, media_errors=2, critical_warning=4)
    s = _by(hg.eval_nvme(smart, None, 40.0))
    assert s["media_errors"] == "FAIL"
    assert s["critical_warning"] == "FAIL"


def test_spare_below_threshold_fails() -> None:
    assert _by(hg.eval_nvme(dict(HEALTHY, avail_spare=9), None, 40.0))["available_spare"] == "FAIL"


def test_thermal_events_delta_detects_new_throttling() -> None:
    cur = dict(HEALTHY, thm_temp1_trans_count=15)
    s = _by(hg.eval_nvme(cur, None, 40.0, prev=dict(HEALTHY)))
    assert s["thermal_events"] == "WARN"
    assert (
        _by(hg.eval_nvme(dict(HEALTHY), None, 40.0, prev=dict(HEALTHY)))["thermal_events"] == "OK"
    )


def test_critical_time_delta_fails() -> None:
    cur = dict(HEALTHY, critical_comp_time=3)
    assert _by(hg.eval_nvme(cur, None, 40.0, prev=dict(HEALTHY)))["thermal_events"] == "FAIL"


@pytest.mark.parametrize(
    ("free", "planned", "want"),
    [
        (60.0, 0.0, "OK"),
        (60.0, 35.0, "WARN"),
        (60.0, 40.1, "FAIL"),
        (60.0, 40.0, "WARN"),
        (21.0, 0.0, "WARN"),
        (19.9, 0.0, "FAIL"),
    ],
)
def test_storage_preflight_with_simulated_capacity(free: float, planned: float, want: str) -> None:
    c = hg.eval_storage(free, planned)
    assert c[0]["status"] == want
    assert ("REFUSE" in c[0]["detail"]) == (want == "FAIL")


def test_storage_unknown_when_statvfs_fails() -> None:
    assert hg.eval_storage(None)[0]["status"] == "UNKNOWN"


def test_thermal_levels() -> None:
    s = _by(hg.eval_thermal(96.0, 88.0, None))
    assert s == {"cpu_package_temp": "FAIL", "gpu_temp": "WARN", "nvme_temp": "UNKNOWN"}


def test_container_checks() -> None:
    rows = [
        {
            "name": "a",
            "oom_killed": True,
            "restarts": 0,
            "health": "healthy",
            "mem_bytes": 1.0,
            "mem_limit_bytes": 10.0,
        },
        {
            "name": "b",
            "oom_killed": False,
            "restarts": 2,
            "health": "unhealthy",
            "mem_bytes": 9.6,
            "mem_limit_bytes": 10.0,
        },
        {
            "name": "c",
            "oom_killed": False,
            "restarts": 0,
            "health": "none",
            "mem_bytes": None,
            "mem_limit_bytes": None,
        },
    ]
    s = _by(hg.eval_containers(rows))
    assert s["a:oom_killed"] == "FAIL"
    assert s["b:restarts"] == "WARN"
    assert s["b:health"] == "FAIL"
    assert s["b:mem_pct_of_limit"] == "FAIL"
    assert s["c:mem_pct_of_limit"] == "UNKNOWN"


def test_overall_ordering_and_empty() -> None:
    def mk(st: str) -> dict[str, str]:
        return {"status": st}

    assert hg.overall([mk("OK"), mk("WARN")]) == "WARN"
    assert hg.overall([mk("WARN"), mk("UNKNOWN")]) == "UNKNOWN"
    assert hg.overall([mk("UNKNOWN"), mk("FAIL")]) == "FAIL"
    assert hg.overall([]) == "UNKNOWN"


def test_cli_storage_json_and_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    rc = hg.main(["storage", "--planned-gib", "100000"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert rep["overall"] == "FAIL"
    assert rep["exit_code"] == 2


def test_policy_values_unchanged_floor() -> None:
    assert hg.POLICY["min_free_gib"] == 20.0


def test_cpu_single_boost_spike_is_warn_not_fail() -> None:
    c = hg.eval_cpu_sustained([55.0, 56.0, 95.0, 57.0, 55.0])
    assert c[0]["status"] == "WARN"


def test_cpu_sustained_heat_fails() -> None:
    assert hg.eval_cpu_sustained([60.0, 95.0, 96.0, 97.0, 70.0])[0]["status"] == "FAIL"


def test_cpu_tjmax_single_sample_fails() -> None:
    assert hg.eval_cpu_sustained([60.0, 100.0])[0]["status"] == "FAIL"


def test_cpu_no_samples_unknown_and_cool_ok() -> None:
    assert hg.eval_cpu_sustained([])[0]["status"] == "UNKNOWN"
    assert hg.eval_cpu_sustained([50.0, 52.0, 51.0])[0]["status"] == "OK"


@pytest.mark.parametrize(("copies", "measured_gib"), [(1, 0.4215), (2, 0.656), (3, 0.8915)])
def test_qdrant_model_reproduces_measurements(copies: int, measured_gib: float) -> None:
    n = 104_443 * copies
    est = hg.estimate_qdrant_peak_anon_gib(hg.qdrant_resident_required(n))
    assert abs(est - measured_gib) / measured_gib < 0.03


def test_qdrant_six_copies_refused_and_provisional_cap_allowed() -> None:
    assert hg.eval_qdrant_build(626_658)[0]["status"] == "FAIL"  # the OOM-killed scale
    assert hg.eval_qdrant_build(250_000)[0]["status"] in (
        "OK",
        "WARN",
    )  # provisional index.max_points
    assert hg.eval_qdrant_build(209_000)[0]["status"] == "OK"


def test_qdrant_r_req_matches_harness_values() -> None:
    assert hg.qdrant_resident_required(104_443) == 234_109
    assert hg.qdrant_resident_required(626_658) == 1_304_649
