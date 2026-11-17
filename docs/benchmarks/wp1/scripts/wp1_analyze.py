#!/usr/bin/env python3
"""Summarise WP1 monitor JSONL files (whole file, or windows). usage: wp1_analyze.py OUT.json"""

# ruff: noqa: E501, E741, SIM115
# (measurement tooling: broad excepts keep sampling alive when a sensor is missing; fixed argv lists only;
#  asserts guard the bench directory/bucket names; PRNG is for I/O offsets, not security.)
import json
import os
import re
import sys

RAW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "raw")


def load(f):
    import gzip

    p = os.path.join(RAW, f)
    op = gzip.open(p + ".gz", "rt") if not os.path.exists(p) else open(p)
    return [json.loads(l) for l in op if l.strip()]


def mib(s):
    m = re.match(r"([\d.]+)\s*([KMG]i?B)", s or "")
    return (
        float(m.group(1))
        * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "KB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)]
        if m
        else None
    )


def stats(rows, name):
    if not rows:
        return dict(name=name, samples=0)
    pk = [
        r["temp_c"].get("Package id 0") for r in rows if r["temp_c"].get("Package id 0") is not None
    ]
    dt = (rows[-1]["epoch"] - rows[0]["epoch"]) or 1
    s = dict(
        name=name,
        samples=len(rows),
        start=rows[0]["ts"],
        end=rows[-1]["ts"],
        secs=round(dt),
        cpu_pct_avg=round(sum(r["cpu_pct"] for r in rows) / len(rows), 1),
        cpu_pct_peak=max(r["cpu_pct"] for r in rows),
        iowait_pct_peak=max(r["cpu_iowait_pct"] for r in rows),
        load1_peak=round(max(r["load1"] for r in rows), 2),
        cpu_pkg_c_avg=round(sum(pk) / len(pk), 1),
        cpu_pkg_c_peak=max(pk),
        secs_pkg_ge_90=sum(2 for p in pk if p >= 90),
        secs_pkg_ge_95=sum(2 for p in pk if p >= 95),
        secs_pkg_ge_100=sum(2 for p in pk if p >= 100),
        freq_avg_mhz=round(sum(r["freq_mhz"]["avg"] for r in rows) / len(rows)),
        freq_avg_min_mhz=min(r["freq_mhz"]["avg"] for r in rows),
        throttle_pkg_delta=rows[-1]["throttle_pkg"] - rows[0]["throttle_pkg"],
        throttle_core_delta=rows[-1]["throttle_core"] - rows[0]["throttle_core"],
        nvme_c_avg=round(sum(r["nvme_temp_c"] for r in rows) / len(rows), 1),
        nvme_c_peak=max(r["nvme_temp_c"] for r in rows),
        mem_avail_mib_min=min(r["mem"]["avail_mib"] for r in rows),
        mem_used_mib_peak=max(r["mem"]["used_mib"] for r in rows),
        swap_mib_peak=max(r["mem"]["swap_used_mib"] for r in rows),
        psi_mem_some_peak=max(r["psi_mem"].get("some", 0) for r in rows),
        psi_io_some_peak=max(r["psi_io"].get("some", 0) for r in rows),
        psi_io_full_peak=max(r["psi_io"].get("full", 0) for r in rows),
        oom_kill_delta=rows[-1]["oom_kill"] - rows[0]["oom_kill"],
        root_free_gib_min=min(r["root_free_gib"] for r in rows),
        root_free_gib_start=rows[0]["root_free_gib"],
        nvme_r_mbps_peak=max(r["nvme"]["r_mbps"] for r in rows),
        nvme_w_mbps_peak=max(r["nvme"]["w_mbps"] for r in rows),
        nvme_util_pct_peak=max(r["nvme"]["util_pct"] for r in rows),
        nvme_r_iops_peak=max(r["nvme"]["r_iops"] for r in rows),
        nvme_w_iops_peak=max(r["nvme"]["w_iops"] for r in rows),
        nvme_r_await_ms_peak=max([r["nvme"]["r_await_ms"] or 0 for r in rows]),
        nvme_w_await_ms_peak=max([r["nvme"]["w_await_ms"] or 0 for r in rows]),
    )
    g = [r["gpu"] for r in rows if "temp" in r["gpu"]]
    if g:
        s.update(
            gpu_temp_c_peak=max(x["temp"] for x in g),
            gpu_util_pct_peak=max(x["util"] for x in g),
            gpu_vram_mib_peak=max(x["vram_mib"] for x in g),
            gpu_power_w_peak=max(x["power_w"] for x in g if isinstance(x["power_w"], float))
            if any(isinstance(x["power_w"], float) for x in g)
            else None,
            gpu_throttle_reasons_seen=sorted({x["throttle"] for x in g}),
        )
    c = {}
    rs = {}
    for r in rows:
        for n, d in r["docker"].items():
            if not isinstance(d, dict):
                continue
            cpu = float(d["cpu"].strip("%"))
            m = mib(d["mem"].split("/")[0].strip())
            e = c.setdefault(n, dict(cpu_pct_peak=0, mem_mib_peak=0, health=set()))
            e["cpu_pct_peak"] = max(e["cpu_pct_peak"], cpu)
            e["mem_mib_peak"] = max(e["mem_mib_peak"], m or 0)
            e["health"].add(d["health"])
            rs.setdefault(n, []).append(d["restarts"])
    for n, e in c.items():
        e["health"] = sorted(x for x in e["health"] if x)
        e["restart_delta"] = (rs[n][-1] or 0) - (rs[n][0] or 0)
        e["cpu_pct_peak"] = round(e["cpu_pct_peak"], 1)
        e["mem_mib_peak"] = round(e["mem_mib_peak"])
    s["containers"] = c
    return s


