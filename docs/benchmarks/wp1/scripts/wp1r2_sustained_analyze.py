#!/usr/bin/env python3
"""Reduce wp1r2_sustained_write.py outputs (runs A, B) to the numbers used in the NVMe report.

usage: wp1r2_sustained_analyze.py RAW_NVME_DIR OUT_JSON
"""

# ruff: noqa: E501, SIM115
# (one-off measurement tooling, same convention as the Run 1 scripts: file reads kept simple, fixed argv lists only)
from __future__ import annotations

import json
import os
import sys
from typing import Any


def analyse(path: str) -> dict[str, Any]:
    d = json.load(open(path))
    bw = d["bw_series_MiBps"]
    sm = [s for s in d["samples"] if s["phase"] == "sustained"]
    t0 = sm[0]["t"]
    temp = {int(s["t"] - t0): s["nvme_c"] for s in sm if s["nvme_c"] is not None}
    plateau = sum(v for _, v in bw[2:6]) / 4
    cum, first = 0.0, None
    for t, v in bw:
        cum += v / 1024
        if first is None and t > 3 and v < 0.7 * plateau:
            first = {
                "t_s": t,
                "cumulative_GiB": round(cum - v / 1024, 1),
                "nvme_c": temp.get(int(t)),
            }
    post = [v for t, v in bw if first and t > first["t_s"] + 20][:60]
    burst = [v for _, v in d.get("post_idle_burst_MiBps", [])]
    idle = [s["nvme_c"] for s in d["samples"] if s["phase"] == "idle" and s["nvme_c"]]
    return {
        "label": d["label"],
        "rate_mib": d["rate_mib"],
        "plateau_MiBps": round(plateau),
        "first_drop": first,
        "mean_MiBps_20_to_80s_after_drop": round(sum(post) / len(post)) if post else None,
        "total_written_GiB": round(sum(v for _, v in bw) / 1024, 1),
        "max_nvme_c": d["max_nvme_c"],
        "max_cpu_c": d["max_cpu_c"],
        "nvme_c_at_drop_minus_TMT1_81": None
        if not first or first["nvme_c"] is None
        else round(first["nvme_c"] - 81, 1),
        "post_idle_burst_MiBps_mean": round(sum(burst) / len(burst)) if burst else None,
        "post_idle_burst_nvme_c_at_start": idle[-1] if idle else None,
        "aborted": d["aborted"],
        "smart_counters_available_in_script": d["smart_counters_available"],
    }


def main() -> int:
    raw, out = sys.argv[1:3]
    res: dict[str, Any] = {
        k: analyse(os.path.join(raw, f"sustained-{k}.json"))
        for k in "ABC"
        if os.path.exists(os.path.join(raw, f"sustained-{k}.json"))
    }
    for run in "ABC":
        try:
            a = json.load(open(os.path.join(raw, f"smart-before-sust{run}.json")))
            b = json.load(open(os.path.join(raw, f"smart-after-sust{run}.json")))
            res[run]["smart_counter_delta_over_run"] = {
                k: b[k] - a[k]
                for k in b
                if k.startswith("thm_") or k.endswith("_temp_time") or k == "critical_comp_time"
            }
        except (OSError, KeyError, ValueError):
            pass
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
