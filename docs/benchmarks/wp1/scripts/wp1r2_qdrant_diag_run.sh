#!/usr/bin/env bash
# WP1 Run 2 / D3: launch a disposable gate-limit Qdrant (1.5 GiB + 1.5 GiB memsw, 2 CPUs, same image
# digest) and run wp1r2_qdrant_diag.py on it. Same watchdog/cleanup rules as wp1r2_qdrant_er1_run.sh.
# usage: wp1r2_qdrant_diag_run.sh OUT_DIR [COPIES=1]   (COPIES>1: steady-state scale mode)
set -u
OUT="${1:?out dir}"; COPIES="${2:-1}"; TAG=""; [ "$COPIES" != 1 ] && TAG="-${COPIES}x"
EXTRA=""; [ "${PAYLOAD_RAM:-0}" = 1 ] && { EXTRA="--payload-in-ram"; TAG="${TAG}-payload-ram"; }
IMG="qdrant/qdrant@sha256:d122138f76868edba68d36cb0833139c1d1761f00f09e48e61f8314196e6a4c6"
NAME="wp1r2-qdrant-diag${TAG}"; VOL="wp1r2-vol-diag${TAG}"; HTTP=27901; GRPC=27902
KEY="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
mkdir -p "$OUT"
free_gib() { df -B1G --output=avail / | tail -1 | tr -d ' '; }
ctemp() { sensors 2>/dev/null | awk '/Package id 0/{gsub(/[+°C]/,"",$4); print int($4)}'; }
ntemp() { echo $(( $(cat /sys/class/nvme/nvme0/hwmon*/temp1_input | head -1) / 1000 )); }
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; docker volume rm "$VOL" >/dev/null 2>&1; kill "$WD" "$MON" "$CGS" 2>/dev/null; }
trap cleanup EXIT
[ "$(free_gib)" -ge 30 ] || { echo "ABORT free space"; exit 3; }
docker run -d --name "$NAME" --memory 1.5g --memory-swap 1.5g --cpus 2 -p 127.0.0.1:${HTTP}:6333 -p 127.0.0.1:${GRPC}:6334 \
  -v "$VOL":/qdrant/storage -e QDRANT__TELEMETRY_DISABLED=true -e QDRANT__SERVICE__API_KEY="$KEY" "$IMG" >/dev/null
for _ in $(seq 1 30); do curl -sf -H "api-key: $KEY" "http://127.0.0.1:${HTTP}/readyz" >/dev/null && break; sleep 1; done
python3 -I "$(dirname "$0")/wp1_monitor.py" "$OUT/diag-monitor.jsonl" --interval 2 >/dev/null 2>&1 & MON=$!
( hot=0; while sleep 5; do c=$(ctemp); [ "${c:-0}" -ge 99 ] && hot=$((hot+1)) || hot=0
    if [ "$hot" -ge 3 ] || [ "$(ntemp)" -ge 82 ] || [ "$(free_gib)" -lt 24 ]; then echo "WATCHDOG ABORT" | tee "$OUT/ABORTED-diag.txt"; docker stop "$NAME" >/dev/null 2>&1; exit 0; fi; done ) & WD=$!
python3 -I "$(dirname "$0")/wp1r2_cgroup_sampler.py" "$OUT/diag-cgroup${TAG}.jsonl" 1500 "$NAME" >/dev/null 2>&1 & CGS=$!
~/anti_piracy/datasets/p10/venv/bin/python -I "$(dirname "$0")/wp1r2_qdrant_diag.py" --vectors ~/anti_piracy/datasets/p10/vectors \
  --port $HTTP --grpc-port $GRPC --api-key "$KEY" --copies "$COPIES" $EXTRA --out "$OUT/qdrant-latency-diag${TAG}.json" > "$OUT/qdrant-latency-diag${TAG}.log" 2>&1
echo "exit=$?" >> "$OUT/qdrant-latency-diag${TAG}.log"
