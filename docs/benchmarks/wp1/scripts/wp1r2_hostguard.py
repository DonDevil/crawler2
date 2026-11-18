#!/usr/bin/env python3
"""WP1 Run 2 host guard: read-only, machine-readable host/NVMe/storage/thermal/container checks.

Design rules (see results/run-2/automation/README.md):
  * Read-only. Never changes device, kernel, Docker or filesystem state.
  * Every check yields OK | WARN | FAIL | UNKNOWN. Missing telemetry is UNKNOWN, never OK.
  * Overall = worst of FAIL > UNKNOWN > WARN > OK.
    Exit codes: 0 OK, 1 WARN, 2 FAIL, 3 UNKNOWN.
  * Thresholds are *policy* values with a documented source (see POLICY); they are not benchmark
    acceptance gates and must never be edited to make a performance gate pass.

Usage:
  wp1r2_hostguard.py nvme       [--sudo] [--baseline prev.json]
  wp1r2_hostguard.py storage    [--planned-gib N] [--path /]
  wp1r2_hostguard.py thermal
  wp1r2_hostguard.py containers
  wp1r2_hostguard.py preflight  [--planned-gib N] [--sudo]     (all of the above)
All subcommands accept --out FILE (JSON is always printed to stdout as well).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

OK, WARN, FAIL, UNKNOWN = "OK", "WARN", "FAIL", "UNKNOWN"
_RANK = {OK: 0, WARN: 1, UNKNOWN: 2, FAIL: 3}
_EXIT = {OK: 0, WARN: 1, FAIL: 2, UNKNOWN: 3}

# Safety / operational policy thresholds. Each has a documented origin; none is a performance gate.
POLICY: dict[str, float] = {
    # NVMe composite temperature (deg C). Origin: Samsung MZVLQ512HBLU controller: host-controlled
    # thermal management TMT1=81, TMT2=83; WCTEMP=83, CCTEMP=85; hwmon max 82.85 / crit 84.85.
    "nvme_warn_c": 75.0,  # operational: approaching TMT1 throttling (81)
    "nvme_fail_c": 81.0,  # safety: controller is (or is about to be) thermally throttling
    # CPU package (deg C). Origin: i5-11400H TjMax 100; Run 1 monitor hard limit 95.
    "cpu_warn_c": 90.0,
    "cpu_fail_c": 95.0,
    # GPU (deg C). Origin: RTX 2050 Laptop; Run 1 observed SW thermal slowdown near 87.
    "gpu_warn_c": 87.0,
    "gpu_fail_c": 95.0,
    # Storage. Origin: task safety floor.
    "min_free_gib": 20.0,
    "warn_free_gib": 30.0,
    # NVMe wear / spare. Origin: NVMe available_spare_threshold semantics; project wear policy.
    "percent_used_warn": 80.0,
    "percent_used_fail": 100.0,
    # Container memory pressure (% of limit, working set).
    "ctr_mem_warn_pct": 85.0,
    "ctr_mem_fail_pct": 95.0,
}


def chk(
    name: str, status: str, value: Any, threshold: Any, detail: str, source: str
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "value": value,
        "threshold": threshold,
        "detail": detail,
        "source": source,
    }


def overall(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return UNKNOWN
    return max((c["status"] for c in checks), key=lambda s: _RANK[s])


def _hi(
    name: str, v: float | None, warn: float, fail: float, src: str, unit: str
) -> dict[str, Any]:
    if v is None:
        return chk(name, UNKNOWN, None, {"warn": warn, "fail": fail}, "telemetry unavailable", src)
    st = FAIL if v >= fail else WARN if v >= warn else OK
    return chk(name, st, v, {"warn": warn, "fail": fail}, f"{v:g}{unit}", src)


# ---------------------------------------------------------------- pure evaluators (unit-tested)
def eval_nvme(
    smart: dict[str, Any] | None,
    smart_err: str | None,
    hwmon_temp_c: float | None,
    prev: dict[str, Any] | None = None,
    policy: dict[str, float] = POLICY,
) -> list[dict[str, Any]]:
    """smart = `nvme smart-log -o json`. smart_err in {permission_denied, tool_missing, <other>}."""
    out: list[dict[str, Any]] = [
        _hi(
            "nvme_temp_hwmon",
            hwmon_temp_c,
            policy["nvme_warn_c"],
            policy["nvme_fail_c"],
            "sysfs hwmon (no root)",
            " C",
        )
    ]
    if smart is None:
        why = smart_err or "unknown"
        for n in (
            "critical_warning",
            "available_spare",
            "percentage_used",
            "media_errors",
            "error_log_entries",
            "thermal_events",
        ):
            out.append(chk(n, UNKNOWN, None, None, f"SMART unavailable: {why}", "nvme smart-log"))
        return out
    src = "nvme smart-log -o json"
    cw = smart.get("critical_warning")
    out.append(
        chk(
            "critical_warning",
            UNKNOWN if cw is None else (OK if cw == 0 else FAIL),
            cw,
            0,
            "bitfield; 0 = none",
            src,
        )
    )
    sp, th = smart.get("avail_spare"), smart.get("spare_thresh")
    if sp is None or th is None:
        out.append(chk("available_spare", UNKNOWN, sp, th, "field absent", src))
    else:
        out.append(chk("available_spare", OK if sp > th else FAIL, sp, f"> {th}", f"{sp}%", src))
    pu = smart.get("percent_used")
    out.append(
        _hi(
            "percentage_used",
            None if pu is None else float(pu),
            policy["percent_used_warn"],
            policy["percent_used_fail"],
            src,
            " %",
        )
    )
    for key, name in (
        ("media_errors", "media_errors"),
        ("num_err_log_entries", "error_log_entries"),
    ):
        v = smart.get(key)
        out.append(
            chk(
                name,
                UNKNOWN if v is None else (OK if v == 0 else FAIL),
                v,
                0,
                "0 expected (a zero is a real zero only because the field is present)",
                src,
            )
        )
    # Thermal events: cumulative counters, only judged relative to a previous reading.
    keys = (
        "warning_temp_time",
        "critical_comp_time",
        "thm_temp1_trans_count",
        "thm_temp2_trans_count",
        "thm_temp1_total_time",
        "thm_temp2_total_time",
    )
    cur = {k: smart.get(k) for k in keys}
    if any(v is None for v in cur.values()):
        out.append(chk("thermal_events", UNKNOWN, cur, None, "one or more counters absent", src))
    elif prev is None:
        st = (
            FAIL
            if (cur["critical_comp_time"] or 0) > 0
            else WARN
            if (cur["warning_temp_time"] or 0) > 0
            else OK
        )
        out.append(
            chk(
                "thermal_events",
                st,
                cur,
                "no baseline: cumulative lifetime counters",
                "lifetime counters only; pass --baseline to detect new events",
                src,
            )
        )
    else:
        d = {k: (cur[k] or 0) - (prev.get(k) or 0) for k in keys}
        st = (
            FAIL
            if d["critical_comp_time"] > 0
            else WARN
            if (
                d["warning_temp_time"] > 0
                or d["thm_temp1_trans_count"] > 0
                or d["thm_temp2_trans_count"] > 0
            )
            else OK
        )
        out.append(
            chk(
                "thermal_events",
                st,
                {"current": cur, "delta": d},
                "delta == 0",
                "new thermal-management transitions since baseline" if st != OK else "none new",
                src,
            )
        )
    return out


def eval_storage(
    free_gib: float | None, planned_gib: float = 0.0, policy: dict[str, float] = POLICY
) -> list[dict[str, Any]]:
    if free_gib is None:
        return [
            chk("free_space", UNKNOWN, None, policy["min_free_gib"], "statvfs failed", "os.statvfs")
        ]
    after = free_gib - planned_gib
    floor, warn = policy["min_free_gib"], policy["warn_free_gib"]
    st = FAIL if after < floor else WARN if after < warn else OK
    d = f"free {free_gib:.1f} GiB - planned {planned_gib:.1f} GiB = {after:.1f} GiB after"
    return [
        chk(
            "free_space_after_operation",
            st,
            round(after, 2),
            {"fail_below": floor, "warn_below": warn},
            d + ("; REFUSE operation" if st == FAIL else ""),
            "os.statvfs",
        )
    ]


def eval_thermal(
    cpu: float | None, gpu: float | None, nvme: float | None, policy: dict[str, float] = POLICY
) -> list[dict[str, Any]]:
    return [
        _hi(
            "cpu_package_temp",
            cpu,
            policy["cpu_warn_c"],
            policy["cpu_fail_c"],
            "sysfs coretemp / lm-sensors",
            " C",
        ),
        _hi("gpu_temp", gpu, policy["gpu_warn_c"], policy["gpu_fail_c"], "nvidia-smi", " C"),
        _hi("nvme_temp", nvme, policy["nvme_warn_c"], policy["nvme_fail_c"], "sysfs hwmon", " C"),
    ]


def eval_cpu_sustained(
    samples: list[float], policy: dict[str, float] = POLICY, n_consecutive: int = 3
) -> list[dict[str, Any]]:
    """Judge CPU package temperature over a window of ~1 Hz samples (oldest first).

    A single boost spike to 85-95 C is normal on this host even at idle, so one sample is not heat
    soak. FAIL: >= cpu_fail_c for n_consecutive polls in a row, or any sample >= 100 (TjMax).
    WARN: any spike >= cpu_fail_c, or the window median >= cpu_warn_c. UNKNOWN: no samples.
    """
    src = "sysfs coretemp, 1 Hz window"
    if not samples:
        return [chk("cpu_package_sustained", UNKNOWN, None, None, "no samples", src)]
    run = best = 0
    for v in samples:
        run = run + 1 if v >= policy["cpu_fail_c"] else 0
        best = max(best, run)
    med = sorted(samples)[len(samples) // 2]
    st = (
        FAIL
        if best >= n_consecutive or max(samples) >= 100.0
        else WARN
        if max(samples) >= policy["cpu_fail_c"] or med >= policy["cpu_warn_c"]
        else OK
    )
    val = {"max": max(samples), "median": med, "longest_run_ge_fail": best, "n": len(samples)}
    return [
        chk("cpu_package_sustained", st, val, {"fail_run": n_consecutive, "tjmax": 100.0}, "", src)
    ]


# Qdrant anon-memory model for the E-R1 gate configuration (int8 always_ram, m=32, on-disk
# originals/payload, 1.5 GiB cgroup). Linear fit of measured peak anon memory vs peak resident
# points R:
#   1x: R=234,109 -> 0.434 GiB (mean of 0.437/0.431)
#   2x: R=448,268 -> 0.662     3x: R=662,362 -> 0.903
# slope = (0.903-0.434)/(662,362-234,109) GiB/point (~1.18 KB/point), intercept 0.178 GiB; the 2x
# point is reproduced within 1.1 %. Extrapolated 6x (R=1,304,649) -> 1.61 GiB > 1.5 GiB limit,
# consistent with the observed OOM-kill. Valid for THIS collection configuration only; refit if it
# changes.
QDRANT_ANON_SLOPE_GIB_PER_POINT = (0.903 - 0.434) / (662_362 - 234_109)
QDRANT_ANON_INTERCEPT_GIB = 0.434 - QDRANT_ANON_SLOPE_GIB_PER_POINT * 234_109
QDRANT_MEDIA_RESIDENT = 10_000
QDRANT_MARGIN_POINTS = 10_000


def qdrant_resident_required(n_target_points: int) -> int:
    """E-R1 R_req(N): resident points while two generations coexist (see capacity_e_r1.py)."""
    return (
        (n_target_points + QDRANT_MEDIA_RESIDENT)
        + -(-105 * n_target_points // 100)
        + QDRANT_MARGIN_POINTS
    )


def estimate_qdrant_peak_anon_gib(resident_points: int) -> float:
    return QDRANT_ANON_INTERCEPT_GIB + QDRANT_ANON_SLOPE_GIB_PER_POINT * resident_points


def eval_qdrant_build(
    n_target_points: int,
    anon_rule_gib: float = 1.35,
    margin: float = 1.10,
    container_limit_gib: float = 1.5,
) -> list[dict[str, Any]]:
    """Refuse/defer a rebuild whose predicted peak anon memory (x margin) exceeds the budget."""
    r = qdrant_resident_required(n_target_points)
    est = estimate_qdrant_peak_anon_gib(r)
    budget = min(anon_rule_gib, container_limit_gib)
    st = FAIL if est * margin > budget else WARN if est * margin > 0.85 * budget else OK
    return [
        chk(
            "qdrant_rebuild_memory_budget",
            st,
            {
                "n_target_points": n_target_points,
                "resident_required": r,
                "predicted_peak_anon_gib": round(est, 3),
            },
            {"budget_gib": budget, "margin": margin},
            "REFUSE/DEFER build: predicted peak anon exceeds budget" if st == FAIL else "",
            "linear fit of E-R1 1x/2x/3x measurements (memory only; latency is a separate gate)",
        )
    ]


def eval_containers(
    rows: list[dict[str, Any]], policy: dict[str, float] = POLICY
) -> list[dict[str, Any]]:
    """rows: {name, oom_killed, restarts, health, mem_bytes, mem_limit_bytes}; None = n/a."""
    out: list[dict[str, Any]] = []
    for r in rows:
        n, src = r["name"], "docker inspect/stats"
        out.append(
            chk(
                f"{n}:oom_killed",
                UNKNOWN if r.get("oom_killed") is None else (FAIL if r["oom_killed"] else OK),
                r.get("oom_killed"),
                False,
                "",
                src,
            )
        )
        rc = r.get("restarts")
        out.append(
            chk(
                f"{n}:restarts", UNKNOWN if rc is None else (WARN if rc > 0 else OK), rc, 0, "", src
            )
        )
        h = r.get("health")
        out.append(
            chk(
                f"{n}:health",
                OK if h in ("healthy", "none") else UNKNOWN if h is None else FAIL,
                h,
                "healthy",
                "'none' = container has no healthcheck",
                src,
            )
        )
        m, lim = r.get("mem_bytes"), r.get("mem_limit_bytes")
        pct = None if (m is None or not lim) else 100.0 * m / lim
        out.append(
            _hi(
                f"{n}:mem_pct_of_limit",
                pct,
                policy["ctr_mem_warn_pct"],
                policy["ctr_mem_fail_pct"],
                src,
                " %",
            )
        )
    return out


# ---------------------------------------------------------------- collectors (side-effect free)
def _run(argv: list[str], timeout: int = 20) -> tuple[int, str, str]:
    try:
        p = subprocess.run(  # noqa: S603 - fixed argv lists, never shell=True
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "tool_missing"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def read_hwmon_nvme_c(dev: str = "nvme0") -> float | None:
    for p in sorted(glob.glob(f"/sys/class/nvme/{dev}/hwmon*/temp1_input")):
        try:
            return int(Path(p).read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return None


def read_smart(dev: str, use_sudo: bool) -> tuple[dict[str, Any] | None, str | None]:
    if shutil.which("nvme") is None:
        return None, "tool_missing"
    argv = ["nvme", "smart-log", f"/dev/{dev}", "-o", "json"]
    if use_sudo:
        argv = ["sudo", "-A" if os.environ.get("SUDO_ASKPASS") else "-n", *argv]
    rc, so, se = _run(argv)
    if rc == 0:
        try:
            return json.loads(so), None
        except json.JSONDecodeError:
            return None, "unparseable_output"
    low = se.lower()
    if "permission" in low or "a password is required" in low or "sudo:" in low:
        return None, "permission_denied"
    return None, f"command_failed rc={rc}: {se.strip()[:120]}"


def read_cpu_pkg_c() -> float | None:
    best: float | None = None
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if Path(f"{d}/name").read_text().strip() != "coretemp":
                continue
            for lab in glob.glob(f"{d}/temp*_label"):
                if Path(lab).read_text().strip().startswith("Package"):
                    best = int(Path(lab.replace("_label", "_input")).read_text()) / 1000.0
        except (OSError, ValueError):
            continue
    return best


def read_gpu_c() -> float | None:
    rc, so, _ = _run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"])
    if rc != 0:
        return None
    m = re.match(r"\s*(\d+)", so)
    return float(m.group(1)) if m else None


def free_gib(path: str) -> float | None:
    try:
        s = os.statvfs(path)
    except OSError:
        return None
    return s.f_bavail * s.f_frsize / 2**30


def _parse_size(s: str) -> float | None:
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", s.strip())
    if not m:
        return None
    mult = {
        "B": 1,
        "KB": 1e3,
        "MB": 1e6,
        "GB": 1e9,
        "KiB": 2**10,
        "MiB": 2**20,
        "GiB": 2**30,
        "TiB": 2**40,
    }
    return float(m.group(1)) * mult.get(m.group(2), float("nan"))


def read_containers() -> list[dict[str, Any]] | None:
    rc, so, _ = _run(["docker", "ps", "-q"])
    if rc != 0:
        return None
    ids = so.split()
    if not ids:
        return []
    fmt = (
        "{{.Name}}|{{.State.OOMKilled}}|{{.RestartCount}}|"
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
    )
    rc, so, _ = _run(["docker", "inspect", "--format", fmt, *ids])
    if rc != 0:
        return None
    rows = {}
    for ln in so.strip().splitlines():
        n, oom, rs, h = ln.split("|")
        rows[n.lstrip("/")] = {
            "name": n.lstrip("/"),
            "oom_killed": oom == "true",
            "restarts": int(rs),
            "health": h,
            "mem_bytes": None,
            "mem_limit_bytes": None,
        }
    rc, so, _ = _run(["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.MemUsage}}"])
    if rc == 0:
        for ln in so.strip().splitlines():
            n, mu = ln.split("|")
            if n in rows and " / " in mu:
                a, b = mu.split(" / ")
                rows[n]["mem_bytes"], rows[n]["mem_limit_bytes"] = _parse_size(a), _parse_size(b)
    return list(rows.values())


# ---------------------------------------------------------------- CLI
def build_report(kind: str, args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if kind in ("nvme", "preflight"):
        smart, err = read_smart(args.device, args.sudo)
        prev = None
        if getattr(args, "baseline", None):
            try:
                prev = json.loads(Path(args.baseline).read_text())
                prev = prev.get("raw_smart", prev)
            except (OSError, json.JSONDecodeError):
                prev = None
        checks += eval_nvme(smart, err, read_hwmon_nvme_c(args.device), prev)
    if kind == "qdrant-build":
        checks += eval_qdrant_build(args.target_points)
    if kind in ("storage", "preflight"):
        checks += eval_storage(free_gib(args.path), args.planned_gib)
    if kind in ("thermal", "preflight"):
        win: list[float] = []
        for _ in range(max(1, args.window_s)):
            v = read_cpu_pkg_c()
            if v is not None:
                win.append(v)
            time.sleep(1.0 if args.window_s > 1 else 0)
        checks += eval_thermal(None, read_gpu_c(), read_hwmon_nvme_c(args.device))[1:]
        checks += eval_cpu_sustained(win)
    if kind in ("containers", "preflight"):
        rows = read_containers()
        checks += (
            eval_containers(rows)
            if rows is not None
            else [chk("containers", UNKNOWN, None, None, "docker unavailable", "docker")]
        )
    st = overall(checks)
    rep: dict[str, Any] = {
        "tool": "wp1r2_hostguard",
        "kind": kind,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "overall": st,
        "exit_code": _EXIT[st],
        "policy": POLICY,
        "checks": checks,
    }
    if kind in ("nvme", "preflight"):
        rep["raw_smart"] = smart
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "kind", choices=["nvme", "storage", "thermal", "containers", "preflight", "qdrant-build"]
    )
    ap.add_argument(
        "--target-points", type=int, default=250_000, help="qdrant-build: N per generation"
    )
    ap.add_argument("--device", default="nvme0")
    ap.add_argument(
        "--sudo",
        action="store_true",
        help="run SMART via sudo (-A if SUDO_ASKPASS is set, else -n needing a cached ticket)",
    )
    ap.add_argument("--baseline", help="previous nvme/preflight JSON, to detect new thermal events")
    ap.add_argument(
        "--planned-gib", type=float, default=0.0, help="storage the operation intends to consume"
    )
    ap.add_argument("--path", default="/")
    ap.add_argument(
        "--window-s", type=int, default=5, help="CPU sampling window (1 Hz) for thermal"
    )
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    rep = build_report(a.kind, a)
    txt = json.dumps(rep, indent=2, sort_keys=True)
    if a.out:
        Path(a.out).write_text(txt + "\n")
    print(txt)
    return int(rep["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
