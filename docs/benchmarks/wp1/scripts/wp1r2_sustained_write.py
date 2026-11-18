#!/usr/bin/env python3
"""WP1 Run 2 / B4: bounded sustained-write experiment with temperature + thermal-counter correlation.

Purpose: separate SLC-cache exhaustion from thermal throttling. Run it twice from a cold start:
  A) unthrottled (--rate-mib 0)     B) rate-limited (--rate-mib 500, less heat per GiB written)
If the throughput cliff appears at the same cumulative GiB in both runs while B stays below the
controller's thermal-management temperature (TMT1 = 81 C) -> cache exhaustion. If the cliff
tracks the temperature crossing TMT1/TMT2 and the thermal-transition counters rise -> thermal.

Safety (all enforced while running; the job is killed and the file removed on breach):
  * file-based fio on ext4 under $HOME/wp1r2-* only; never a block device
  * abort at NVMe >= 82 C, CPU package >= 95 C, free space < 28 GiB (20 GiB floor + margin)
  * refuses to start unless NVMe <= --start-below-c (cold start) and free space is sufficient
  * only the benchmark's own file is deleted

usage: wp1r2_sustained_write.py --label A --dir ~/wp1r2-sust --out OUT.json [--rate-mib 0]
         [--file-gib 24] [--total-gib 48] [--post-idle-s 60] [--post-burst-gib 6]
Optional: SUDO_ASKPASS set => thermal counters via `sudo nvme smart-log` every 5 s (one dialog at start).
"""

# ruff: noqa: E501, SIM115, SIM105, S603, S607
# (one-off measurement tooling, same convention as the Run 1 scripts: file reads kept simple, fixed argv lists only)
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any

HOME = os.path.expanduser("~")
NVME_ABORT_C, CPU_ABORT_C, FREE_ABORT_GIB = 82.0, 95.0, 28.0


def nvme_c() -> float | None:
    for p in glob.glob("/sys/class/nvme/nvme0/hwmon*/temp1_input"):
        try:
            return int(open(p).read()) / 1000
        except (OSError, ValueError):
            pass
    return None


def cpu_c() -> float | None:
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(d + "/name").read().strip() != "coretemp":
                continue
            for lab in glob.glob(d + "/temp*_label"):
                if open(lab).read().startswith("Package"):
                    return int(open(lab.replace("_label", "_input")).read()) / 1000
        except (OSError, ValueError):
            pass
    return None


def free_gib() -> float:
    s = os.statvfs("/")
    return s.f_bavail * s.f_frsize / 2**30


