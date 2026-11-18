#!/usr/bin/env python3
"""Sample container cgroup memory (anon / file / current / peak / limit / events) at 1 Hz. Read-only.

usage: wp1r2_cgroup_sampler.py OUT.jsonl SECONDS CONTAINER [CONTAINER ...]
Anonymous memory is what an OOM kill is about; `file` is reclaimable page cache. `docker stats`
cannot tell them apart, which is why this exists.
"""

# ruff: noqa: E501, S603, S607
# (one-off measurement tooling, same convention as the Run 1 scripts: file reads kept simple, fixed argv lists only)
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def cg_path(name: str) -> Path:
    cid = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", name],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope")


def read(cg: Path) -> dict[str, int]:
    stat = dict(ln.split() for ln in (cg / "memory.stat").read_text().splitlines())
    ev = dict(ln.split() for ln in (cg / "memory.events").read_text().splitlines())
    lim = (cg / "memory.max").read_text().strip()
    return {
        "anon": int(stat["anon"]),
        "file": int(stat["file"]),
        "current": int((cg / "memory.current").read_text()),
        "peak": int((cg / "memory.peak").read_text()),
        "limit": -1 if lim == "max" else int(lim),
        "high_events": int(ev["high"]),
        "max_events": int(ev["max"]),
        "oom_kill": int(ev["oom_kill"]),
    }


def main() -> int:
    out, secs, names = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
    cgs = {n: cg_path(n) for n in names}
    end = time.time() + secs
    with open(out, "w") as f:
        while time.time() < end:
            row = {"t": round(time.time(), 1)}
            for n, cg in cgs.items():
                try:
                    row[n] = read(cg)
                except OSError:
                    row[n] = None
            f.write(json.dumps(row) + "\n")
            f.flush()
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
