"""WP1 Run 2 / D3-D4: attribute the 1-copy E-R1 query latency, one variable at a time. NOT a gate.

Run with the isolated venv (qdrant-client, numpy) against a disposable gate-limit container:
  wp1r2_qdrant_diag_run.sh launches the container and calls this with --port/--grpc-port/--api-key.

Corpus: the 104,443 cached real dinov2-seg vectors as one target-role generation (one copy), the
same collection configuration and payload as capacity_e_r1.py ("C0"), queries = the harness's
760 target-film segment vectors. Variants change ONE thing and always report recall@100 against an
exact brute-force top-100 (so a faster setting that loses matches is visible):

  REST (server-reported `time`) vs client wall time, sequential (service time, no pacing):
    V0 gate parameters (payload returned, rescore, oversampling 2.0)
    V1 payload not returned          V2 oversampling 1.0        V3 hnsw_ef 64
  gRPC via qdrant-client exactly as the harness queries (V4), sequential and paced 2 x 5 q/s.
  C1 = originals in RAM (vectors on_disk=False): V0 and V4 repeated.

The E-R1 gate (capacity_e_r1.py, p95 <= 10 ms, harness client timing) is untouched and remains the
only judge of capacity.
"""

# ruff: noqa: S101, E501, N806, B023, PT018
# (one-off measurement tooling, same convention as the Run 1 scripts: file reads kept simple, fixed argv lists only)
from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

HARNESS = Path.home() / "anti_piracy/fingerprinter/benchmarks/p10-index/capacity_e_r1.py"
spec = importlib.util.spec_from_file_location("capacity_e_r1", HARNESS)
assert spec is not None and spec.loader is not None
er1 = importlib.util.module_from_spec(spec)
sys.modules["capacity_e_r1"] = er1
spec.loader.exec_module(er1)


def pct(xs: list[float], p: float) -> float | None:
    return round(float(np.percentile(xs, p)) * 1000, 3) if xs else None


