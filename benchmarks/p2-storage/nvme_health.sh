#!/usr/bin/env bash
# READ-ONLY NVMe/system health snapshot for WP1 (needs root for SMART, nvme admin reads, dmidecode, RAPL).
# Never writes to a device, never changes settings. Usage: sudo bash benchmarks/p2-storage/nvme_health.sh <tag>
set -u
TAG=${1:?tag (e.g. before|after)}
DEV=${DEV:-/dev/nvme0}
OUT="$(cd "$(dirname "$0")" && pwd)/results/wp1-nvme-health-$TAG.txt"
mkdir -p "$(dirname "$OUT")"
{
  echo "# wp1-nvme-health $TAG  $(date -u +%FT%TZ)  host=$(hostname)  kernel=$(uname -r)"
  echo "## nvme list";            nvme list
  echo "## nvme id-ctrl";         nvme id-ctrl "$DEV"
  echo "## nvme smart-log";       nvme smart-log "$DEV"
  echo "## nvme error-log";       nvme error-log "$DEV"
  echo "## nvme get-feature power-management (fid 2)"; nvme get-feature "$DEV" -f 2 -H
  echo "## smartctl -x";          smartctl -x "${DEV}n1"
  echo "## aer counters";         grep -H . /sys/class/nvme/nvme0/device/aer_dev_* 2>/dev/null
  echo "## dmidecode memory";     dmidecode -t memory | grep -E 'Size|Speed|Type:|Manufacturer|Locator' | grep -v -i 'no module'
  echo "## dmesg AER/nvme/ext4";  dmesg | grep -i -E 'aer|nvme|ext4-fs error|i/o error' | tail -50
} >"$OUT" 2>&1
chown "${SUDO_UID:-$(id -u)}:${SUDO_GID:-$(id -g)}" "$OUT" 2>/dev/null
echo "wrote $OUT"
