"""Merge the benchmark plans with Scylla's own partition statistics; enforce the gate.

    env/bin/python benchmarks/p2-storage/report.py OUT_DIR KEYSPACE SCYLLA_CONTAINER

Sources, per table: ``system.large_partitions`` (exact size and row count of
every partition >= 1 MB, written at compaction) and the REST metric
``max_row_size`` (largest partition, histogram-bucketed). Exit 1 if any
partition exceeds 100 MB.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from crawler2.storage.scylla.migrations import load_migrations

GATE_BYTES = 100 * 1000 * 1000


def _exec(container: str, *argv: str) -> str:
    return subprocess.run(
        ["docker", "exec", container, *argv], check=True, capture_output=True, text=True
    ).stdout


def _large_partitions(container: str, keyspace: str, table: str) -> list[dict[str, Any]]:
    query = (
        "SELECT JSON partition_key, partition_size, rows FROM system.large_partitions "
        f"WHERE keyspace_name='{keyspace}' AND table_name='{table}'"
    )
    text = _exec(container, "sh", "-c", f'cqlsh $(hostname -i) -e "{query}"')
    rows = [json.loads(line.strip()) for line in text.splitlines() if line.strip().startswith("{")]
    best: dict[str, dict[str, Any]] = {}
    for row in rows:  # one entry per sstable; keep the largest per partition
        key = row["partition_key"]
        if key not in best or row["partition_size"] > best[key]["partition_size"]:
            best[key] = row
    return sorted(best.values(), key=lambda r: r["partition_size"], reverse=True)


def main() -> int:
    out, keyspace, container = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    plans: dict[str, dict[str, Any]] = {}
    for plan_file in sorted(out.glob("plan-*.json")):
        for scenario in json.loads(plan_file.read_text())["scenarios"]:
            plans[scenario["table"]] = scenario
    tables = [t for m in load_migrations() for t in m.tables]
    results: list[dict[str, Any]] = []
    for table in tables:
        max_row = int(
            _exec(
                container,
                "curl",
                "-sf",
                f"http://127.0.0.1:10000/column_family/metrics/max_row_size/{keyspace}:{table}",
            ).strip()
            or 0
        )
        large = _large_partitions(container, keyspace, table)
        exact = large[0] if large else None
        size = max(max_row, exact["partition_size"] if exact else 0)
        results.append(
            {
                "table": table,
                "scenario": plans.get(table, {}).get("code"),
                "what": plans.get(table, {}).get("what"),
                "logical_rows": plans.get(table, {}).get("logical_rows"),
                "shards": plans.get(table, {}).get("shards"),
                "shard_rows": plans.get(table, {}).get("shard_rows"),
                "max_partition_bytes": size,
                "exact_partition_bytes": exact["partition_size"] if exact else None,
                "rows_in_largest": exact["rows"] if exact else None,
                "bytes_per_row": round(exact["partition_size"] / exact["rows"])
                if exact and exact["rows"]
                else None,
                "over_gate": size > GATE_BYTES,
            }
        )
    summary = {"keyspace": keyspace, "gate_bytes": GATE_BYTES, "tables": results}
    (out / "partitions.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = [
        "| table | scenario | logical rows | shards | rows in largest | largest | B/row |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: r["max_partition_bytes"], reverse=True):
        mb = r["max_partition_bytes"] / 1e6
        lines.append(
            f"| {r['table']} | {r['scenario'] or '-'} | {r['logical_rows'] or '-'} | "
            f"{r['shards'] or '-'} | {r['rows_in_largest'] or '-'} | {mb:.2f} MB | "
            f"{r['bytes_per_row'] or '-'} |"
        )
    (out / "partitions.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    over = [r["table"] for r in results if r["over_gate"]]
    if over:
        print(f"GATE FAILED: partitions > 100 MB in {over}", file=sys.stderr)
        return 1
    print("gate: no partition > 100 MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
