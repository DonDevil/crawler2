#!/usr/bin/env bash
# WP1 Run 2 / D5: run the UNMODIFIED E-R1 harness (fingerprinter/benchmarks/p10-index/capacity_e_r1.py)
# at a given copy count on a disposable Qdrant container (same image digest and limits as the gate:
# 1.5 GiB RAM + 1.5 GiB memsw, 2 CPUs). Unique container/volume names; the dev Qdrant is never touched.
#
# usage: wp1r2_qdrant_er1_run.sh COPIES OUT_DIR [STEADY_S=300]
# Watchdog (every 5 s): stops the container and marks the run ABORTED on NVMe >= 82 C, CPU package >= 99 C
# for 3 consecutive polls, or free space < 24 GiB (20 GiB floor + margin). Cleanup always runs.
set -u
COPIES="${1:?copies}"; OUT="${2:?out dir}"; STEADY="${3:-300}"
IMG="qdrant/qdrant@sha256:d122138f76868edba68d36cb0833139c1d1761f00f09e48e61f8314196e6a4c6"
NAME="wp1r2-qdrant-er1-${COPIES}x"; VOL="wp1r2-vol-er1-${COPIES}x"
HTTP=$((27000 + COPIES)); GRPC=$((28000 + COPIES))
KEY="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
mkdir -p "$OUT"
free_gib() { df -B1G --output=avail / | tail -1 | tr -d ' '; }
ctemp() { sensors 2>/dev/null | awk '/Package id 0/{gsub(/[+°C]/,"",$4); print int($4)}'; }
ntemp() { echo $(( $(cat /sys/class/nvme/nvme0/hwmon*/temp1_input | head -1) / 1000 )); }
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; docker volume rm "$VOL" >/dev/null 2>&1; kill "$WD" "$MON" 2>/dev/null; }
trap cleanup EXIT
[ "$(free_gib)" -ge 30 ] || { echo "ABORT: free space $(free_gib) GiB < 30 before start"; exit 3; }
docker ps -a --format '{{.Names}}' | grep -qx "$NAME" && { echo "container $NAME exists; refusing"; exit 2; }

CID=$(docker run -d --name "$NAME" --memory 1.5g --memory-swap 1.5g --cpus 2 \
  -p 127.0.0.1:${HTTP}:6333 -p 127.0.0.1:${GRPC}:6334 -v "$VOL":/qdrant/storage \
  -e QDRANT__TELEMETRY_DISABLED=true -e QDRANT__SERVICE__API_KEY="$KEY" "$IMG")
CG="/sys/fs/cgroup/system.slice/docker-${CID}.scope"
for _ in $(seq 1 30); do curl -sf -H "api-key: $KEY" "http://127.0.0.1:${HTTP}/readyz" >/dev/null && break; sleep 1; done
python3 -I "$(dirname "$0")/wp1_monitor.py" "$OUT/monitor-${COPIES}x.jsonl" --interval 2 >/dev/null 2>&1 & MON=$!
( hot=0; while sleep 5; do
    c=$(ctemp); [ "${c:-0}" -ge 99 ] && hot=$((hot+1)) || hot=0
    if [ "$hot" -ge 3 ] || [ "$(ntemp)" -ge 82 ] || [ "$(free_gib)" -lt 24 ]; then
      echo "WATCHDOG ABORT cpu=$c nvme=$(ntemp) free=$(free_gib)" | tee "$OUT/ABORTED-${COPIES}x.txt"; docker stop "$NAME" >/dev/null 2>&1; exit 0; fi
  done ) & WD=$!

cd ~/anti_piracy/fingerprinter || exit 1
FINGERPRINTER_INDEX__API_KEY="$KEY" ~/anti_piracy/datasets/p10/venv/bin/python -I benchmarks/p10-index/capacity_e_r1.py \
  --vectors ~/anti_piracy/datasets/p10/vectors --copies "$COPIES" --container "$NAME" \
  --http-port "$HTTP" --grpc-port "$GRPC" --steady-s "$STEADY" --cgroup "$CG" \
  --out "$OUT/e-r1-${COPIES}copies.json" --samples "$OUT/e-r1-${COPIES}copies.samples.jsonl" \
  > "$OUT/e-r1-${COPIES}copies.log" 2>&1
echo "harness exit=$?" >> "$OUT/e-r1-${COPIES}copies.log"
docker inspect "$NAME" --format 'OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}}' > "$OUT/container-final-state-${COPIES}x.txt" 2>&1
