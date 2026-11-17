"""WP1 Stage E: bounded encoder environment check (run with the fingerprinter venv, cwd = fingerprinter repo).
Loads the real Embedder on CPU and CUDA offline (any non-loopback socket connect is recorded and refused),
times init / cold / warm inference on golden inputs, verifies shape, finiteness, L2 norm, CPU-vs-GPU cosine."""

# ruff: noqa: S607, E501, E731, E402
# (measurement tooling: broad excepts keep sampling alive when a sensor is missing; fixed argv lists only;
#  asserts guard the bench directory/bucket names; PRNG is for I/O offsets, not security.)
import json
import os
import resource
import socket
import subprocess
import time

import numpy as np

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
attempts = []
_orig = socket.socket.connect


def guarded(self, addr, *a, **k):
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
        attempts.append(str(host))
        raise OSError("WP1 offline guard: external connect refused")
    return _orig(self, addr, *a, **k)


socket.socket.connect = guarded
import torch
from fingerprinter.model import Embedder, golden_inputs


def gpu():
    o = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=temperature.gpu,memory.used,power.draw,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return o


rss = lambda: round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
x = golden_inputs(8)
R = dict(
    torch=torch.__version__,
    cuda_available=torch.cuda.is_available(),
    torch_cuda=torch.version.cuda,
    device=torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    input_shape=list(x.shape),
    gpu_before=gpu(),
    runs={},
)
out = {}
for dev, thr in (("cpu", 12), ("cuda", None)):
    r = {}
    t0 = time.perf_counter()
    e = Embedder(dev, "float32", threads=thr)
    r["init_s"] = round(time.perf_counter() - t0, 2)
    if dev == "cuda":
        e.reset_peak()
    t0 = time.perf_counter()
    v = e.embed(x[:2])
    e.sync()
    r["cold_first_batch2_s"] = round(time.perf_counter() - t0, 3)
    ts = []
    for _ in range(5):
        t0 = time.perf_counter()
        v = e.embed(x)
        e.sync()
        ts.append(time.perf_counter() - t0)
    r["warm_batch8_s"] = [round(t, 4) for t in ts]
    r["warm_frames_per_s_median"] = round(8 / sorted(ts)[2], 2)
    r["shape"] = list(v.shape)
    r["finite"] = bool(np.isfinite(v).all())
    n = np.linalg.norm(v, axis=1)
    r["l2_norm_min_max"] = [float(n.min()), float(n.max())]
    r["peak_rss_mib"] = rss()
    if dev == "cuda":
        r["vram"] = e.vram()
        r["gpu_after"] = gpu()
    out[dev] = v
    R["runs"][dev] = r
    e.free()
    del e
cos = (out["cpu"] * out["cuda"]).sum(1)
R["cpu_vs_cuda_cosine_min"] = float(cos.min())
R["external_connect_attempts"] = attempts
print(json.dumps(R, indent=1))
