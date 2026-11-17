#!/usr/bin/env python3
"""WP1 bounded filesystem I/O benchmark (fio substitute; fio was not installed and could not be installed).

O_DIRECT on one file inside a dedicated directory on the root ext4 filesystem. Never touches a block device.
Python threads release the GIL during pread/pwrite, so thread count ~= queue depth; per-op Python overhead
(~5-15 us) is included in latencies and caps achievable IOPS -- numbers are a conservative lower bound.

usage: wp1_nvme_bench.py OUT.json [--dir DIR] [--size-gib 4] [--secs 20] [--runs 3] [--sustained-secs 120]
"""

# ruff: noqa: E501, E741, E731, SIM115, S101, S311, N806, N812
# (measurement tooling: broad excepts keep sampling alive when a sensor is missing; fixed argv lists only;
#  asserts guard the bench directory/bucket names; PRNG is for I/O offsets, not security.)
import argparse
import json
import mmap
import os
import random
import shutil
import sys
import threading
import time
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wp1_monitor as M

BS_SEQ, BS_RND, ALIGN = 1 << 20, 4096, 4096
ABORT = None
HOT_INFO = []
HOT = threading.Event()  # set by watcher when CPU package >= 95 C
THERMAL_LIMIT, COOL_TARGET = 95, 70


def pkg_temp():
    return M.temps().get("Package id 0")


def watcher(done):
    while not done.is_set():
        t = pkg_temp()
        if t is not None and t >= THERMAL_LIMIT:
            HOT_INFO.append((round(time.time(), 1), t))
            HOT.set()
        time.sleep(1)


def cooldown(maxs=240, nvme_target=None):
    t0 = time.time()
    nv = lambda: M.temp_c(M.HW_NVME + "/temp1_input") or 0
    while (
        (pkg_temp() or 0) > COOL_TARGET or (nvme_target and nv() > nvme_target)
    ) and time.time() - t0 < maxs:
        time.sleep(3)
    return [round(time.time() - t0), pkg_temp()]


def snapshot():
    sv = os.statvfs("/")
    t = M.temps()
    m = M.meminfo()
    pk, co = M.throttle()
    return dict(
        ts=time.strftime("%FT%T%z"),
        root_free_gib=round(sv.f_bavail * sv.f_frsize / 2**30, 2),
        root_used_pct=round(100 * (1 - sv.f_bavail / sv.f_blocks), 1),
        nvme_temp_c=M.temp_c(M.HW_NVME + "/temp1_input"),
        cpu_pkg_c=t.get("Package id 0"),
        mem_avail_mib=m["avail_mib"],
        swap_used_mib=m["swap_used_mib"],
        throttle_pkg=pk,
        throttle_core=co,
        gpu=M.gpu(),
    )


def pct(a, p):
    s = sorted(a)
    return (
        round(s[min(len(s) - 1, int(len(s) * p / 100))] * 1e3, 4) if s else None
    )  # a in seconds -> ms


def worker(fd, mode, bs, nblocks, region, deadline, res, seed):
    rnd = random.Random(seed)
    buf = mmap.mmap(-1, bs)
    buf.write(os.urandom(bs))
    lat_r, lat_w = array("d"), array("d")
    nb = 0
    seq = region[0]
    pc = time.perf_counter
    while pc() < deadline and not HOT.is_set():
        if mode.startswith("seq"):
            off = seq
            seq += bs
            if seq + bs > region[1]:
                seq = region[0]
            op = "w" if mode == "seqwrite" else "r"
        else:
            off = rnd.randrange(nblocks) * bs
            op = {"randread": "r", "randwrite": "w"}.get(mode) or (
                "r" if rnd.random() < 0.7 else "w"
            )
        t0 = pc()
        if op == "r":
            os.preadv(fd, [buf], off)
            lat_r.append(pc() - t0)
        else:
            os.pwritev(fd, [buf], off)
            lat_w.append(pc() - t0)
        nb += bs
    res.append((lat_r, lat_w, nb))


