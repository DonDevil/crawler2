#!/usr/bin/env bash
# Reproducible V1 baseline run (P0). V1 is used read-only:
#   * code comes from `git archive <V1_COMMIT>` into a scratch dir, because
#     V1 resolves datasets/ relative to cwd and mutates its blacklist at
#     runtime (plan A.2 D6) -- running in place would modify V1;
#   * V1's own virtualenv interpreter is used unchanged; psutil (needed by
#     V1's ResourceMonitor for CPU/RSS) is supplied on a side PYTHONPATH;
#   * frontier + media evidence use the crawler2 compose Redis, db 2,
#     bench_* namespaces (flushed first) -- never V1's production db.
#
# Usage: benchmarks/v1-baseline/run.sh [RUNTIME_MINUTES] [CONCURRENCY]
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
V1_REPO=${V1_REPO:-$HOME/anti_piracy/crawler}
V1_COMMIT=${V1_COMMIT:-2dfb542}
V1_PYTHON=${V1_PYTHON:-$V1_REPO/env/bin/python}
RUNTIME_MIN=${1:-10}
CONCURRENCY=${2:-50}
REDIS_PORT=${REDIS_PORT:-16379}
REDIS_DB=2
NS=bench_v1_baseline
WORK=${WORK:-$(mktemp -d)}
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$HERE/results/$RUN_ID"

mkdir -p "$WORK/v1" "$WORK/pydeps" "$OUT"
git -C "$V1_REPO" archive "$V1_COMMIT" | tar -x -C "$WORK/v1"
uv pip install -q --target "$WORK/pydeps" --python "$V1_PYTHON" psutil==7.1.0
cp "$HERE/seeds.txt" "$WORK/v1/seeds/baseline_seeds.txt"

"$V1_PYTHON" - "$WORK/v1/config.yaml" "$CONCURRENCY" "$REDIS_PORT" "$REDIS_DB" "$NS" <<'PY'
import sys, yaml
path, cc, port, db, ns = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
cfg = yaml.safe_load(open(path))
c = cfg["crawler"]
c["concurrency"] = cc
c["seed_files"] = ["seeds/baseline_seeds.txt"]
for section, suffix in (("frontier", ""), ("media_evidence", "_evidence")):
    c[section].update(type="redis", redis_host="127.0.0.1", redis_port=port, redis_db=db,
                      redis_namespace=ns + suffix)
yaml.safe_dump(cfg, open(path, "w"), sort_keys=False)
PY

redis-cli -p "$REDIS_PORT" -n "$REDIS_DB" FLUSHDB >/dev/null

{
  echo "run_id=$RUN_ID"
  echo "v1_commit=$(git -C "$V1_REPO" rev-parse "$V1_COMMIT")"
  echo "runtime_minutes=$RUNTIME_MIN concurrency=$CONCURRENCY engine=auto"
  echo "python=$("$V1_PYTHON" --version 2>&1)"
  echo "cpu=$(lscpu | sed -n 's/^Model name: *//p') cores=$(nproc)"
  echo "mem_total=$(free -h | awk '/Mem:/{print $2}') kernel=$(uname -r)"
  echo "gpu=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo none)"
} > "$OUT/environment.txt"
cp "$WORK/v1/config.yaml" "$OUT/config.yaml"

cd "$WORK/v1"
# /usr/bin/time: independent CPU (incl. reaped browser children) and max RSS.
PYTHONPATH="$WORK/pydeps" /usr/bin/time -v -o "$OUT/time.txt" "$V1_PYTHON" main.py \
  --runtime "$RUNTIME_MIN" --crawler-engine auto \
  --monitor-resources --monitor-interval 5 \
  --crawler-id "baseline-$RUN_ID" \
  --output "$OUT/v1_report.json" > "$OUT/crawl.log" 2>&1 || echo "V1 exited with status $?" >> "$OUT/environment.txt"

python3 "$HERE/analyze.py" "$OUT" --redis-port "$REDIS_PORT" --redis-db "$REDIS_DB" --namespace "$NS"
gzip -9 "$OUT/crawl.log"
echo "results: $OUT"
