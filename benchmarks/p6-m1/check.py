"""One-shot M1 health check for periodic operator checks (Gate G).

Prints the window progress and the last hour: process liveness, exits,
completions, fetch outcomes, extraction/admission lag, RSS, host iowait,
free disk. Exit status 1 when a supervisor is down or the newest monitor
sample is older than five minutes, so a scheduled check can flag it.
Reads only ``var/p6-m1/`` (no Redis, Scylla or MinIO access).

    benchmarks/p6-m1/run.sh check
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VAR = ROOT / "var" / "p6-m1"
WINDOW_H = 24.0


def alive(pid_file: Path) -> bool:
    try:
        os.kill(int(pid_file.read_text()), 0)
    except (OSError, ValueError):
        return False
    return True


def tail(path: Path, n: int) -> list[str]:
    """The last ``n`` lines, read from the end (samples.jsonl grows to tens of MB)."""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = min(size, n * 40_000)
        f.seek(size - block)
        return f.read().decode(errors="replace").splitlines()[-n:]


def main() -> int:
    now = datetime.now(UTC)
    problems = []
    down = [p.stem for p in sorted((VAR / "pids").glob("*.pid")) if not alive(p)]
    if down:
        problems.append(f"DOWN: {', '.join(down)}")
    window = (VAR / "window_start").read_text().strip()
    start = datetime.fromisoformat(window.replace("Z", "+00:00"))
    elapsed = now - start
    samples = [json.loads(line) for line in tail(VAR / "samples.jsonl", 61)]
    last = samples[-1]
    age = (now - datetime.fromisoformat(last["at"])).total_seconds()
    if age > 300:
        problems.append(f"monitor stale: last sample {age:.0f} s old")
    hour_ago = [s for s in samples if now - datetime.fromisoformat(s["at"]) <= timedelta(hours=1)]
    first = hour_ago[0] if hour_ago else last

    cutoff = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    log = (VAR / "restarts.log").read_text().splitlines()
    exits_1h = Counter(r.split()[2] for r in log if " exit " in r and r.split()[0] >= cutoff)
    exits_window = Counter(r.split()[2] for r in log if " exit " in r and r.split()[0] >= window)

    def counter(s: dict[str, Any], name: str) -> int:
        return int(s.get("frontier", {}).get("counters", {}).get(name, 0))

    fetch: Counter[str] = Counter()
    status: Counter[str] = Counter()
    attempts = missed = 0
    for s in hour_ago:
        f = s.get("fetch") or {}
        attempts += f.get("attempts", 0)
        missed += f.get("missed", 0)
        fetch.update(f.get("outcome", {}))
        status.update({k: v for k, v in f.get("status", {}).items() if k.endswith("xx")})
    if missed:
        problems.append(f"monitor missed {missed} fetch.completed entries (stream trimmed)")
    lag = {
        f"{name}/{group}": g.get("lag")
        for name, st in last.get("streams", {}).items()
        for group, g in st.get("groups", {}).items()
    }
    rss = {name: p.get("tree_rss_mb", p.get("error")) for name, p in last["processes"].items()}
    disk = next((s["disk"] for s in reversed(samples) if "disk" in s), None)
    host = last.get("host", {})
    iowait = [s["host"]["iowait_pct"] for s in hour_ago if "host" in s]

    pct = 100 * elapsed / timedelta(hours=WINDOW_H)
    hours = elapsed.total_seconds() / 3600
    print(f"M1 window {window}: {hours:.2f} h of {WINDOW_H:.0f} ({pct:.0f} %)")
    print(f"processes: {'all up' if not down else 'DOWN ' + ', '.join(down)}")
    print(f"exits last 1 h: {dict(exits_1h) or 'none'}; in window: {dict(exits_window) or 'none'}")
    print(
        f"last 1 h: {counter(last, 'completed') - counter(first, 'completed')} completed, "
        f"{counter(last, 'retried') - counter(first, 'retried')} retried, "
        f"dead letters {last.get('frontier', {}).get('dead_letters')}, "
        f"depth {last.get('frontier', {}).get('depth', {}).get('http')}"
    )
    if attempts:
        print(
            f"fetches last 1 h: {attempts} ({dict(fetch.most_common())}); status "
            f"{dict(status.most_common())}; 2xx {100 * status['2xx'] / attempts:.1f} %"
        )
    print(f"stream lag: {lag}")
    print(f"tree RSS MB: {rss}")
    if iowait:
        print(
            f"host: iowait mean {sum(iowait) / len(iowait):.0f} % (1 h), "
            f"load {host.get('load_1m')}, "
            f"available {host.get('mem_available_mb')} MB, swap {host.get('swap_used_mb')} MB"
        )
    if disk:
        print(f"disk: {disk}")
    if last.get("errors"):
        print(f"monitor probe errors: {last['errors']}")
    print("OK" if not problems else "PROBLEM: " + "; ".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
