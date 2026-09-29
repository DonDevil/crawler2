"""Compare the V1 and V2 gate runs with the design §28 definitions (fixed before measuring).

Gates: success rate V2 >= V1; bytes per success V2 <= V1; browser share
V2 < V1; media body downloads V2 == 0. Also the D14 re-check: pages V1
fetched only via Selenium that V2 did not fetch, with V2's HTTP status.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    run = Path(sys.argv[1])
    v1 = json.loads((run / "v1_hybrid.json").read_text())
    v2 = json.loads((run / "v2_gate.json").read_text())
    s1, s2 = v1["summary"], v2["summary"]
    v2_rows = {r["url"]: r for r in v2["rows"]}
    selenium_only = [
        {
            "url": r["url"],
            "v2_chain": v2_rows[r["url"]]["chain"],
            "v2_status": v2_rows[r["url"]]["status"],
        }
        for r in v1["rows"]
        if r["ok"] and r["engine"] == "selenium" and not v2_rows.get(r["url"], {}).get("ok")
    ]
    v1_ok = {r["url"] for r in v1["rows"] if r["ok"]}
    v2_ok = {r["url"] for r in v2["rows"] if r["ok"]}
    gates = {
        "success_rate": {
            "v1": s1["success_rate"],
            "v2": s2["success_rate"],
            "pass": s2["success_rate"] >= s1["success_rate"],
        },
        "bytes_per_success": {
            "v1": s1["bytes_per_success"],
            "v2": s2["bytes_per_success"],
            "pass": s2["bytes_per_success"] <= s1["bytes_per_success"],
        },
        "browser_share": {
            "v1": s1["browser_share"],
            "v2": s2["browser_share"],
            "pass": s2["browser_share"] < s1["browser_share"],
        },
        "media_body_downloads": {
            "v2": s2["media_body_downloads"],
            "pass": s2["media_body_downloads"] == 0,
        },
    }
    report = {
        "gates": gates,
        "all_pass": all(g["pass"] for g in gates.values()),
        "overlap": {
            "both": len(v1_ok & v2_ok),
            "v1_only": len(v1_ok - v2_ok),
            "v2_only": len(v2_ok - v1_ok),
            "neither": len({r["url"] for r in v1["rows"]} - v1_ok - v2_ok),
        },
        "v1_only_examples": sorted(v1_ok - v2_ok)[:40],
        "selenium_recheck": selenium_only,
        "v1": s1,
        "v2": s2,
    }
    (run / "gate.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: report[k] for k in ("gates", "all_pass", "overlap")}, indent=1))


if __name__ == "__main__":
    main()
