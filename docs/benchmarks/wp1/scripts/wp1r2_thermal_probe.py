#!/usr/bin/env python3
"""WP1 Run 2 / C1-C4: bounded CPU thermal probe: idle baseline -> load at N workers -> recovery.

For each worker count the probe records, at 1 Hz: CPU package/core temperature, mean frequency,
fan RPM, NVMe and GPU temperature, kernel throttle counters, and workload throughput (float32
matrix multiplications per second, single-threaded BLAS per worker, i.e. an AVX-heavy numeric load
similar to the encoder's CPU path). Recovery time = seconds until the package is within
`--recover-margin` C of the pre-load idle baseline.

Safety (probe policy for a SHORT bounded load; the long-run operational rule of 95 C lives in
wp1r2_hostguard.py): load is stopped on CPU >= --abort-c (default 99 C, i.e. at the hardware
thermal-control limit, TjMax 100 C) for 3 consecutive 1 s polls, NVMe >= 82 C, or GPU >= 95 C.
The CPU reaches its 95 C throttle point within seconds of load on this host (see report). Nothing persistent is changed; workers are always
terminated, even on error.

usage: OMP_NUM_THREADS=1 python3 wp1r2_thermal_probe.py OUT.json --workers 4 8 12 --load-s 75
(run with an interpreter that has numpy, e.g. fingerprinter/env/bin/python)
"""

# ruff: noqa: E501, S607
# (one-off measurement tooling, same convention as the Run 1 scripts: file reads kept simple, fixed argv lists only)
from __future__ import annotations

import argparse
import glob
import json
import multiprocessing as mp
import os
import re
import subprocess
import sys
import time
from typing import Any


def _read(p: str) -> str | None:
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return None


def cpu_temps() -> tuple[float | None, float | None]:
    pkg = core = None
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(d + "/name") != "coretemp":
            continue
        for lab in glob.glob(d + "/temp*_label"):
            lv, v = _read(lab) or "", _read(lab.replace("_label", "_input"))
            if v is None:
                continue
            t = int(v) / 1000
            if lv.startswith("Package"):
                pkg = t
            elif lv.startswith("Core"):
                core = max(core or 0, t)
    return pkg, core


def nvme_temp() -> float | None:
    for p in glob.glob("/sys/class/nvme/nvme0/hwmon*/temp1_input"):
        v = _read(p)
        if v:
            return int(v) / 1000
    return None


def freq_mhz() -> float | None:
    xs = [
        int(v)
        for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")
        if (v := _read(p))
    ]
    return round(sum(xs) / len(xs) / 1000) if xs else None


def fans() -> dict[str, int]:
    out: dict[str, int] = {}
    r = subprocess.run(["sensors"], capture_output=True, text=True, check=False)
    for m in re.finditer(r"^(\w*fan\w*):\s+(\d+) RPM", r.stdout, re.M):
        out[m.group(1)] = int(m.group(2))
    return out


def gpu_temp() -> float | None:
    r = subprocess.run(
        ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=False,
    )
    return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def throttle_counts() -> dict[str, int]:
    base = "/sys/devices/system/cpu/cpu0/thermal_throttle/"
    return {
        k: int(_read(base + k) or 0)
        for k in ("core_throttle_count", "package_throttle_count", "package_throttle_total_time_ms")
    }


def worker(stop: Any, counter: Any, idx: int) -> None:
    import numpy as np

    rng = np.random.default_rng(idx)
    a = rng.standard_normal((768, 768), dtype=np.float32)
    b = rng.standard_normal((768, 768), dtype=np.float32)
    n = 0
    while not stop.is_set():
        a = (a @ b) * np.float32(1e-3) + a
        a /= np.abs(a).max() + np.float32(1e-6)
        n += 1
        if n % 5 == 0:
            with counter.get_lock():
                counter.value += 5


def sample(phase: str, t0: float, ops: int) -> dict[str, Any]:
    pkg, core = cpu_temps()
    return {
        "t": round(time.time() - t0, 1),
        "phase": phase,
        "pkg_c": pkg,
        "hot_core_c": core,
        "freq_mhz": freq_mhz(),
        "nvme_c": nvme_temp(),
        "gpu_c": gpu_temp(),
        "fans": fans(),
        "ops_total": ops,
        "throttle": throttle_counts(),
    }