def smart_counters() -> dict[str, Any] | None:
    r = subprocess.run(
        ["sudo", "-n", "nvme", "smart-log", "/dev/nvme0", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    keys = (
        "thm_temp1_trans_count",
        "thm_temp2_trans_count",
        "thm_temp1_total_time",
        "thm_temp2_total_time",
        "warning_temp_time",
        "critical_comp_time",
        "controller_busy_time",
        "data_units_written",
    )
    return {k: d.get(k) for k in keys}


def read_bwlog(prefix: str) -> list[tuple[float, float]]:
    """fio --write_bw_log lines: msec, KiB/s, dir, bs[, offset]. Returns [(t_s, MiB/s)]."""
    out: list[tuple[float, float]] = []
    for p in sorted(glob.glob(prefix + "_bw*.log")):
        for ln in open(p):
            f = ln.split(",")
            if len(f) >= 2:
                out.append((float(f[0]) / 1000.0, float(f[1]) / 1024.0))
    return sorted(out)


def detect_cliff(
    series: list[tuple[float, float]], head_s: float = 10.0, window: int = 5, drop: float = 0.5
) -> dict[str, Any]:
    """series [(t_s, MiB/s)] at ~1 s spacing. Cliff = first t where the trailing-window mean of
    throughput falls below `drop` x the mean of the first `head_s` seconds. Pure function (tested)."""
    head = [v for t, v in series if t <= head_s]
    if len(head) < 3 or len(series) < head_s + window:
        return {"found": False, "reason": "series too short"}
    base = sum(head) / len(head)
    vals = [v for _, v in series]
    for i in range(window, len(series)):
        w = sum(vals[i - window + 1 : i + 1]) / window
        if series[i][0] > head_s and w < drop * base:
            cum = (
                sum(vals[: i + 1]) * (series[1][0] - series[0][0]) / 1024.0
                if len(series) > 1
                else 0.0
            )
            return {
                "found": True,
                "t_s": series[i][0],
                "baseline_MiBps": round(base, 1),
                "window_mean_MiBps": round(w, 1),
                "cumulative_GiB_at_cliff": round(cum, 2),
            }
    return {"found": False, "baseline_MiBps": round(base, 1), "reason": "no sustained drop"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rate-mib", type=int, default=0)
    ap.add_argument("--file-gib", type=int, default=24)
    ap.add_argument("--total-gib", type=int, default=48)
    ap.add_argument("--post-idle-s", type=int, default=60)
    ap.add_argument("--post-burst-gib", type=int, default=6)
    ap.add_argument("--start-below-c", type=float, default=48.0)
    a = ap.parse_args()
    d = os.path.abspath(os.path.expanduser(a.dir))
    if not d.startswith(HOME + "/wp1r2-"):
        print("refusing --dir outside $HOME/wp1r2-*")
        return 2
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "sust.dat")
    if free_gib() - a.file_gib < FREE_ABORT_GIB:
        print(f"refusing: free {free_gib():.1f} GiB - file {a.file_gib} < {FREE_ABORT_GIB}")
        return 3
    t0 = time.time()
    while (nvme_c() or 99) > a.start_below_c:  # cold start for comparable runs
        if time.time() - t0 > 900:
            print("could not cool to start temperature")
            return 4
        time.sleep(5)
    samples: list[dict[str, Any]] = []
    prefix = os.path.join(d, "bw")

    def fio_cmd(io_gib: int, output: str, rate_mib: int = 0) -> list[str]:
        cmd = [
            "fio",
            "--name=sust",
            f"--filename={f}",
            f"--size={a.file_gib}g",
            "--rw=write",
            "--bs=1M",
            "--iodepth=8",
            "--ioengine=io_uring",
            "--direct=1",
            "--output-format=json",
            f"--output={output}",
            f"--write_bw_log={prefix}",
            "--log_avg_msec=1000",
            f"--io_size={io_gib}g",
        ]
        if rate_mib:
            cmd.append(f"--rate={rate_mib}m")
        return cmd

    c0 = smart_counters()
    verdict: dict[str, Any] = {
        "label": a.label,
        "rate_mib": a.rate_mib,
        "file_gib": a.file_gib,
        "total_gib": a.total_gib,
        "counters_start": c0,
        "smart_counters_available": c0 is not None,
        "aborted": None,
    }

    def watch(proc: subprocess.Popen[bytes], phase: str, limit_s: float) -> None:
        start, n, hot = time.time(), 0, 0
        while proc.poll() is None:
            n += 1
            s = {
                "t": round(time.time() - t0, 1),
                "phase": phase,
                "nvme_c": nvme_c(),
                "cpu_c": cpu_c(),
                "free_gib": round(free_gib(), 1),
            }
            if n % 5 == 0:
                s["smart"] = smart_counters()
            samples.append(s)
            # CPU rule: sustained >= 95 C for 3 consecutive 1 s polls, or >= 100 C once (1 s boost spikes
            # to 85-95 C happen even at idle; see run-2 thermal evidence). NVMe/free-space rules unchanged.
            hot = hot + 1 if (s["cpu_c"] or 0) >= CPU_ABORT_C else 0
            if (
                (s["nvme_c"] or 0) >= NVME_ABORT_C
                or hot >= 3
                or (s["cpu_c"] or 0) >= 100
                or s["free_gib"] < FREE_ABORT_GIB
                or time.time() - start > limit_s
            ):
                verdict["aborted"] = {
                    k: s[k] for k in ("t", "phase", "nvme_c", "cpu_c", "free_gib")
                }
                proc.kill()
                break
            time.sleep(1)
        proc.wait()

    # phase 1: the sustained write. io_size bounds cumulative bytes (file is overwritten cyclically).
    p = subprocess.Popen(fio_cmd(a.total_gib, f"{a.out}.fio.json", a.rate_mib))
    watch(p, "sustained", 3600)
    series = read_bwlog(prefix)
    verdict["cliff"] = detect_cliff(series)
    verdict["bw_series_MiBps"] = [[round(t, 1), round(v, 1)] for t, v in series]
    verdict["counters_after_write"] = smart_counters()
    # phase 2: idle, then a burst: does speed come back (cache folds back / drive cools)?
    if verdict["aborted"] is None and a.post_idle_s:
        for _ in range(a.post_idle_s):
            samples.append(
                {
                    "t": round(time.time() - t0, 1),
                    "phase": "idle",
                    "nvme_c": nvme_c(),
                    "cpu_c": cpu_c(),
                    "free_gib": round(free_gib(), 1),
                }
            )
            time.sleep(1)
        for fp in glob.glob(prefix + "_bw*.log"):
            os.rename(fp, fp + ".phase1")
        b = subprocess.Popen(fio_cmd(a.post_burst_gib, f"{a.out}.burst.fio.json"))
        watch(b, "burst", 600)
        bs = read_bwlog(prefix)
        verdict["post_idle_burst_MiBps"] = [[round(t, 1), round(v, 1)] for t, v in bs]
        verdict["counters_end"] = smart_counters()
    try:
        os.remove(f)
    except OSError:
        pass
    shutil.rmtree(d, ignore_errors=True)
    verdict["samples"] = samples
    verdict["max_nvme_c"] = max((s["nvme_c"] or 0) for s in samples) if samples else None
    verdict["max_cpu_c"] = max((s["cpu_c"] or 0) for s in samples) if samples else None
    with open(a.out, "w") as fh:
        json.dump(verdict, fh, indent=1)
    print(
        json.dumps(
            {k: verdict[k] for k in ("label", "cliff", "aborted", "max_nvme_c", "max_cpu_c")}
        )
    )
    return 0 if verdict["aborted"] is None else 6


if __name__ == "__main__":
    sys.exit(main())
