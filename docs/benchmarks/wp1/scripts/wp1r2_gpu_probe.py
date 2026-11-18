"""WP1 Run 2 / E2-E4: bounded GPU probe on the real encoder (run with the fingerprinter venv,
cwd = fingerprinter repo):

  env/bin/python -I <this file> OUT.json [--batches 1 2 4 8 16] [--secs 30]

Measures, per batch size: sustained throughput (frames/s) over `--secs`, per-batch latency,
peak torch VRAM, process peak RSS, and 1 Hz GPU temperature / power / SM clock / utilisation /
clock-throttle reasons / CPU package temperature. Also CPU-vs-GPU numerical agreement (cosine,
norms, finiteness) on identical golden inputs, and proves offline operation (any non-loopback
socket connect is recorded and refused; HF offline env + the project's own guard stay on).

Safety: stops a level on GPU >= 92 C or CPU package >= 99 C for 3 consecutive polls; cools to
<= 60 C GPU (max 300 s) between levels. Read-only with respect to the system.
"""

# ruff: noqa: E402, E501, SIM115, N806, S603, S607
# (one-off measurement tooling, same convention as the Run 1 scripts: file reads kept simple, fixed argv lists only)

import json
import os
import resource
import socket
import subprocess
import sys
import time
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
ATTEMPTS: list[str] = []
_orig = socket.socket.connect


def _guard(self, addr, *a, **k):
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
        ATTEMPTS.append(str(host))
        raise OSError("WP1 offline guard: external connect refused")
    return _orig(self, addr, *a, **k)


socket.socket.connect = _guard
import glob

import numpy as np
import torch
from fingerprinter.model import Embedder, golden_inputs

Q = "temperature.gpu,power.draw,clocks.sm,utilization.gpu,memory.used,clocks_throttle_reasons.active"


