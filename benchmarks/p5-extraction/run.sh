#!/usr/bin/env bash
# P5 benchmark driver (docs/phases/p05-extraction-page-intelligence/benchmarks.md).
#   benchmarks/p5-extraction/run.sh capture <urls> <out>   # HTML capture via the P4 runtime
#   benchmarks/p5-extraction/run.sh parse <capture> [rounds]  # V1 vs V2 CPU/page, alternating
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
v1="${V1_REPO:-$HOME/anti_piracy/crawler}"
case "${1:-}" in
  capture)
    CRAWLER2_REDIS__HOST="${CRAWLER2_REDIS__HOST:-127.0.0.1}" \
    CRAWLER2_REDIS__PORT="${CRAWLER2_REDIS__PORT:-16379}" \
      "$repo/env/bin/python" "$here/capture.py" "$2" --out "$3" ;;
  parse)
    capture="$(realpath "$2")"; rounds="${3:-2}"
    out="$here/results/$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$out"
    { uname -a; lscpu | grep -E 'Model name|^CPU\(s\)|MHz'; free -m | head -2; } > "$out/environment.txt"
    for r in $(seq 1 "$rounds"); do
      (cd "$v1" && PYTHONDONTWRITEBYTECODE=1 env/bin/python "$here/v1_parse.py" "$capture" --out "$out/v1.round$r.json")
      "$repo/env/bin/python" "$here/v2_parse.py" "$capture" --out "$out/v2.round$r.json"
    done
    python3 "$here/compare.py" "$out" ;;
  *) echo "usage: $0 capture <urls> <out> | parse <capture> [rounds]" >&2; exit 2 ;;
esac
