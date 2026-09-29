"""Combine V1/V2 parse results of one session into the P5 CPU gate result (stdlib only).

    python3 benchmarks/p5-extraction/compare.py RESULTS_DIR

Reads ``v1.round<N>.json`` and ``v2.round<N>.json`` and writes ``gate.json``:
per round V2/V1 aggregate CPU per page, and the gate verdict on the median
ratio (gate: V2 ≤ 50 % of V1). Per-page samples stay in the round files.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

GATE = 0.5


def main() -> None:
    results = Path(sys.argv[1])
    rounds = []
    for v1_path in sorted(results.glob("v1.round*.json")):
        v2_path = results / v1_path.name.replace("v1.", "v2.", 1)
        v1, v2 = json.loads(v1_path.read_text()), json.loads(v2_path.read_text())
        if v1["pages"] != v2["pages"]:
            raise SystemExit("rounds must use the same workload")
        paired = [v2["per_page_ms"][k] / v1["per_page_ms"][k] for k in v1["per_page_ms"]]
        rounds.append(
            {
                "round": v1_path.name.split(".")[1],
                "pages": v1["pages"],
                "v1": {k: v1[k] for k in ("aggregate_ms_per_page", "median_ms", "p95_ms")},
                "v2": {k: v2[k] for k in ("aggregate_ms_per_page", "median_ms", "p95_ms")},
                "ratio_aggregate": round(
                    v2["aggregate_ms_per_page"] / v1["aggregate_ms_per_page"], 4
                ),
                "ratio_per_page_median": round(statistics.median(paired), 4),
                "ratio_per_page_max": round(max(paired), 4),
            }
        )
    ratio = statistics.median(r["ratio_aggregate"] for r in rounds)
    gate = {
        "gate": f"V2 parse CPU/page <= {GATE:.0%} of V1",
        "ratio_aggregate_median_of_rounds": round(ratio, 4),
        "passed": ratio <= GATE,
        "rounds": rounds,
    }
    (results / "gate.json").write_text(json.dumps(gate, indent=1) + "\n")
    print(json.dumps(gate, indent=1))


if __name__ == "__main__":
    main()
