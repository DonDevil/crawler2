#!/usr/bin/env bash
# P4 fetch-layer benchmarks. V1 is used read-only: its code comes from
# `git archive <V1_COMMIT>` into a scratch dir and runs with V1's own
# interpreter; psutil is supplied on a side PYTHONPATH (as P0's run.sh).
#
#   benchmarks/p4-fetch/run.sh v1-engines [URL_FILE]   # per-engine eval (default: P0 seeds)
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
V1_REPO=${V1_REPO:-$HOME/anti_piracy/crawler}
V1_COMMIT=${V1_COMMIT:-2dfb542}
V1_PYTHON=${V1_PYTHON:-$V1_REPO/env/bin/python}
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$HERE/results/$RUN_ID"

v1_snapshot() {
  WORK=${WORK:-$(mktemp -d)}
  mkdir -p "$WORK/v1" "$WORK/pydeps"
  git -C "$V1_REPO" archive "$V1_COMMIT" | tar -x -C "$WORK/v1"
  uv pip install -q --target "$WORK/pydeps" --python "$V1_PYTHON" psutil==7.1.0
}

environment() {
  {
    echo "run_id=$RUN_ID"
    echo "v1_commit=$(git -C "$V1_REPO" rev-parse "$V1_COMMIT")"
    echo "crawler2_commit=$(git -C "$HERE/../.." rev-parse HEAD)"
    echo "cpu=$(lscpu | sed -n 's/^Model name: *//p') cores=$(nproc)"
    echo "mem_total=$(free -h | awk '/Mem:/{print $2}') kernel=$(uname -r)"
  } > "$OUT/environment.txt"
}

case "${1:-}" in
  v1-engines)
    URLS=${2:-$HERE/../v1-baseline/seeds.txt}
    mkdir -p "$OUT"; environment; v1_snapshot
    cd "$WORK/v1"
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$WORK/pydeps" \
      "$V1_PYTHON" "$HERE/v1_engines.py" "$URLS" --out "$OUT/v1_engines.json" \
      ${ENGINES:+--engines "$ENGINES"} 2>&1 | tee "$OUT/v1_engines.log"
    echo "results: $OUT"
    ;;
  *)
    echo "usage: $0 v1-engines [URL_FILE]" >&2; exit 2 ;;
esac