class Rest:
    def __init__(self, port: int, key: str) -> None:
        self.c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        self.h = {"api-key": key, "content-type": "application/json"}

    def query(
        self, coll: str, q: np.ndarray, params: dict, payload: bool
    ) -> tuple[list[str], float]:
        body = json.dumps(
            {"query": q.tolist(), "limit": 100, "params": params, "with_payload": payload}
        )
        for attempt in (0, 1):
            try:
                self.c.request("POST", f"/collections/{coll}/points/query", body, self.h)
                r = json.loads(self.c.getresponse().read())
                break
            except (http.client.HTTPException, OSError):
                if attempt:
                    raise
                self.c.close()  # server closed an idle keep-alive connection: reconnect once
        return [p["id"] for p in r["result"]["points"]], float(r["time"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vectors", type=Path, required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--grpc-port", type=int, required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seq-n", type=int, default=400)
    ap.add_argument("--paced-s", type=int, default=90)
    ap.add_argument(
        "--payload-in-ram",
        action="store_true",
        help="candidate config C2: on_disk_payload=False (everything else as the gate)",
    )
    ap.add_argument(
        "--copies",
        type=int,
        default=1,
        help=">1: scale mode (steady state, C0 only): N-1 seeded rotations like E-R1",
    )
    a = ap.parse_args()
    base, Q = er1.load_corpus(a.vectors)
    base = base / np.linalg.norm(base, axis=1, keepdims=True)
    if a.copies > 1:
        parts = [base] + [base @ er1.rotation(768, 1000 + c) for c in range(1, a.copies)]
        base = np.vstack(parts).astype(np.float32)
        base /= np.linalg.norm(base, axis=1, keepdims=True)
    ids = [er1.pid(f"s{i // 12}", 1, i % 12) for i in range(len(base))]
    ns = argparse.Namespace(api_key=a.api_key, http_port=a.port, grpc_port=a.grpc_port)
    h = er1.Harness(ns)
    rest = Rest(a.port, a.api_key)
    # exact top-100 for recall (first 150 queries)
    nq = (
        min(150, len(Q)) if a.copies == 1 else 0
    )  # recall only at 1 copy (rotated copies: memory/latency only)
    exact = [set(ids[j] for j in np.argsort(-(base @ Q[i]))[:100]) for i in range(nq)]
    m = h.m
    gate = {"quantization": {"rescore": True, "oversampling": 2.0}}
    variants = {
        "V0_gate_params": (gate, True),
        "V1_no_payload": (gate, False),
        "V2_oversampling_1": ({"quantization": {"rescore": True, "oversampling": 1.0}}, True),
        "V3_hnsw_ef_64": ({**gate, "hnsw_ef": 64}, True),
    }
    report: dict = {
        "benchmark": "E-R1-diag (not a gate)",
        "points": len(base),
        "queries": len(Q),
        "configs": {},
    }
    configs = (("C0_gate_config", True), ("C1_originals_in_RAM", False))
    if a.payload_in_ram:
        configs = (("C2_payload_in_RAM", True),)
    for cname, on_disk in configs[: 1 if a.copies > 1 else 2]:
        coll = cname.lower()
        h.client.create_collection(
            coll,
            vectors_config=m.VectorParams(size=768, distance=m.Distance.DOT, on_disk=on_disk),
            hnsw_config=m.HnswConfigDiff(m=32, ef_construct=200),
            quantization_config=m.ScalarQuantization(
                scalar=m.ScalarQuantizationConfig(type=m.ScalarType.INT8, always_ram=True)
            ),
            on_disk_payload=not a.payload_in_ram,
        )
        for f in ("rep", "trep", "cid", "tid", "spec"):
            h.client.create_payload_index(coll, f, m.PayloadSchemaType.KEYWORD, wait=True)
        h.client.create_payload_index(coll, "seq", m.PayloadSchemaType.INTEGER, wait=True)
        h.resident[coll] = 0
        t_ing = h.bulk(coll, "target", base.astype(np.float32))
        t_green = h.wait_green(coll)
        rest = Rest(a.port, a.api_key)
        res: dict = {"ingest_s": round(t_ing, 1), "green_s": round(t_green, 1), "variants": {}}
        for _ in range(2):  # warm
            for q in Q:
                h.client.query_points(coll, query=q.tolist(), limit=100)
        for vname, (params, payload) in variants.items():
            if (cname.startswith("C1") and vname not in ("V0_gate_params",)) or (
                a.copies > 1 and vname not in ("V0_gate_params", "V1_no_payload")
            ):
                continue
            cl, sv, rec = [], [], []
            for i in range(a.seq_n):
                q = Q[i % len(Q)]
                t0 = time.perf_counter()
                got, srv = rest.query(coll, q, params, payload)
                cl.append(time.perf_counter() - t0)
                sv.append(srv)
                if i < nq:
                    rec.append(len(set(got) & exact[i]) / 100.0)
            res["variants"][vname] = {
                "client_ms_p50_p95_p99": [pct(cl, 50), pct(cl, 95), pct(cl, 99)],
                "server_ms_p50_p95_p99": [pct(sv, 50), pct(sv, 95), pct(sv, 99)],
                "recall_at_100_mean_min": [
                    round(float(np.mean(rec)), 4),
                    round(float(np.min(rec)), 4),
                ]
                if rec
                else None,
            }
            print(cname, vname, res["variants"][vname], flush=True)
        # V5: REST paced 2 x 5 q/s (same pacing as the harness) recording client AND server time
        pc: list[tuple[float, float, float]] = []

        def paced_rest(seed: int) -> None:
            rr, rng, nxt = Rest(a.port, a.api_key), np.random.default_rng(seed), time.time()
            while not stop5.is_set():
                q = Q[int(rng.integers(len(Q)))]
                t0 = time.time()
                _, srv = rr.query(coll, q, gate, True)
                pc.append((t0, time.time() - t0, srv))
                nxt += 0.2
                time.sleep(max(0.0, nxt - time.time()))

        stop5 = threading.Event()
        th5 = [threading.Thread(target=paced_rest, args=(s,), daemon=True) for s in (11, 12)]
        for t in th5:
            t.start()
        time.sleep(a.paced_s)
        stop5.set()
        for t in th5:
            t.join(timeout=30)
        t5 = min(t for t, _, _ in pc)
        c5 = [c for t, c, _ in pc if t >= t5 + 30]
        v5 = [v for t, _, v in pc if t >= t5 + 30]
        res["variants"]["V5_rest_paced_2x5qps"] = {
            "n": len(c5),
            "client_ms_p50_p95_p99": [pct(c5, 50), pct(c5, 95), pct(c5, 99)],
            "server_ms_p50_p95_p99": [pct(v5, 50), pct(v5, 95), pct(v5, 99)],
        }
        print(cname, "rest paced", res["variants"]["V5_rest_paced_2x5qps"], flush=True)
        # V4: gRPC qdrant-client, exactly as the harness queries (default payload, rescore, os 2.0)
        sp = m.SearchParams(quantization=m.QuantizationSearchParams(rescore=True, oversampling=2.0))
        cl, rec = [], []
        for i in range(a.seq_n):
            q = Q[i % len(Q)]
            t0 = time.perf_counter()
            r = h.client.query_points(coll, query=q.tolist(), limit=100, search_params=sp)
            cl.append(time.perf_counter() - t0)
            if i < nq:
                rec.append(len(set(str(p.id) for p in r.points) & exact[i]) / 100.0)
        res["variants"]["V4_grpc_harness_sequential"] = {
            "client_ms_p50_p95_p99": [pct(cl, 50), pct(cl, 95), pct(cl, 99)],
            "recall_at_100_mean_min": [round(float(np.mean(rec)), 4), round(float(np.min(rec)), 4)]
            if rec
            else None,
        }
        # paced 2 x 5 q/s exactly like the harness steady phase
        lat: list[tuple[str, float, float]] = []
        stop = threading.Event()
        h.query_lat = lat
        th = [
            threading.Thread(
                target=h.queries, args=(stop, lambda: "p", lambda c=coll: c, Q, 5.0, s), daemon=True
            )
            for s in (1, 2)
        ]
        for t in th:
            t.start()
        time.sleep(a.paced_s)
        stop.set()
        for t in th:
            t.join(timeout=30)
        t_first = min(t for _, t, _ in lat)
        xs = [d for _, t, d in lat if t >= t_first + 30]
        res["variants"]["V4_grpc_harness_paced_2x5qps"] = {
            "n": len(xs),
            "client_ms_p50_p95_p99": [pct(xs, 50), pct(xs, 95), pct(xs, 99)],
        }
        print(cname, "paced", res["variants"]["V4_grpc_harness_paced_2x5qps"], flush=True)
        info = h.client.get_collection(coll)
        res["copies"] = a.copies
        res["segments"] = info.segments_count
        res["indexed_vectors"] = info.indexed_vectors_count
        res["points"] = info.points_count
        report["configs"][cname] = res
        h.client.delete_collection(coll)
        del h.resident[coll]
    a.out.write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
