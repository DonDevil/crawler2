#!/usr/bin/env bash
# WP1 Run 2 / B3: bounded canonical fio baseline on the Ubuntu ext4 root. File-based only, never a block device.
# usage: [ONLY=regex] [REP=-r1] [COOL_TO=55] [CPU_COOL_TO=65] wp1r2_fio_suite.sh OUT_DIR [WORKDIR=$HOME/wp1r2-fio-tmp] [SIZE=4G] [RUNTIME=20]
# REP: suffix for result files so the same job can be repeated (variance); ONLY: run only matching job names.
# Safety (enforced during every job): NVMe >= 81 C at 4 Hz (controller thermal-management TMT1 = 81 C: the job is stopped,
# its partial output discarded and logged as ABORT_THERMAL; the suite continues with the next job), CPU package sustained
# >= 95 C for 3 one-second polls or >= 100 C (same handling), free space < 28 GiB (suite exits).
set -u
OUT="${1:?out dir}"; WORK="${2:-$HOME/wp1r2-fio-tmp}"; SIZE="${3:-4G}"; RT="${4:-20}"
case "$WORK" in "$HOME"/wp1r2-*) ;; *) echo "refusing WORKDIR outside \$HOME/wp1r2-*"; exit 2;; esac
mkdir -p "$OUT" "$WORK"; F="$WORK/fio.dat"
NT=$(ls /sys/class/nvme/nvme0/hwmon*/temp1_input | head -1)
ntemp() { echo $(( $(cat "$NT") / 1000 )); }
free_gib() { df -B1G --output=avail / | tail -1 | tr -d ' '; }
ctemp() { sensors 2>/dev/null | awk '/Package id 0/{gsub(/[+°C]/,"",$4); print int($4)}'; }
cool() { # start each job from a comparable thermal state (NVMe and CPU)
  local n=0
  while [ "$(ntemp)" -gt "${COOL_TO:-55}" ] || [ "$(ctemp)" -gt "${CPU_COOL_TO:-65}" ]; do sleep 5; n=$((n+1)); [ $n -gt 120 ] && { echo "ABORT cannot cool"; exit 5; }; done; }
job() { # name, args...
  local n="$1"; shift
  [[ "$n" =~ $ONLY ]] || return 0
  local res="$OUT/$n${REP:-}.json"
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$res" 2>/dev/null && { echo "skip $n${REP:-} (result exists)"; return 0; }
  [ "$(free_gib)" -ge 28 ] || { echo "ABORT free space"; exit 3; }
  [ "$(ntemp)" -lt 80 ] || { echo "ABORT nvme temp $(ntemp)"; exit 4; }
  cool
  local t0=$(ntemp)
  fio --name="$n" --filename="$F" --size="$SIZE" --ioengine=io_uring --direct=1 --time_based --runtime="$RT" \
      --ramp_time=3 --group_reporting --lat_percentiles=1 --percentile_list=50:95:99:99.9 --output-format=json \
      --output="$res" "$@" &
  local pid=$!
  local hot=0 c nmax=0 m i=0
  while kill -0 $pid 2>/dev/null; do
    m=$(cat "$NT"); [ "$m" -gt "$nmax" ] && nmax=$m
    i=$((i+1))
    if [ $((i % 4)) -eq 0 ]; then c=$(ctemp); if [ "${c:-0}" -ge 95 ]; then hot=$((hot+1)); else hot=0; fi; fi
    if [ "$hot" -ge 3 ] || [ "${c:-0}" -ge 100 ] || [ "$m" -ge 81000 ]; then
      kill $pid; wait $pid 2>/dev/null; rm -f "$res"
      echo "ABORT_THERMAL $n${REP:-}: cpu=${c:-?} (hot_polls=$hot) nvme_mC=$m (partial output discarded)" | tee -a "$OUT/suite.log"; return 0; fi
    sleep 0.25
  done; wait $pid
  local flag=""; [ "$nmax" -ge 80000 ] && flag=" NEAR_THROTTLE"
  echo "$n${REP:-} t_before=$t0 t_after=$(ntemp) nvme_max_mC=$nmax$flag free=$(free_gib)GiB mem_avail_MiB=$(( $(awk '/MemAvailable/{print $2}' /proc/meminfo)/1024 ))" | tee -a "$OUT/suite.log"
}
fio --version > "$OUT/fio-version.txt"
# prefill once (sequential write, not timed as a result)
ONLY="${ONLY:-.}"   # regex: run only matching job names (for resuming after an abort)
fio --name=prefill --filename="$F" --size="$SIZE" --rw=write --bs=1M --direct=1 --ioengine=io_uring >/dev/null
job seqread-1M-qd8    --rw=read  --bs=1M --iodepth=8
job seqwrite-1M-qd8   --rw=write --bs=1M --iodepth=8
for qd in 1 4 16 32 64; do job randread-4k-qd$qd  --rw=randread  --bs=4k --iodepth=$qd; done
for qd in 1 4 16 32 64; do job randwrite-4k-qd$qd --rw=randwrite --bs=4k --iodepth=$qd; done
job randread-4k-qd32-j4  --rw=randread  --bs=4k --iodepth=32 --numjobs=4
job randwrite-4k-qd32-j4 --rw=randwrite --bs=4k --iodepth=32 --numjobs=4
job mixed-4k-70r30w-qd16-j4 --rw=randrw --rwmixread=70 --bs=4k --iodepth=16 --numjobs=4
rm -f "$F"; rmdir "$WORK" 2>/dev/null; echo finished >> "$OUT/suite.log"
