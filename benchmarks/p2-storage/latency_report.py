"""Merge per-host latency results and compare p99 with the targets.

env/bin/python benchmarks/p2-storage/latency_report.py OUT_DIR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    out = Path(sys.argv[1])
    hosts: list[dict[str, Any]] = [
        json.loads(p.read_text()) for p in sorted(out.glob("latency-host-*.json"))
    ]
    targets: dict[str, float] = hosts[0]["targets_ms"]
    lines = [
        "| operation | target p99 | "
        + " | ".join(f"{h['host']} p50 / p99" for h in hosts)
        + " | met |",
        "|---|---|" + "---|" * len(hosts) + "---|",
    ]
    missed = []
    for name, target in targets.items():
        cells, met = [], True
        for h in hosts:
            op = h["operations"].get(name)
            if op is None:
                cells.append("-")
                continue
            cells.append(f"{op['p50_ms']} / {op['p99_ms']} ms")
            met = met and op["p99_ms"] <= target
        lines.append(
            f"| {name} | {target} ms | " + " | ".join(cells) + f" | {'yes' if met else 'NO'} |"
        )
        if not met:
            missed.append(name)
    lines.append("")
    for h in hosts:
        lines.append(
            f"- {h['host']}: {h['achieved_units_per_s']}/{h['rate_target_units_per_s']} units/s "
            f"(10x the expected {h['expected_per_host_units_per_s']}/s), "
            f"failed units {h['failed_units']}, client CPU {h['client_cpu_utilisation']} cores, "
            f"schedule lag p99 {h['schedule_lag']['p99_ms']} ms, "
            f"publication lag p50/p99 {h['publication_lag_s']['p50']}/"
            f"{h['publication_lag_s']['p99']} s"
        )
    text = "\n".join(lines) + "\n"
    (out / "latency.md").write_text(text)
    print(text)
    if missed:
        print(f"p99 target missed: {missed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
