#!/usr/bin/env python3
"""Summarise fio JSON results (WP1 Run 2 / B3) into one JSON file and a markdown table.

usage: wp1r2_fio_summarize.py FIO_DIR OUT_JSON OUT_MD
Numbers are copied from the fio output unchanged (latencies converted ns -> us/ms for display only).
"""

# ruff: noqa: E501, SIM115, RUF001
# (one-off measurement tooling, same convention as the Run 1 scripts: file reads kept simple, fixed argv lists only)
from __future__ import annotations

import glob
import json
import os
import sys
from typing import Any


def row(path: str) -> dict[str, Any]:
    if (
        os.path.getsize(path) == 0
    ):  # fio writes JSON only at job end: empty = aborted or still running
        return {
            "name": os.path.basename(path)[:-5],
            "fio_error": "EMPTY_OUTPUT (job aborted or running)",
        }
    d = json.load(open(path))
    j = d["jobs"][0]
    o = j["job options"]
    rw = o["rw"]
    side = (
        "read" if rw in ("read", "randread") else "write" if rw in ("write", "randwrite") else None
    )
    r: dict[str, Any] = {
        "name": os.path.basename(path)[:-5],
        "rw": rw,
        "bs": o["bs"],
        "iodepth": int(o["iodepth"]),
        "numjobs": int(o.get("numjobs", 1)),
        "runtime_s": int(o["runtime"]),
        "fio_error": j["error"],
        "device_util_pct": d["disk_util"][0]["util"],
        "cpu_usr_pct": j["usr_cpu"],
        "cpu_sys_pct": j["sys_cpu"],
    }
    for s in ("read", "write"):
        if side not in (None, s):
            continue
        x = j[s]
        if x["total_ios"] == 0:
            continue
        pc = x["clat_ns"]["percentile"]
        r[s] = {
            "iops": round(x["iops"], 1),
            "bw_MiBps": round(x["bw_bytes"] / 2**20, 1),
            "lat_mean_us": round(x["lat_ns"]["mean"] / 1e3, 1),
            "clat_p50_us": pc["50.000000"] / 1e3,
            "clat_p95_us": pc["95.000000"] / 1e3,
            "clat_p99_us": pc["99.000000"] / 1e3,
            "clat_p99.9_us": pc["99.900000"] / 1e3,
        }
    return r


def aggregate(rows: list[dict[str, Any]], suite_log: str | None) -> list[dict[str, Any]]:
    """Group `<job>-rN` repeats: median / min / max / CV of IOPS, bandwidth and p99/p99.9, + max NVMe temp per run."""
    import re
    import statistics

    nmax: dict[str, float] = {}
    if suite_log and os.path.exists(suite_log):
        for ln in open(suite_log):
            m = re.match(r"(\S+) .*nvme_max_mC=(\d+)", ln)
            if m:
                nmax[m.group(1)] = int(m.group(2)) / 1000
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if "rw" in r:
            groups.setdefault(re.sub(r"-r\d+$", "", r["name"]), []).append(r)
    out = []
    for base, rs in sorted(groups.items()):
        for side in ("read", "write"):
            xs = [r for r in rs if side in r]
            if not xs:
                continue

            def stat(key: str, xs: list[dict[str, Any]] = xs, side: str = side) -> dict[str, float]:
                v = [r[side][key] for r in xs]
                med = statistics.median(v)
                cv = (
                    100 * statistics.pstdev(v) / statistics.mean(v)
                    if len(v) > 1 and statistics.mean(v)
                    else 0.0
                )
                return {
                    "median": round(med, 1),
                    "min": round(min(v), 1),
                    "max": round(max(v), 1),
                    "cv_pct": round(cv, 1),
                }

            temps = [nmax.get(r["name"]) for r in xs if nmax.get(r["name"]) is not None]
            out.append(
                {
                    "job": base,
                    "side": side,
                    "n": len(xs),
                    "rw": rs[0]["rw"],
                    "bs": rs[0]["bs"],
                    "iodepth": rs[0]["iodepth"],
                    "numjobs": rs[0]["numjobs"],
                    "runtime_s": rs[0]["runtime_s"],
                    "iops": stat("iops"),
                    "bw_MiBps": stat("bw_MiBps"),
                    "clat_p99_us": stat("clat_p99_us"),
                    "clat_p99.9_us": stat("clat_p99.9_us"),
                    "lat_mean_us": stat("lat_mean_us"),
                    "device_util_pct_median": statistics.median(r["device_util_pct"] for r in xs),
                    "nvme_max_c_per_run": temps,
                    "near_throttle_runs": sum(1 for t in temps if t >= 80),
                }
            )
    return out


def main() -> int:
    fio_dir, out_json, out_md = sys.argv[1:4]
    rows = [row(p) for p in sorted(glob.glob(os.path.join(fio_dir, "*.json")))]
    json.dump(rows, open(out_json, "w"), indent=1)
    lines = [
        "| job | rw | bs | QD | jobs | side | IOPS | MiB/s | mean us | p50 us | p95 us | p99 us | p99.9 us | util % | cpu usr/sys % |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "rw" not in r:
            lines.append(f"| {r['name']} | **{r['fio_error']}** |" + " |" * 13)
            continue
        for s in ("read", "write"):
            if s in r:
                x = r[s]
                lines.append(
                    f"| {r['name']} | {r['rw']} | {r['bs']} | {r['iodepth']} | {r['numjobs']} | {s} | {x['iops']:g} | "
                    f"{x['bw_MiBps']:g} | {x['lat_mean_us']:g} | {x['clat_p50_us']:g} | {x['clat_p95_us']:g} | "
                    f"{x['clat_p99_us']:g} | {x['clat_p99.9_us']:g} | {r['device_util_pct']:.0f} | "
                    f"{r['cpu_usr_pct']:.0f}/{r['cpu_sys_pct']:.0f} |"
                )
    open(out_md, "w").write("\n".join(lines) + "\n")
    agg = aggregate(rows, os.path.join(fio_dir, "suite.log"))
    if agg:
        json.dump(agg, open(out_json.replace(".json", "-repeats.json"), "w"), indent=1)
        al = [
            "| job | side | n | IOPS median [min–max] (CV %) | MiB/s median | p99 µs median [min–max] | p99.9 µs median | max NVMe °C per run |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for g in agg:
            i, p99 = g["iops"], g["clat_p99_us"]
            al.append(
                f"| {g['job']} | {g['side']} | {g['n']} | {i['median']:g} [{i['min']:g}–{i['max']:g}] ({i['cv_pct']}) "
                f"| {g['bw_MiBps']['median']:g} | {p99['median']:g} [{p99['min']:g}–{p99['max']:g}] "
                f"| {g['clat_p99.9_us']['median']:g} | {', '.join(f'{t:g}' for t in g['nvme_max_c_per_run'])} |"
            )
        open(out_md.replace(".md", "-repeats.md"), "w").write("\n".join(al) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
