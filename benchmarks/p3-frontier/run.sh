#!/usr/bin/env bash
# P3 frontier benchmarks (docs/phases/p03-frontier-scheduling §21).
#
#   benchmarks/p3-frontier/run.sh redis        # start the throwaway benchmark Redis
#   benchmarks/p3-frontier/run.sh all          # everything below, one results dir
#   benchmarks/p3-frontier/run.sh throughput|compose|eligible|starvation|behaviour|million
#   benchmarks/p3-frontier/run.sh stop         # remove the benchmark Redis
#
# Benchmark Redis: same image as the compose stack, host network, AOF off,
# default `save` — the configuration V1's ceiling was measured on (host
# Redis 7.0, localhost TCP). The compose stack is not modified; `compose`
# re-measures V1 and V2 on the compose Redis (AOF on, 1 CPU, docker-proxy).
# V1 tools run from $V1_ROOT with V1's own venv and PYTHONDONTWRITEBYTECODE=1
# (V1 is read, never written).
set -euo pipefail
cd "$(dirname "$0")/../.."

V1=${V1_ROOT:-$HOME/anti_piracy/crawler}
PORT=${BENCH_REDIS_PORT:-16380}
PY=env/bin/python
V1PY=("env" "PYTHONDONTWRITEBYTECODE=1" "$V1/env/bin/python")
B=benchmarks/p3-frontier
out=${OUT:-$B/results/$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$out"

redis() {
  docker run -d --rm --name crawler2-bench-redis --network host \
    docker.io/library/redis:7.4.2-alpine redis-server --port "$PORT" --bind 127.0.0.1 \
    --appendonly no --save "3600 1 300 100 60 10000" >/dev/null
  sleep 1
}

v1_dist() {  # workers port db label
  (cd "$V1" && "${V1PY[@]}" tests/benchmarks/distributed_benchmark.py --workers "$1" \
    --urls 200000 --domains 40 --duration 30 --rate-limit 0 --redis-port "$2" \
    --redis-db "$3" --namespace "bench_v1ref_$RANDOM" --output "$OLDPWD/$out/v1-$4-w$1.json" \
    >/dev/null 2>&1)
  $PY $B/compact_v1.py "$out/v1-$4-w$1.json"
}

throughput() {
  for w in 1 2 4 8 16; do
    $PY $B/throughput.py --workers "$w" --redis-port "$PORT" --label bench-redis \
      --output "$out/v2-bench-w$w.json" >/dev/null
    v1_dist "$w" "$PORT" 2 bench
  done
  for rep in 2 3; do  # repeat the gate point
    $PY $B/throughput.py --workers 8 --redis-port "$PORT" --output "$out/v2-bench-w8-r$rep.json" >/dev/null
    v1_dist 8 "$PORT" 2 "bench-r$rep"
  done
}

compose() {
  $PY $B/throughput.py --workers 8 --redis-port 16379 --redis-db 3 --label compose-redis \
    --output "$out/v2-compose-w8.json" >/dev/null
  v1_dist 8 16379 3 compose
}

eligible() {
  $PY $B/eligible_index.py --redis-port "$PORT" --output "$out/eligible-index-v2.json" >/dev/null
  "${V1PY[@]}" $B/v1_scan_probe.py --v1-root "$V1" --redis-port "$PORT" \
    --output "$out/eligible-index-v1.json" 2>/dev/null
}

starvation() {
  $PY $B/starvation.py --redis-port "$PORT" --output "$out/starvation-v2.json" >/dev/null
  # V1's scan-limit-window scenario, corrected to run with politeness on (see starvation.py).
  (cd "$V1" && "${V1PY[@]}" tests/benchmarks/domain_starvation.py scan-limit-window \
    --frontier redis --domain-count 260 --domain-scan-limit 250 --num-claims 800 \
    --replenish-batch 1 --rate-limit 1.0 --max-idle-polls 500 --redis-port "$PORT" \
    --redis-db 2 --output "$OLDPWD/$out/starvation-v1-scan-window.json" >/dev/null 2>&1)
}

behaviour() {
  $PY $B/priority_ratelimit.py --redis-port "$PORT" --output "$out/priority-ratelimit.json" >/dev/null
  $PY $B/crash_recovery.py --redis-port "$PORT" --output "$out/crash-recovery.json" >/dev/null
  $PY $B/heartbeat_endurance.py --redis-port "$PORT" --output "$out/heartbeat-endurance.json" >/dev/null
}

million() {
  $PY $B/distributed_1m.py --redis-port "$PORT" --label bench-redis \
    --output "$out/distributed-1m.json" >/dev/null
}

case "${1:-all}" in
  redis) redis ;;
  stop) docker rm -f crawler2-bench-redis >/dev/null ;;
  all) throughput; compose; eligible; starvation; behaviour; million ;;
  throughput|compose|eligible|starvation|behaviour|million) "$1" ;;
  *) echo "unknown target: $1" >&2; exit 2 ;;
esac
echo "results: $out"