def run_case(path, name, mode, bs, threads, secs, size):
    fdflags = os.O_RDWR | os.O_DIRECT
    fd = os.open(path, fdflags)
    nblocks = size // bs
    step = size // threads // ALIGN * ALIGN
    HOT.clear()
    done = threading.Event()
    threading.Thread(target=watcher, args=(done,), daemon=True).start()
    res, th = [], []
    t0 = time.perf_counter()
    deadline = t0 + secs
    for i in range(threads):
        th.append(
            threading.Thread(
                target=worker,
                args=(fd, mode, bs, nblocks, (i * step, (i + 1) * step), deadline, res, i + 1),
            )
        )
    ev0 = time.time()
    [t.start() for t in th]
    [t.join() for t in th]
    el = time.perf_counter() - t0
    done.set()
    trunc = HOT.is_set()
    os.fsync(fd)
    os.close(fd)
    lr = array("d")
    lw = array("d")
    nb = 0
    for a, b, n in res:
        lr.extend(a)
        lw.extend(b)
        nb += n
    out = dict(
        name=name,
        mode=mode,
        block_bytes=bs,
        threads=threads,
        secs=round(el, 2),
        start_epoch=round(ev0, 1),
        truncated_by_thermal_limit=trunc,
        mib_s=round(nb / 2**20 / el, 1),
        iops=round((len(lr) + len(lw)) / el),
    )
    for lab, l in (("read", lr), ("write", lw)):
        if len(l):
            out[lab] = dict(
                n=len(l),
                iops=round(len(l) / el),
                avg_ms=round(sum(l) / len(l) * 1e3, 4),
                p50_ms=pct(l, 50),
                p95_ms=pct(l, 95),
                p99_ms=pct(l, 99),
                p999_ms=pct(l, 99.9),
                max_ms=round(max(l) * 1e3, 3),
            )
    return out


def sustained(path, secs, size, threads=4):
    """Sequential O_DIRECT write for `secs`, per-5s throughput, to expose SLC-cache exhaustion / thermal fade."""
    fd = os.open(path, os.O_RDWR | os.O_DIRECT)
    buf = mmap.mmap(-1, BS_SEQ)
    buf.write(os.urandom(BS_SEQ))
    step = size // threads // ALIGN * ALIGN
    counters = [0] * threads
    stop = time.time() + secs
    HOT.clear()
    done = threading.Event()
    threading.Thread(target=watcher, args=(done,), daemon=True).start()

    def w(i):
        off = i * step
        while time.time() < stop and not HOT.is_set() and not (ABORT and os.path.exists(ABORT)):
            os.pwritev(fd, [buf], off)
            counters[i] += BS_SEQ
            off += BS_SEQ
            if off + BS_SEQ > (i + 1) * step:
                off = i * step

    ts = [threading.Thread(target=w, args=(i,)) for i in range(threads)]
    [t.start() for t in ts]
    series, last, t0 = [], 0, time.time()
    while any(t.is_alive() for t in ts):
        time.sleep(5)
        tot = sum(counters)
        series.append(
            dict(
                t=round(time.time() - t0),
                mib_s=round((tot - last) / 2**20 / 5, 1),
                nvme_temp_c=M.temp_c(M.HW_NVME + "/temp1_input"),
                cpu_pkg_c=pkg_temp(),
            )
        )
        last = tot
    [t.join() for t in ts]
    done.set()
    os.fsync(fd)
    os.close(fd)
    return dict(
        name="seq_write_sustained",
        truncated_by_thermal_limit=HOT.is_set(),
        hot_trigger=list(HOT_INFO),
        secs=secs,
        total_gib_written=round(sum(counters) / 2**30, 1),
        series_5s=series,
    )


def smallfiles(d, n=3000, sz=65536):
    os.makedirs(d)
    blob = os.urandom(sz)
    t0 = time.perf_counter()
    lat = array("d")
    for i in range(n):
        t = time.perf_counter()
        fd = os.open(f"{d}/f{i}", os.O_WRONLY | os.O_CREAT)
        os.write(fd, blob)
        os.fsync(fd)
        os.close(fd)
        lat.append(time.perf_counter() - t)
    w = time.perf_counter() - t0
    t0 = time.perf_counter()
    for i in range(n):
        with open(f"{d}/f{i}", "rb") as f:
            f.read()
    r = time.perf_counter() - t0  # NOTE: buffered, page cache likely warm
    return dict(
        name="smallfiles_create_fsync_64KiB",
        files=n,
        create_fsync_per_s=round(n / w),
        create_p50_ms=pct(lat, 50),
        create_p99_ms=pct(lat, 99),
        readback_files_per_s_cached=round(n / r),
    )


