#!/usr/bin/env python3
# ruff: noqa: E501, RUF002
"""Tiered-retention capacity model (H-08, owner decision: tiered retention) — formulas + measured inputs, no invented workload.

    required_bytes(tier, category) = unit_bytes × units_per_day × retention_days × safety_factor
    (index categories:             points × bytes_per_point × safety_factor)

``unit_bytes`` come from measurements (``MEASURED`` below — each with its source). ``units_per_day`` and
``retention_days`` are OWNER INPUTS: they are ``None`` until the owner supplies them, and a category with a missing
input is reported as ``UNDEFINED`` (never a made-up number). Supply inputs in a JSON file:

    {"safety_factor": 1.3,
     "workload": {"page_revisions": {"per_day": 1e6, "retention_days": 30}, ...}}

    python3 benchmarks/p2-storage/capacity_model.py [--inputs inputs.json] [--out results/wp1-capacity-model.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# category -> tier assignment (owner decision: tiered retention) and the measured unit size, with provenance.
CATEGORIES: dict[str, dict[str, Any]] = {
    "active_operational": {
        "tier": "SSD/NVMe",
        "unit": "page revision (Scylla)",
        "unit_bytes": None,
        "source": "NOT MEASURED per record: dev Scylla holds 561.8 MB total (docker system df, 2026-11-22), "
        "row counts not taken; P2 bench: largest partition 43.4 MB per 1M pages",
    },
    "active_qdrant_index": {
        "tier": "SSD/NVMe",
        "unit": "point (dinov2-seg/2, 768-d fp32 + payload + HNSW + int8)",
        "unit_bytes": 6018,
        "source": "fingerprinter e-r1-diag*.json: 628,502,393 B / 104,443 points "
        "(F15 host, Qdrant 1.15.1, one generation; rebuild needs 2 generations resident)",
    },
    "recent_snapshots": {
        "tier": "SSD/NVMe",
        "unit": "snapshot object (MinIO)",
        "unit_bytes": None,
        "source": "NOT MEASURED (dev MinIO 253 kB)",
    },
    "historical_snapshots": {
        "tier": "HDD/bulk",
        "unit": "snapshot object (MinIO)",
        "unit_bytes": None,
        "source": "NOT MEASURED",
    },
    "bulk_evidence": {
        "tier": "HDD/bulk",
        "unit": "evidence object",
        "unit_bytes": None,
        "source": "NOT MEASURED",
    },
    "long_retention_evidence": {
        "tier": "separately retained",
        "unit": "evidence object",
        "unit_bytes": None,
        "source": "NOT MEASURED",
    },
    "logs_temp": {
        "tier": "explicit retention policy",
        "unit": "GiB/day",
        "unit_bytes": None,
        "source": "NOT MEASURED",
    },
}

MEASURED_DEVICES = {  # tier bandwidth evidence (A15, file-based fio, see wp1-fio-*.json) — different conditions, not a like-for-like comparison
    "SSD/NVMe /mnt/data (nvme0n1p9)": "results/wp1-fio-a15-nvme-mnt-data-p9.json",
    "HDD /media/dhanush/Media (sda1, USB, rotational)": "results/wp1-fio-a15-usb-hdd-media.json",
}


def required(cat: dict[str, Any], w: dict[str, Any] | None, sf: float | None) -> str | float:
    if cat["unit_bytes"] is None or not w or sf is None:
        return "UNDEFINED"
    if "points" in w:
        return float(cat["unit_bytes"] * w["points"] * sf)
    if w.get("per_day") is None or w.get("retention_days") is None:
        return "UNDEFINED"
    return float(cat["unit_bytes"] * w["per_day"] * w["retention_days"] * sf)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", type=Path)
    ap.add_argument(
        "--out", type=Path, default=Path(__file__).parent / "results" / "wp1-capacity-model.json"
    )
    a = ap.parse_args()
    inp = json.loads(a.inputs.read_text()) if a.inputs else {}
    sf = inp.get("safety_factor")
    rows: dict[str, dict[str, Any]] = {}
    for name, c in CATEGORIES.items():
        r = required(c, (inp.get("workload") or {}).get(name), sf)
        rows[name] = {
            **c,
            "workload_input": (inp.get("workload") or {}).get(name),
            "required_bytes": r,
        }
    out = {
        "model": "wp1-capacity/1",
        "formula": __doc__.split("\n")[2].strip(),
        "safety_factor": sf,
        "inputs_supplied": bool(inp),
        "categories": rows,
        "tier_bandwidth_evidence": MEASURED_DEVICES,
        "status": "UNDEFINED until workload (units/day), retention (days) and safety factor are supplied by the owner",
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1))
    for n, row in rows.items():
        print(
            f"{n:26s} {row['tier']:26s} unit={row['unit_bytes']!s:>6}  required={row['required_bytes']}"
        )


if __name__ == "__main__":
    main()
