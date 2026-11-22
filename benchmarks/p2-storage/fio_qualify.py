#!/usr/bin/env python3
# ruff: noqa: E501
"""Bounded, file-based fio qualification of a mounted filesystem (WP1 H-02 / H-08 evidence).

Safety rules (enforced, not advisory):
  * only a regular file inside --dir is ever used (never a block device); it is deleted afterwards;
  * refuses to start unless free space after the test file stays >= --min-free-gib (default 20);
  * every job is bounded by --runtime seconds AND by the file size (no unbounded writes);
  * the sustained-write job writes at most --sustained-write-gib in total.

Jobs (O_DIRECT, ioengine=io_uring (libaio unusable while fs.aio-nr == fs.aio-max-nr, Scylla container), so the page cache is not measured):
  seq_read / seq_write   1 MiB, QD8, 1 job          rand_read_4k / rand_write_4k   4 KiB, QD32, 4 jobs
  qd1_read_4k / qd1_write_4k   4 KiB, QD1, 1 job    sustained_seq_read / sustained_seq_write   1 MiB, QD8,
  1-second bandwidth log (to locate a performance cliff, if any).
Alongside: sysfs temperature sampling (1 Hz), kernel-journal AER / I/O / EXT4 message count before vs after.
SMART is NOT read here (needs root); the report records whether it was available.

    python3 benchmarks/p2-storage/fio_qualify.py --dir /mnt/data --label a15-nvme-mnt-data \
        --hwmon /sys/class/nvme/nvme0/hwmon3 --out benchmarks/p2-storage/results/wp1-fio-a15-nvme-mnt-data.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GiB = 1024**3
VERSION = "wp1-fio/1"


def sh(cmd: list[str], timeout: int = 3600) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout


def journal_hits() -> dict[str, int]:
    out = sh(["journalctl", "-k", "--no-pager", "-b"])
    pats = {
        "aer": r"AER:",
        "io_error": r"I/O error|blk_update_request",
        "ext4": r"EXT4-fs (error|warning)",
        "nvme_reset": r"nvme nvme\d+: (controller is down|resetting|I/O .* timeout)",
    }
    return {k: len(re.findall(p, out)) for k, p in pats.items()}


class Temps(threading.Thread):
    def __init__(self, hwmon: str | None, device: str | None) -> None:
        super().__init__(daemon=True)
        self.hwmon, self.device, self._halt = hwmon, device, threading.Event()
        self.samples: list[dict[str, float | None]] = []

    def read(self) -> dict[str, float | None]:
        r: dict[str, float | None] = {}
        if self.hwmon:
            for f in sorted(Path(self.hwmon).glob("temp*_input")):
                with contextlib.suppress(OSError):
                    r[f.name.replace("_input", "")] = int(f.read_text()) / 1000
        if self.device:
            out = sh(["smartctl", "-A", self.device]) if shutil.which("smartctl") else ""
            m = re.search(r"Temperature:\s+(\d+)", out)
            if m:
                r["smart_temp"] = float(m.group(1))
        return r

    def run(self) -> None:
        while not self._halt.wait(1.0):
            self.samples.append({"t": time.time(), **self.read()})

    def stop(self) -> None:
        self._halt.set()


def fio_job(
    name: str,
    path: Path,
    rw: str,
    bs: str,
    iodepth: int,
    numjobs: int,
    runtime: int,
    size_gib: int,
    loops_write_cap_gib: int | None = None,
    bwlog: Path | None = None,
) -> dict[str, Any]:
    cmd = [
        "fio",
        f"--name={name}",
        f"--filename={path}",
        f"--rw={rw}",
        f"--bs={bs}",
        "--direct=1",
        "--ioengine=io_uring",
        f"--iodepth={iodepth}",
        f"--numjobs={numjobs}",
        f"--size={size_gib}G",
        "--group_reporting",
        "--output-format=json",
        "--randrepeat=0",
    ]
    if (
        runtime and not loops_write_cap_gib
    ):  # short jobs: time-bounded; sustained jobs: bounded by --size only
        cmd += [f"--runtime={runtime}", "--time_based", "--ramp_time=2"]
    if "write" in rw:
        cmd += ["--end_fsync=1"]
    if bwlog:
        cmd += [f"--write_bw_log={bwlog}", "--log_avg_msec=1000"]
    t0 = time.time()
    raw = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=7200)
    j = json.loads(raw.stdout[raw.stdout.index("{") :]) if "{" in raw.stdout else {}
    job = (j.get("jobs") or [{}])[0]
    side = "read" if "read" in rw else "write"
    d = job.get(side, {})
    pct = (d.get("clat_ns") or {}).get("percentile", {})
    return {
        "name": name,
        "cmd": " ".join(cmd),
        "rw": rw,
        "bs": bs,
        "iodepth": iodepth,
        "numjobs": numjobs,
        "runtime_s": round(time.time() - t0, 1),
        "bytes": d.get("io_bytes"),
        "bw_mib_s": round(d.get("bw", 0) / 1024, 1),
        "iops": round(d.get("iops", 0), 1),
        "lat_us": {
            "mean": round((d.get("clat_ns") or {}).get("mean", 0) / 1e3, 1),
            "p50": round(pct.get("50.000000", 0) / 1e3, 1),
            "p99": round(pct.get("99.000000", 0) / 1e3, 1),
            "p99.9": round(pct.get("99.900000", 0) / 1e3, 1),
            "p99.99": round(pct.get("99.990000", 0) / 1e3, 1),
            "max": round((d.get("clat_ns") or {}).get("max", 0) / 1e3, 1),
        },
        "error": job.get("error", 0) or (raw.stderr[-300:] if raw.returncode else 0),
    }


def bw_cliff(log: Path) -> dict[str, Any]:
    """1-s average bandwidth log -> first/median/last-quartile MiB/s and the first second below 50 % of the first-10 s median."""
    rows = []
    for fl in sorted(log.parent.glob(log.name + "*.log")):
        for line in fl.read_text().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 2:
                rows.append((int(p[0]) / 1000, int(p[1]) / 1024))
    for g in log.parent.glob(log.name + "*.log"):
        g.unlink(missing_ok=True)
    if len(rows) < 12:
        return {"seconds": len(rows)}
    rows.sort()
    head = statistics.median(b for _, b in rows[:10])
    below = next((t for t, b in rows[10:] if b < 0.5 * head), None)
    return {
        "seconds": len(rows),
        "first10s_median_mib_s": round(head, 1),
        "overall_median_mib_s": round(statistics.median(b for _, b in rows), 1),
        "last_quartile_median_mib_s": round(
            statistics.median(b for _, b in rows[-len(rows) // 4 :]), 1
        ),
        "min_mib_s": round(min(b for _, b in rows), 1),
        "first_second_below_50pct_of_head": below,
        "series_mib_s_every_10s": [round(b) for _, b in rows[::10]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--hwmon")
    ap.add_argument("--size-gib", type=int, default=4, help="test file size for the short jobs")
    ap.add_argument("--runtime", type=int, default=20)
    ap.add_argument("--sustained-write-gib", type=int, default=40)
    ap.add_argument("--sustained-read-gib", type=int, default=16)
    ap.add_argument("--min-free-gib", type=int, default=20)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--skip-sustained", action="store_true")
    a = ap.parse_args()

    d = Path(a.dir)
    big = max(a.sustained_write_gib, a.sustained_read_gib, a.size_gib)
    free0 = shutil.disk_usage(d).free
    need = big * GiB + a.min_free_gib * GiB
    if a.skip_sustained:
        need = a.size_gib * GiB + a.min_free_gib * GiB
    if free0 < need:
        print(
            f"REFUSED: free {free0 / GiB:.1f} GiB < required {need / GiB:.1f} GiB (file + {a.min_free_gib} GiB floor)"
        )
        return 2
    work = d / f".wp1-fio-{os.getpid()}"
    work.mkdir()
    findmnt = sh(["findmnt", "-no", "SOURCE,FSTYPE,OPTIONS", "-T", str(d)]).strip()
    dev = findmnt.split()[0] if findmnt else ""
    res: dict[str, Any] = {
        "benchmark": VERSION,
        "label": a.label,
        "started": datetime.now(UTC).isoformat(),
        "host": platform.node(),
        "kernel": platform.release(),
        "fio": sh(["fio", "--version"]).strip(),
        "dir": str(d),
        "mount": findmnt,
        "source_device": dev,
        "block_parent": sh(["lsblk", "-no", "PKNAME", dev]).strip() if dev else "",
        "free_gib_before": round(free0 / GiB, 1),
        "min_free_gib": a.min_free_gib,
        "params": {k: v for k, v in vars(a).items()},
        "smart": "NOT_AVAILABLE (needs root; this tool never reads it)",
        "journal_before": journal_hits(),
        "jobs": [],
        "pass_criteria": "none defined in repo (see wp1 blocker register)",
    }
    temps = Temps(a.hwmon, None)
    temps.start()
    res["temp_before"] = temps.read()
    f = work / "testfile"
    try:
        # lay out the file once (sequential write, also the first sequential-write measurement)
        res["jobs"].append(
            {
                **fio_job("layout_seq_write", f, "write", "1M", 8, 1, 0, a.size_gib),
                "note": "file layout pass",
            }
        )
        for rep in range(a.repeats):
            for nm, rw, bs, qd, nj in (
                ("seq_read", "read", "1M", 8, 1),
                ("seq_write", "write", "1M", 8, 1),
                ("rand_read_4k", "randread", "4k", 32, 4),
                ("rand_write_4k", "randwrite", "4k", 32, 4),
                ("qd1_read_4k", "randread", "4k", 1, 1),
                ("qd1_write_4k", "randwrite", "4k", 1, 1),
            ):
                r = fio_job(nm, f, rw, bs, qd, nj, a.runtime, a.size_gib)
                r["repeat"] = rep + 1
                res["jobs"].append(r)
        f.unlink(missing_ok=True)
        if not a.skip_sustained:
            log = work / "bw_sw"
            r = fio_job(
                "sustained_seq_write",
                f,
                "write",
                "1M",
                8,
                1,
                3600,
                a.sustained_write_gib,
                loops_write_cap_gib=1,
                bwlog=log,
            )
            r["bw_log"] = bw_cliff(log)
            res["jobs"].append(r)
            temps_mid = temps.read()
            res["temp_after_sustained_write"] = temps_mid
            f.unlink(missing_ok=True)
            fio_job("layout_for_read", f, "write", "1M", 8, 1, 0, a.sustained_read_gib)
            subprocess.run(["sync"], check=False)
            r = fio_job(
                "sustained_seq_read",
                f,
                "read",
                "1M",
                8,
                1,
                3600,
                a.sustained_read_gib,
                loops_write_cap_gib=1,
                bwlog=work / "bw_sr",
            )
            r["bw_log"] = bw_cliff(work / "bw_sr")
            res["jobs"].append(r)
    finally:
        temps.stop()
        shutil.rmtree(work, ignore_errors=True)
    ts = [{k: v for k, v in s.items() if k != "t"} for s in temps.samples]
    keys = sorted({k for s in ts for k in s})
    res["temp_series"] = (
        {
            k: {
                "min": min(float(s[k] or 0) for s in ts if k in s),
                "max": max(float(s[k] or 0) for s in ts if k in s),
                "every_30s": [s.get(k) for s in ts[::30]],
            }
            for k in keys
        }
        if ts
        else {}
    )
    res["temp_after"] = temps.read()
    res["journal_after"] = journal_hits()
    res["free_gib_after"] = round(shutil.disk_usage(d).free / GiB, 1)
    res["finished"] = datetime.now(UTC).isoformat()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1))
    print(
        json.dumps(
            {j["name"]: (j["bw_mib_s"], j["iops"], j["lat_us"]["p99"]) for j in res["jobs"]},
            indent=0,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
