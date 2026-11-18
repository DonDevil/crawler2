#!/usr/bin/env bash
# WP1 Run 2 / B1+B2: READ-ONLY NVMe health collection. Needs root for the admin passthru.
# Usage: sudo bash wp1r2_nvme_smart_collect.sh <out_dir> [owner_uid:gid]
# Issues only: smartctl -x/-a, nvme id-ctrl / smart-log / error-log / get-feature (no set-feature, no format, no fw ops).
set -u
OUT="${1:?out dir}"; OWNER="${2:-}"; DEV="${DEV:-/dev/nvme0}"
mkdir -p "$OUT"; cd "$OUT" || exit 1
run() { local name="$1"; shift; { echo "# $(date -Is) \$ $*"; "$@"; echo "# exit=$?"; } >"$name.txt" 2>&1; }
run smartctl-x           smartctl -x "$DEV"
run smartctl-json        smartctl -x -j "$DEV"
run nvme-id-ctrl         nvme id-ctrl "$DEV" -H
run nvme-id-ns           nvme id-ns "${DEV}n1" -H
run nvme-smart-log       nvme smart-log "$DEV"
run nvme-smart-log-json  nvme smart-log "$DEV" -o json
run nvme-smart-log-ext   nvme smart-log "$DEV" -v
run nvme-error-log       nvme error-log "$DEV" -e 64
run nvme-fw-log          nvme fw-log "$DEV"
run nvme-supported-logs  nvme supported-log-pages "$DEV"
run nvme-list            nvme list
# read-only get-feature: 0x02 power mgmt, 0x04 temp threshold, 0x0c APST, 0x10 HCTM, 0x11 NOPSC(if any)
for f in 0x01 0x02 0x04 0x06 0x07 0x08 0x0b 0x0c 0x10 0x11; do run "nvme-get-feature-$f" nvme get-feature "$DEV" -f $f -H; done
run nvme-get-feature-0x04-sel1 nvme get-feature "$DEV" -f 0x04 -s 1 -H
run nvme-get-feature-0x04-sensor1 nvme get-feature "$DEV" -f 0x04 -H --sel=0 --cdw11=0x10000
run lspci-link  bash -c "lspci -vv -s \$(basename \$(readlink -f /sys/class/nvme/nvme0/device)) | grep -E 'LnkCap|LnkSta|ASPM|Non-Volatile|Samsung'"
[ -n "$OWNER" ] && chown -R "$OWNER" "$OUT"
echo "done -> $OUT"; ls "$OUT" | wc -l