def gpu_sample() -> dict:
    o = (
        subprocess.run(
            ["nvidia-smi", f"--query-gpu={Q}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .split(", ")
    )
    try:
        return {
            "gpu_c": float(o[0]),
            "power_w": float(o[1]),
            "sm_mhz": float(o[2]),
            "util_pct": float(o[3]),
            "vram_used_mib": float(o[4]),
            "throttle_reasons": o[5],
        }
    except (ValueError, IndexError):
        return {"gpu_c": None}


def cpu_pkg() -> float | None:
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


def rss_mib() -> int:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)


def main() -> int:
    out_path = sys.argv[1]
    batches = [1, 2, 4, 8, 16]
    secs = 30
    if "--batches" in sys.argv:
        i = sys.argv.index("--batches") + 1
        batches = []
        while i < len(sys.argv) and not sys.argv[i].startswith("--"):
            batches.append(int(sys.argv[i]))
            i += 1
    if "--secs" in sys.argv:
        secs = int(sys.argv[sys.argv.index("--secs") + 1])
    R: dict = {
        "torch_runtime": torch.__version__,
        "cuda": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
        "device": torch.cuda.get_device_name(0),
        "capability": list(torch.cuda.get_device_capability(0)),
        "levels": [],
        "secs_per_level": secs,
    }
    x8 = golden_inputs(8)
    # numerical agreement CPU vs CUDA on identical inputs
    ec = Embedder("cpu", "float32", threads=12)
    vc = ec.embed(x8)
    ec.free()
    eg = Embedder("cuda", "float32", threads=None)
    vg = eg.embed(x8)
    eg.sync()
    cos = (vc * vg).sum(1)
    R["agreement"] = {
        "shape": list(vg.shape),
        "finite_cpu": bool(np.isfinite(vc).all()),
        "finite_gpu": bool(np.isfinite(vg).all()),
        "norm_gpu_min_max": [
            float(np.linalg.norm(vg, axis=1).min()),
            float(np.linalg.norm(vg, axis=1).max()),
        ],
        "cosine_min": float(cos.min()),
        "max_abs_diff": float(np.abs(vc - vg).max()),
    }
    eg.free()
    del eg

    # --- CPU leg (E2): same golden inputs, 12 threads, batch 8, sustained `secs` seconds with telemetry
    def cpu_stat() -> tuple[int, int]:
        f = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        v = [int(x) for x in f]
        return sum(v), v[3] + v[4]

    ecpu = Embedder("cpu", "float32", threads=12)
    xcpu = golden_inputs(8)
    ecpu.embed(xcpu)
    lat_c, samp_c, nfr, t_total0, t_idle0 = [], [], 0, *cpu_stat()
    start = time.perf_counter()
    last_s = 0.0
    hot_c = 0
    abort_c = None
    while time.perf_counter() - start < secs:
        t1 = time.perf_counter()
        ecpu.embed(xcpu)
        lat_c.append(time.perf_counter() - t1)
        nfr += 8
        if time.perf_counter() - last_s >= 1.0:
            last_s = time.perf_counter()
            sc = {
                "t": round(last_s - start, 1),
                "cpu_pkg_c": cpu_pkg(),
                "gpu_c": gpu_sample().get("gpu_c"),
            }
            samp_c.append(sc)
            hot_c = hot_c + 1 if (sc["cpu_pkg_c"] or 0) >= 99 else 0
            if hot_c >= 3:
                abort_c = sc
                break
    el = time.perf_counter() - start
    t_total1, t_idle1 = cpu_stat()
    lat_c.sort()
    R["cpu_leg"] = {
        "threads": 12,
        "batch": 8,
        "seconds": round(el, 1),
        "frames_per_s": round(nfr / el, 2),
        "batch_latency_ms_p50": round(lat_c[len(lat_c) // 2] * 1000, 1),
        "batch_latency_ms_p95": round(lat_c[int(len(lat_c) * 0.95)] * 1000, 1),
        "cpu_util_pct_mean": round(
            100.0 * (1 - (t_idle1 - t_idle0) / max(1, t_total1 - t_total0)), 1
        ),
        "peak_rss_mib": rss_mib(),
        "cpu_pkg_c_max": max((x["cpu_pkg_c"] or 0) for x in samp_c) if samp_c else None,
        "aborted": abort_c,
        "samples": samp_c,
    }
    ecpu.free()
    del ecpu
    while (cpu_pkg() or 99) > 70:  # let the CPU settle before the GPU levels
        time.sleep(5)

    for B in batches:
        t0 = time.time()
        while (gpu_sample().get("gpu_c") or 99) > 60 and time.time() - t0 < 300:
            time.sleep(5)
        e = Embedder("cuda", "float32", threads=None)
        x = golden_inputs(B)
        e.embed(x)
        e.sync()
        e.reset_peak()
        lat, samples, hot_g, hot_c, aborted = [], [], 0, 0, None
        start = time.perf_counter()
        last_s = 0.0
        n_frames = 0
        while time.perf_counter() - start < secs:
            t1 = time.perf_counter()
            e.embed(x)
            e.sync()
            lat.append(time.perf_counter() - t1)
            n_frames += B
            if time.perf_counter() - last_s >= 1.0:
                last_s = time.perf_counter()
                s = gpu_sample()
                s["t"] = round(last_s - start, 1)
                s["cpu_pkg_c"] = cpu_pkg()
                s["frames_cum"] = n_frames
                samples.append(s)
                hot_g = hot_g + 1 if (s.get("gpu_c") or 0) >= 92 else 0
                hot_c = hot_c + 1 if (s.get("cpu_pkg_c") or 0) >= 99 else 0
                if hot_g >= 3 or hot_c >= 3:
                    aborted = {"t": s["t"], "gpu_c": s.get("gpu_c"), "cpu_pkg_c": s["cpu_pkg_c"]}
                    break
        el = time.perf_counter() - start
        lat_s = sorted(lat)
        g = [s["gpu_c"] for s in samples if s.get("gpu_c")]
        half = len(g) // 2
        R["levels"].append(
            {
                "batch": B,
                "seconds": round(el, 1),
                "frames_per_s": round(n_frames / el, 2),
                "batch_latency_ms_p50": round(lat_s[len(lat_s) // 2] * 1000, 1),
                "batch_latency_ms_p95": round(lat_s[int(len(lat_s) * 0.95)] * 1000, 1),
                "vram": e.vram(),
                "peak_rss_mib": rss_mib(),
                "gpu_c_start": g[0] if g else None,
                "gpu_c_max": max(g) if g else None,
                "gpu_c_last10_mean": round(sum(g[-10:]) / len(g[-10:]), 1) if g else None,
                "power_w_mean": round(sum(s["power_w"] for s in samples) / len(samples), 1)
                if samples
                else None,
                "sm_mhz_first_last": [samples[0]["sm_mhz"], samples[-1]["sm_mhz"]]
                if samples
                else None,
                "frames_per_s_first_half_vs_second_half": None
                if half < 2
                else [
                    round(samples[half - 1]["frames_cum"] / samples[half - 1]["t"], 2),
                    round(
                        (n_frames - samples[half - 1]["frames_cum"])
                        / (el - samples[half - 1]["t"]),
                        2,
                    ),
                ],
                "throttle_reasons_seen": sorted({s["throttle_reasons"] for s in samples}),
                "cpu_pkg_c_max": max((s["cpu_pkg_c"] or 0) for s in samples) if samples else None,
                "aborted": aborted,
                "samples": samples,
            }
        )
        e.free()
        del e
        torch.cuda.empty_cache()
        if aborted:
            break
    R["external_connect_attempts"] = ATTEMPTS
    with open(out_path, "w") as f:
        json.dump(R, f, indent=1)
    print(
        json.dumps(
            {
                "agreement": R["agreement"],
                "external_connect_attempts": ATTEMPTS,
                "levels": [
                    {k: v for k, v in L.items() if k not in ("samples",)} for L in R["levels"]
                ],
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