def run_level(
    n: int, load_s: int, idle_s: int, recover_margin: float, recover_max_s: int, abort_c: float
) -> dict[str, Any]:
    t0 = time.time()
    rows: list[dict[str, Any]] = []
    for _ in range(idle_s):
        rows.append(sample("idle", t0, 0))
        time.sleep(1)
    base_vals = [r["pkg_c"] for r in rows[-10:] if r["pkg_c"] is not None]
    baseline = sorted(base_vals)[len(base_vals) // 2] if base_vals else None
    stop, counter = mp.Event(), mp.Value("l", 0)
    procs = [mp.Process(target=worker, args=(stop, counter, i), daemon=True) for i in range(n)]
    aborted = None
    hot = 0
    ops0 = 0
    try:
        for p in procs:
            p.start()
        load_start = time.time()
        while time.time() - load_start < load_s:
            r = sample("load", t0, counter.value)
            rows.append(r)
            hot = hot + 1 if (r["pkg_c"] or 0) >= abort_c else 0
            if hot >= 3 or (r["nvme_c"] or 0) >= 82 or (r["gpu_c"] or 0) >= 95:
                aborted = {k: r[k] for k in ("t", "pkg_c", "nvme_c", "gpu_c")}
                break
            time.sleep(1)
        ops_load = counter.value - ops0
        load_elapsed = time.time() - load_start
    finally:
        stop.set()
        for p in procs:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()
    rec_start = time.time()
    recovered_s = None
    while time.time() - rec_start < recover_max_s:
        r = sample("recovery", t0, counter.value)
        rows.append(r)
        if (
            baseline is not None
            and r["pkg_c"] is not None
            and r["pkg_c"] <= baseline + recover_margin
            and recovered_s is None
        ):
            recovered_s = round(time.time() - rec_start)
            break
        time.sleep(1)
    load_rows = [r for r in rows if r["phase"] == "load"]
    pk = [r["pkg_c"] for r in load_rows if r["pkg_c"] is not None]
    fr = [r["freq_mhz"] for r in load_rows if r["freq_mhz"]]
    th0: dict[str, int] = load_rows[0]["throttle"] if load_rows else {}
    th1: dict[str, int] = load_rows[-1]["throttle"] if load_rows else {}
    return {
        "workers": n,
        "load_s_requested": load_s,
        "load_s_actual": round(load_elapsed, 1),
        "aborted": aborted,
        "idle_baseline_pkg_c": baseline,
        "ops_per_s": round(ops_load / load_elapsed, 1) if load_elapsed else None,
        "pkg_c_max": max(pk) if pk else None,
        "pkg_c_median": sorted(pk)[len(pk) // 2] if pk else None,
        "pkg_c_last10_median": sorted(pk[-10:])[len(pk[-10:]) // 2] if pk else None,
        "freq_mhz_mean": round(sum(fr) / len(fr)) if fr else None,
        "freq_mhz_last10_mean": round(sum(fr[-10:]) / len(fr[-10:])) if fr else None,
        "throttle_delta": {k: th1.get(k, 0) - th0.get(k, 0) for k in th0},
        "recovery_s_to_baseline_plus_margin": recovered_s,
        "recover_margin_c": recover_margin,
        "samples": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--workers", type=int, nargs="+", default=[4, 8, 12])
    ap.add_argument("--load-s", type=int, default=75)
    ap.add_argument("--idle-s", type=int, default=20)
    ap.add_argument("--recover-margin", type=float, default=5.0)
    ap.add_argument("--recover-max-s", type=int, default=240)
    ap.add_argument("--start-below-c", type=float, default=55.0)
    ap.add_argument("--abort-c", type=float, default=99.0)
    a = ap.parse_args()
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    results = []
    for n in a.workers:
        t0 = time.time()
        while (cpu_temps()[0] or 99) > a.start_below_c:
            if time.time() - t0 > 600:
                print("cannot cool to start temperature")
                return 4
            time.sleep(5)
        res = run_level(n, a.load_s, a.idle_s, a.recover_margin, a.recover_max_s, a.abort_c)
        results.append(res)
        print(json.dumps({k: v for k, v in res.items() if k != "samples"}), flush=True)
        if res["aborted"]:
            break
    with open(a.out, "w") as f:
        json.dump({"levels": results, "ncpu": os.cpu_count()}, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
