"""Summarise M1 process logs into a small text file kept with the results.

The raw logs (``var/p6-m1/logs``, tens of MB per run) stay git-ignored; this
keeps what the reports rely on: per process, the line count per log level,
the most frequent warning/error events, and the exceptions that ended
tracebacks (class plus message head, with the Scylla table when present).

    env/bin/python benchmarks/p6-m1/logsummary.py var/p6-m1/logs > results/run2/log-summary.txt
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

LEVEL = re.compile(r"^(\S+ \S+) \[(\w+)\s*\] (\S+)")
EXCEPTION = re.compile(r"^([a-z_][\w.]*\.)?([A-Z]\w*(?:Error|Exception|Timeout|TimedOut)\w*): (.*)")
TABLE = re.compile(r"for (crawler2_m1\.\w+)")


def summarise(path: Path) -> list[str]:
    levels: Counter[str] = Counter()
    events: Counter[str] = Counter()
    exceptions: Counter[str] = Counter()
    first = last = ""
    with path.open(errors="replace") as f:
        for line in f:
            m = LEVEL.match(line)
            if m:
                first = first or m.group(1)
                last = m.group(1)
                levels[m.group(2)] += 1
                if m.group(2) in ("warning", "error", "critical"):
                    events[f"{m.group(2)} {m.group(3)}"] += 1
                continue
            e = EXCEPTION.match(line)
            if e:
                table = TABLE.search(e.group(3))
                head = table.group(1) if table else e.group(3)[:70]
                exceptions[f"{e.group(2)}: {head}"] += 1
    out = [f"== {path.stem}  ({first} .. {last}, local time)"]
    out.append("   levels: " + ", ".join(f"{k} {v}" for k, v in levels.most_common()))
    out += [f"   {n:7d}  {k}" for k, n in events.most_common(12)]
    if exceptions:
        out.append("   exceptions (each traceback counts its chained causes):")
        out += [f"   {n:7d}  {k}" for k, n in exceptions.most_common(12)]
    return out


def main() -> None:
    logs = Path(sys.argv[1])
    for path in sorted(logs.glob("*.log")):
        print("\n".join(summarise(path)))


if __name__ == "__main__":
    main()
