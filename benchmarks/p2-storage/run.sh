#!/usr/bin/env bash
# P2 storage benchmarks. Needs the two-host profile up (`make up-two-host`).
#
#   benchmarks/p2-storage/run.sh partitions   # 1M-page worst-case partitions (~15 min)
#   benchmarks/p2-storage/run.sh latency      # p99 at 10x expected per-host load (~3 min)
#
# Writers run inside app-host-1/app-host-2 (only containers reach Scylla);
# flush/compaction and size statistics come from Scylla itself (REST API and
# system.large_partitions). Results: benchmarks/p2-storage/results/<UTC time>/.
set -euo pipefail
cd "$(dirname "$0")/../.."

COMPOSE=(${COMPOSE:-docker compose})
KS=${KS:-crawler2_bench}
SCALE=${SCALE:-1.0}
DURATION=${DURATION:-60}
out=benchmarks/p2-storage/results/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$out"

cid() { "${COMPOSE[@]}" ps -q "$1"; }
scylla=$(cid scylla)
h1=$(cid app-host-1)
h2=$(cid app-host-2)
[[ -n "$h1" && -n "$h2" ]] || { echo "two-host profile is not running" >&2; exit 1; }

cql() { docker exec "$scylla" sh -c "cqlsh \$(hostname -i) -e \"$1\""; }
api() { docker exec "$scylla" curl -sf -X "$1" "http://127.0.0.1:10000$2"; }
in_host() { local c=$1; shift; docker exec "$c" env PYTHONPATH=/app "$@"; }
threshold() {
  cql "UPDATE system.config SET value='$1' WHERE name='compaction_large_partition_warning_threshold_mb'" >/dev/null
}

partitions() {
  local bench=(python benchmarks/p2-storage/partitions.py write --keyspace "$KS" --scale "$SCALE")
  # Record every partition >= 1 MB with its exact size (runtime-only; restored on exit).
  threshold 1
  trap 'threshold 1000' EXIT
  in_host "$h1" "${bench[@]}" --reset --only NONE >/dev/null
  in_host "$h1" "${bench[@]}" --only S1a,S1b,S1c,S2,S3a,S5,S7 >"$out/plan-host-1.json" & p1=$!
  in_host "$h2" "${bench[@]}" --only S3b,S4a,S4b,S6a,S6b,S8,S9 >"$out/plan-host-2.json" & p2=$!
  wait "$p1" && wait "$p2"
  echo "flush + major compaction of $KS"
  api POST "/storage_service/keyspace_flush/$KS" >/dev/null
  api POST "/storage_service/keyspace_compaction/$KS" >/dev/null
  env/bin/python benchmarks/p2-storage/report.py "$out" "$KS" "$scylla"
}

# Load generators run in throwaway client containers (same image and network,
# identity of app-host-1/2) with CLIENT_CPUS each: a 1-CPU app container can
# only generate ~7 units/s of Python-side work, which would measure the client.
client() {
  local host_id=$1; shift
  local net
  net=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$h1")
  docker run --rm --network "$net" --cpus "${CLIENT_CPUS:-4}" --memory 3g \
    --env-file <(docker exec "$h1" env | grep '^CRAWLER2_' | grep -v '^CRAWLER2_HOST_ID=') \
    -e "CRAWLER2_HOST_ID=$host_id" -e PYTHONPATH=/app localhost/crawler2-app:dev "$@"
}

latency() {
  local bench=(python benchmarks/p2-storage/latency.py --keyspace "${KS}_latency" --duration "$DURATION" --rate "${RATE:-100}")
  in_host "$h1" "${bench[@]}" --reset --prepare-only >/dev/null
  iostat -dxy 10 9 >"$out/iostat.txt" 2>&1 &  # disk utilisation during the run
  client host-1 "${bench[@]}" --processes "${CLIENT_PROCS:-4}" >"$out/latency-host-1.json" & p1=$!
  client host-2 "${bench[@]}" --processes "${CLIENT_PROCS:-4}" >"$out/latency-host-2.json" & p2=$!
  wait "$p1" && wait "$p2"
  wait
  env/bin/python benchmarks/p2-storage/latency_report.py "$out" || true  # report, not a gate
}

case "${1:-}" in
  partitions) partitions ;;
  latency) latency ;;
  *) echo "usage: $0 partitions|latency" >&2; exit 2 ;;
esac
echo "results: $out"
