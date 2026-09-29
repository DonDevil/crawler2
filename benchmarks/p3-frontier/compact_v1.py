#!/usr/bin/env python3
"""Drop V1 distributed_benchmark's per-URL lists (tens of MB), keep every aggregate.

Usage: compact_v1.py FILE... (rewrites each file in place).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

for name in sys.argv[1:]:
    path = Path(name)
    data = json.loads(path.read_text())
    for worker in data.get("workers", {}).get("per_worker", []) or []:
        if worker and "success_urls" in worker:
            worker["success_urls_count"] = len(worker.pop("success_urls"))
    data.get("aggregate", {}).pop("duplicate_claim_details", None)
    path.write_text(json.dumps(data, indent=2))
