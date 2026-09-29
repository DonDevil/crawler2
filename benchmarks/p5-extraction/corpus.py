"""Corpus loader shared by the V1 and V2 parse benchmarks (stdlib only: runs in both venvs).

A corpus is a capture directory (``capture.py``): ``manifest.jsonl`` plus
``bodies/<sha256>``. The workload is every 2xx HTML body, in manifest order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Page:
    url: str
    final_url: str
    content_type: str | None
    body: bytes
    sha256: str


def load(capture: Path) -> list[Page]:
    pages = []
    for line in (capture / "manifest.jsonl").read_text().splitlines():
        row = json.loads(line)
        status = row.get("status") or 0
        if "sha256" not in row or not 200 <= status < 300:
            continue
        if "html" not in (row.get("content_type") or "").lower():
            continue
        body = (capture / "bodies" / row["sha256"]).read_bytes()
        pages.append(
            Page(
                row["url"],
                row.get("final_url") or row["url"],
                row["content_type"],
                body,
                row["sha256"],
            )
        )
    return pages


def summarize(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    n = len(ordered)
    return {
        "pages": n,
        "median_ms": round(ordered[n // 2], 3),
        "p95_ms": round(ordered[min(n - 1, int(n * 0.95))], 3),
        "mean_ms": round(sum(ordered) / n, 3),
    }