def win(rows, a, b):
    return [r for r in rows if a <= r["ts"] <= b]


out = []
rows = load("stageB-attempt1-aborted-thermal-monitor.jsonl")
out.append(stats(rows, "B attempt1 (aborted at 95C) whole file"))
rows = load("stageB-nvme-monitor.jsonl")
bj = json.load(open(os.path.join(RAW, "stageB-nvme-bench.json")))
out.append(
    stats(win(rows, rows[0]["ts"], "2026-11-17T18:46:50+0530"), "B idle baseline (pre-benchmark)")
)
out.append(
    stats(
        win(rows, bj["start"]["ts"], bj["end"]["ts"]),
        "B run-2 case matrix (3 runs x 7 cases + smallfiles)",
    )
)
sj = json.load(open(os.path.join(RAW, "stageB-nvme-sustained.json")))
out.append(
    stats(
        win(rows, sj["sustained"]["pre"]["ts"], sj["sustained"]["post"]["ts"]),
        "B sustained 120 s seq write (final)",
    )
)
for f, n in (
    ("stageD-p2-monitor.jsonl", "D P2 latency (1x + 2x 10x + cleanup) whole file"),
    ("stageD-qdrant-er1-1copy-monitor.jsonl", "D E-R1 1-copy run 1"),
    ("stageD-qdrant-er1-6copy-monitor.jsonl", "D E-R1 6-copy (OOM-killed)"),
    ("stageD-qdrant-er1-1copy-run2-monitor.jsonl", "D E-R1 1-copy run 2"),
    ("stageE-encoder-monitor.jsonl", "E encoder B-GPU-a + env check"),
):
    out.append(stats(load(f), n))
for name, a, b in (
    ("D P2 1x run", "2026-11-17T19:00:20+0530", "2026-11-17T19:02:10+0530"),
    ("D P2 10x run 1", "2026-11-17T19:02:30+0530", "2026-11-17T19:04:15+0530"),
    ("D P2 10x run 2", "2026-11-17T19:05:15+0530", "2026-11-17T19:07:00+0530"),
):
    out.append(stats(win(load("stageD-p2-monitor.jsonl"), a, b), name))
json.dump(out, open(sys.argv[1], "w"), indent=1)
for s in out:
    print(
        f"\n## {s['name']}  [{s.get('start')} → {s.get('end')}  {s.get('secs')}s, n={s['samples']}]"
    )
    print(
        {k: v for k, v in s.items() if k not in ("name", "start", "end", "samples", "containers")}
    )
    for n, e in s.get("containers", {}).items():
        print("   ", n, e)