def main():
    global ABORT
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--dir", default=os.path.expanduser("~/wp1-bench-tmp"))
    ap.add_argument("--size-gib", type=float, default=4)
    ap.add_argument("--secs", type=int, default=20)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--sustained-secs", type=int, default=120)
    ap.add_argument("--abort-flag")
    ap.add_argument("--only-sustained", action="store_true")
    a = ap.parse_args()
    ABORT = a.abort_flag
    d = a.dir
    assert os.path.basename(d).startswith("wp1-bench-tmp"), "refusing unexpected dir name"
    assert not os.path.exists(d), "bench dir already exists"
    os.makedirs(d)
    path = f"{d}/testfile"
    size = int(a.size_gib * 2**30) // BS_SEQ * BS_SEQ
    cases = [
        ("seq_write_1MiB_qd4", "seqwrite", BS_SEQ, 4),
        ("seq_read_1MiB_qd4", "seqread", BS_SEQ, 4),
        ("rand_read_4KiB_qd1", "randread", BS_RND, 1),
        ("rand_read_4KiB_qd32", "randread", BS_RND, 32),
        ("rand_write_4KiB_qd1", "randwrite", BS_RND, 1),
        ("rand_write_4KiB_qd32", "randwrite", BS_RND, 32),
        ("mixed_70r30w_4KiB_qd16", "mixed", BS_RND, 16),
    ]
    R = dict(
        tool="wp1_nvme_bench.py (python O_DIRECT; fio substitute)",
        params=vars(a),
        file_bytes=size,
        fs_dir=d,
        start=snapshot(),
        cases=[],
        notes="O_DIRECT, ext4 root fs, 4 KiB aligned, thread-per-queue-slot",
    )
    try:
        with open(path, "wb") as f:
            f.truncate(size)  # sparse; first pass is the full-file seq write below
        fd = os.open(path, os.O_RDWR | os.O_DIRECT)
        buf = mmap.mmap(-1, BS_SEQ)
        buf.write(os.urandom(BS_SEQ))
        for off in range(0, size, BS_SEQ):
            os.pwritev(fd, [buf], off)  # fully materialise file
        os.fsync(fd)
        os.close(fd)
        for run in range(1, 1 if a.only_sustained else a.runs + 1):
            if a.only_sustained:
                break
            for name, mode, bs, qd in cases:
                if ABORT and os.path.exists(ABORT):
                    R["aborted"] = open(ABORT).read()
                    raise SystemExit
                cd = cooldown()
                pre = snapshot()
                r = run_case(path, name, mode, bs, qd, a.secs, size)
                r.update(run=run, cooldown_s_and_pkg_c=cd, pre=pre, post=snapshot())
                R["cases"].append(r)
                print(
                    f"run{run} {name}: {r['mib_s']} MiB/s {r['iops']} IOPS trunc={r['truncated_by_thermal_limit']}  nvme={r['post']['nvme_temp_c']}C free={r['post']['root_free_gib']}GiB",
                    flush=True,
                )
                json.dump(R, open(a.out, "w"), indent=1)
        R["cooldown_before_sustained"] = cooldown(600, 50)
        R["sustained"] = dict(pre=snapshot(), **sustained(path, a.sustained_secs, size))
        R["sustained"]["post"] = snapshot()
        print("sustained:", [s["mib_s"] for s in R["sustained"]["series_5s"]], flush=True)
        if not a.only_sustained:
            R["smallfiles"] = smallfiles(f"{d}/small")
    except SystemExit:
        print("ABORTED by safety flag", flush=True)
    finally:
        R["end"] = snapshot()
        json.dump(R, open(a.out, "w"), indent=1)
        if os.path.basename(d).startswith("wp1-bench-tmp") and os.path.isdir(d):
            shutil.rmtree(d)
        R["cleanup_dir_exists"] = os.path.exists(d)
        json.dump(R, open(a.out, "w"), indent=1)
        print("cleanup: dir exists =", os.path.exists(d), flush=True)


if __name__ == "__main__":
    main()
