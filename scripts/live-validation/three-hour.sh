#!/usr/bin/env bash
# Three-hour real-webpage validation supervisor (single host, dev stack).
#
#   scripts/live-validation/three-hour.sh preflight     # checks only; starts nothing
#   scripts/live-validation/three-hour.sh plan          # prints the exact commands; starts nothing
#   scripts/live-validation/three-hour.sh run           # preflight, start roles, 10,800 s window, ordered shutdown
#   touch $RUN_DIR/STOP_REQUESTED                       # operator early stop (same ordered shutdown)
#
# Uses only the documented role entry points (deploy/systemd/run-role, fingerprinter
# docs/v2-encoder.md, docs/p10-matcher.md). Project Redis only (127.0.0.1:16379);
# the system Redis on 6379 is never contacted. Operational procedure and rationale:
# docs/phases/p14-production-hardening/validation.md "Three-hour live validation plan".
#
# STUB=1 replaces every role with an idle process that handles SIGTERM, so the
# sequencing/timing logic can be tested without touching any service.
set -uo pipefail

C2=${C2:-$HOME/anti_piracy/crawler2}
FP=${FP:-$HOME/anti_piracy/fingerprinter}
QUERY_FILE=${QUERY_FILE:-$HOME/Documents/query.txt}
TARGET_ID=${TARGET_ID:-tgt_01a1dfb096c67b3fab003ec6677832e7}
DURATION_S=${DURATION_S:-10800}      # crawl window: from search start to producer SIGTERM
DRAIN_S=${DRAIN_S:-300}              # producers stopped, consumers still running (0 = none)
TERM_WAIT_S=${TERM_WAIT_S:-60}       # per shutdown phase; matches the role unit's TimeoutStopSec
SEARCH_EVERY_S=${SEARCH_EVERY_S:-3600}
ENCODER_DEVICE=${ENCODER_DEVICE:-cuda}  # preflight refuses unless `runtime admit-cuda` was run; ENCODER_DEVICE=cpu overrides
MIN_FREE_GB=${MIN_FREE_GB:-15}
CPU_WARN_C=${CPU_WARN_C:-90}; CPU_STOP_C=${CPU_STOP_C:-97}
GPU_WARN_C=${GPU_WARN_C:-80}; GPU_STOP_C=${GPU_STOP_C:-87}
MEM_WARN_MIB=${MEM_WARN_MIB:-2048}; MEM_STOP_MIB=${MEM_STOP_MIB:-1024}   # MemAvailable
SWAP_WARN_MIB=${SWAP_WARN_MIB:-1024}
SAMPLE_S=${SAMPLE_S:-30}; SNAPSHOT_S=${SNAPSHOT_S:-300}
STUB=${STUB:-0}
RUN_DIR=${RUN_DIR:-$HOME/anti_piracy/monitoring_runs/live3h_$(date -u +%Y%m%dT%H%M%SZ)}
REDIS_CLI=(docker exec crawler2-redis-1 redis-cli)   # the project Redis container, never :6379 on the host

# ---- environment (explicit; no test-prefixed names) ---------------------------------
set -a; . "$C2/.env"; set +a
SCYLLA_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' crawler2-scylla-1 2>/dev/null)
export CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export CRAWLER2_SCYLLA__CONTACT_POINTS=$SCYLLA_IP
export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:${MINIO_HOST_PORT:-19000}
export CRAWLER2_MINIO__ACCESS_KEY=$MINIO_ROOT_USER CRAWLER2_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD
export CRAWLER2_HOST_ID=live3h-1 CRAWLER2_SCRATCH_DIR=${CRAWLER2_SCRATCH_DIR:-$HOME/anti_piracy/var-live3h/crawler2-scratch}
export CRAWLER2_SEARCH__ENGINES=duckduckgo,bing,brave          # ahmia/Tor excluded: Tor is not running
export CRAWLER2_WORKERS__HTTP__CONCURRENCY=16
export CRAWLER2_WORKERS__BROWSER__CONCURRENCY=2 CRAWLER2_BROWSER__CONTEXTS=2
export CRAWLER2_MEDIA__PROBE_CONCURRENCY=4 CRAWLER2_MEDIA__HASH_CONCURRENCY=2 CRAWLER2_MEDIA__HASH_PER_HOST_CONCURRENCY=1
unset CRAWLER2_FEEDBACK__ENABLED                                # P11 admission boost stays off (OD-2)
export FINGERPRINTER_SCYLLA__CONTACT_POINTS=$SCYLLA_IP FINGERPRINTER_SCYLLA__KEYSPACE=fingerprinter
export FINGERPRINTER_REDIS__HOST=127.0.0.1 FINGERPRINTER_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export FINGERPRINTER_MINIO__ENDPOINT=127.0.0.1:${MINIO_HOST_PORT:-19000}
export FINGERPRINTER_MINIO__ACCESS_KEY=$MINIO_ROOT_USER FINGERPRINTER_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD
export FINGERPRINTER_HOST_ID=live3h-1 FINGERPRINTER_SCRATCH_DIR=$HOME/anti_piracy/var-live3h/fp-scratch
export FINGERPRINTER_ENCODER__MAX_BATCH=32 FINGERPRINTER_ENCODER__DECODE_THREADS=2
set -a; . "$FP/deploy/qdrant/.env"; set +a
export FINGERPRINTER_INDEX__URL=http://127.0.0.1:16333
unset FINGERPRINTER_ROLES
mkdir -p "$CRAWLER2_SCRATCH_DIR" "$FINGERPRINTER_SCRATCH_DIR"

fpy() { (cd "$FP" && exec env/bin/python -m fingerprinter "$@"); }

# name|cwd|metrics-port-env|command   (start order == this order; shutdown order is in shutdown())
ROLES=(
  "relay|$C2||env/bin/crawler2-storage relay"
  "admit|$C2||env/bin/crawler2-discover admit"
  "extract|$C2||env/bin/crawler2-extract"
  "resolve|$C2||env/bin/crawler2-media resolve run"
  "probe|$C2||env/bin/crawler2-media probe run"
  "hash|$C2||env/bin/crawler2-media hash run"
  "representations|$C2||env/bin/crawler2-media representations run"
  "fp-reconcile|$FP|9201|env/bin/python -m fingerprinter reconcile run"
  "fp-intake|$FP|9202|env/bin/python -m fingerprinter intake run"
  "fp-scheduler|$FP|9203|env/bin/python -m fingerprinter scheduler run"
  "fp-encoder|$FP|9204|env/bin/python -m fingerprinter encoder run --device $ENCODER_DEVICE"
  "fp-match-reconcile|$FP|9205|env/bin/python -m fingerprinter matcher reconcile"
  "fp-match|$FP|9206|env/bin/python -m fingerprinter matcher run"
  "feedback|$C2||env/bin/crawler2-intel feedback run"
  "evidence|$C2||env/bin/crawler2-evidence collect run"
  "worker-http|$C2||env/bin/crawler2-worker --pool http"
  "worker-browser|$C2||env/bin/crawler2-worker --pool browser"
  "search|$C2||env/bin/crawler2-discover search --queries $QUERY_FILE --every $SEARCH_EVERY_S"
)
declare -A PID
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$RUN_DIR/supervisor.log" >&2; }
alert() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$RUN_DIR/alerts.log"; log "ALERT $*"; }

preflight() {
  local ok=0 bad=0
  chk() { if eval "$2" >/dev/null 2>&1; then echo "PASS $1"; ok=$((ok+1)); else echo "FAIL $1"; bad=$((bad+1)); fi; }
  chk "core containers healthy" '[ "$(docker ps --filter health=healthy --format "{{.Names}}" | grep -cE "crawler2-(redis|scylla|minio)-1|fingerprinter-index-qdrant-1")" = 4 ]'
  chk "qdrant /healthz" 'curl -fs http://127.0.0.1:16333/healthz'
  chk "qdrant rejects unauthenticated" '[ "$(curl -s -o /dev/null -w %{http_code} http://127.0.0.1:16333/collections)" = 401 ]'
  chk "disk free >= ${MIN_FREE_GB} GB" "[ \$(df -BG --output=avail $HOME | tail -1 | tr -dc 0-9) -ge $MIN_FREE_GB ]"
  chk "query file has 4 non-empty lines" "[ \$(grep -c . $QUERY_FILE) = 4 ]"
  chk "engines resolve to duckduckgo,bing,brave" "[ \"\$(cd $C2 && env/bin/python -c 'from crawler2.core.configuration import Settings;print(\",\".join(Settings().search.engines))')\" = duckduckgo,bing,brave ]"
  chk "target $TARGET_ID READY" "fpy target show $TARGET_ID | grep -q '\"state\": \"READY\"'"
  chk "fingerprinter intake initialised" "fpy intake status | grep -q '\"initialized\": true'"
  chk "index: target+media generations ACTIVE" "fpy index status | python3 -c 'import sys,json;d=json.load(sys.stdin)[\"specs\"];sys.exit(0 if all(v[r][\"active_generation\"]>0 for v in d.values() for r in (\"target\",\"media\")) else 1)'"
  chk "runtime admits device $ENCODER_DEVICE" "fpy runtime check --spec \$(fpy spec list | python3 -c 'import sys,json;print([s for s in json.load(sys.stdin) if s[\"default\"]][0][\"spec_digest\"])') --device $ENCODER_DEVICE | grep -q 'allowed: True'"
  chk "crawler2 backends answer" "(cd $C2 && env/bin/crawler2-check --wait 10)"
  chk "no leftover role processes" '! pgrep -f "crawler2-(worker|extract|media|discover|storage relay)|fingerprinter (intake|scheduler|encoder|matcher|reconcile) run" >/dev/null'
  chk "no process on this host's system redis is used (project redis port ${CRAWLER2_REDIS__PORT})" "[ ${CRAWLER2_REDIS__PORT} != 6379 ]"
  echo "preflight: $ok passed, $bad failed"; [ "$bad" = 0 ]
}

plan() {
  echo "RUN_DIR=$RUN_DIR  DURATION_S=$DURATION_S DRAIN_S=$DRAIN_S TERM_WAIT_S=$TERM_WAIT_S ENCODER_DEVICE=$ENCODER_DEVICE"
  echo "engines=$CRAWLER2_SEARCH__ENGINES queries=$QUERY_FILE target=$TARGET_ID"
  local r; for r in "${ROLES[@]}"; do IFS='|' read -r n d m c <<<"$r"; printf '%-20s (cd %s; %s%s)\n' "$n" "$d" "${m:+FINGERPRINTER_METRICS_PORT=$m }" "$c"; done
}

start_role() {
  local n=$1 d=$2 m=$3 c=$4
  if [ "$STUB" = 1 ]; then c="bash -c 'trap \"exit 0\" TERM; while :; do sleep 0.5; done'"; fi
  ( cd "$d" && exec env ${m:+FINGERPRINTER_METRICS_PORT=$m} bash -c "exec $c" ) >>"$RUN_DIR/logs/$n.log" 2>&1 &
  PID[$n]=$!; echo "${PID[$n]}" > "$RUN_DIR/pids/$n.pid"; log "started $n pid=${PID[$n]}"
}

stop_phase() {   # SIGTERM every named role, wait up to TERM_WAIT_S, report exit codes; SIGKILL only as last resort
  local names=("$@") n deadline=$((SECONDS+TERM_WAIT_S))
  for n in "${names[@]}"; do [ -n "${PID[$n]:-}" ] && kill -TERM "${PID[$n]}" 2>/dev/null; done
  for n in "${names[@]}"; do
    [ -z "${PID[$n]:-}" ] && continue
    while kill -0 "${PID[$n]}" 2>/dev/null && [ $SECONDS -lt $deadline ]; do sleep 0.5; done
    if kill -0 "${PID[$n]}" 2>/dev/null; then alert "$n did not exit within ${TERM_WAIT_S}s; SIGKILL (claims/leases recover by expiry)"; kill -KILL "${PID[$n]}" 2>/dev/null; wait "${PID[$n]}" 2>/dev/null
    else wait "${PID[$n]}" 2>/dev/null; log "stopped $n rc=$?"; fi
    unset "PID[$n]"; rm -f "$RUN_DIR/pids/$n.pid"
  done
}

SHUTTING=0
shutdown() {
  [ "$SHUTTING" = 1 ] && return; SHUTTING=1; touch "$RUN_DIR/SHUTTING"
  log "shutdown begins (phase order follows data flow; see validation.md)"
  stop_phase search worker-browser worker-http                        # 1 producers: no new queries/fetches; in-flight claims are returned
  if [ "$DRAIN_S" -gt 0 ]; then log "drain ${DRAIN_S}s: producers stopped, consumers running"; sleep "$DRAIN_S"; fi
  stop_phase extract admit                                            # 2 page consumers
  stop_phase relay resolve probe hash representations                 # 3 outbox relay + media chain (writes encode.requested)
  stop_phase fp-intake fp-scheduler fp-encoder                        # 4 fingerprinter intake -> scheduling -> encode (attempts are released)
  stop_phase fp-match fp-match-reconcile fp-reconcile                 # 5 matcher, then the reconciler/publisher last (flushes held/pending intents)
  stop_phase feedback evidence                                        # 6 match.found consumers
  [ -n "${MON_PID:-}" ] && kill "$MON_PID" 2>/dev/null
  [ "$STUB" = 1 ] || snapshot final
  local orphans; orphans=$(pgrep -af "crawler2-(worker|extract|media|discover|storage relay|intel|evidence)|fingerprinter (intake|scheduler|encoder|matcher|reconcile) run|chrom" | grep -v pgrep || true)
  if [ -n "$orphans" ]; then alert "possible orphan processes:"; echo "$orphans" >> "$RUN_DIR/alerts.log"; else log "no orphan processes"; fi
  log "shutdown complete; elapsed ${SECONDS}s since supervisor start"; touch "$RUN_DIR/SHUTDOWN_COMPLETE"
}

cpu_c() { local m=0 z t; for z in /sys/class/thermal/thermal_zone*; do grep -q x86_pkg "$z/type" 2>/dev/null && { t=$(( $(cat "$z/temp") / 1000 )); [ "$t" -gt "$m" ] && m=$t; }; done; echo "$m"; }

monitor() {
  local csv=$RUN_DIR/resources.csv hot=0 last_snap=0 t
  echo "utc,elapsed_s,cpu_pkg_c,gpu_c,gpu_util,vram_mib,mem_avail_mib,swap_used_mib,disk_free_gb,load1,dead_roles" > "$csv"
  while :; do
    local cpu gpu gu vr ma su df l1 dead="" n
    cpu=$(cpu_c)
    IFS=',' read -r gu vr gpu < <(nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | tr -d ' ') || true
    ma=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo); su=$(awk '/SwapTotal/{t=$2}/SwapFree/{f=$2}END{print int((t-f)/1024)}' /proc/meminfo)
    df=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc 0-9); l1=$(cut -d' ' -f1 /proc/loadavg)
    if [ ! -e "$RUN_DIR/SHUTTING" ]; then   # the monitor is a subshell: it reads pid files, not the supervisor's arrays
      for f in "$RUN_DIR"/pids/*.pid; do [ -e "$f" ] || continue; kill -0 "$(cat "$f")" 2>/dev/null || dead="$dead$(basename "$f" .pid) "; done
      [ -n "$dead" ] && alert "role(s) not running: $dead"
    fi
    echo "$(date -u +%FT%TZ),$SECONDS,$cpu,${gpu:-},${gu:-},${vr:-},$ma,$su,$df,$l1,${dead% }" >> "$csv"
    [ "$cpu" -ge "$CPU_WARN_C" ] && alert "WARN cpu ${cpu}C >= ${CPU_WARN_C}"
    [ -n "${gpu:-}" ] && [ "$gpu" -ge "$GPU_WARN_C" ] && alert "WARN gpu ${gpu}C >= ${GPU_WARN_C}"
    [ "$ma" -le "$MEM_WARN_MIB" ] && alert "WARN MemAvailable ${ma}MiB <= ${MEM_WARN_MIB}"
    [ "$su" -ge "$SWAP_WARN_MIB" ] && alert "WARN swap ${su}MiB >= ${SWAP_WARN_MIB}"
    # stop conditions (request the ordered shutdown; the main loop acts on the file)
    [ "$df" -lt "$MIN_FREE_GB" ] && { alert "STOP disk free ${df}GB < ${MIN_FREE_GB}GB"; echo disk > "$RUN_DIR/STOP_REQUESTED"; }
    [ "$ma" -le "$MEM_STOP_MIB" ] && { alert "STOP MemAvailable ${ma}MiB <= ${MEM_STOP_MIB}"; echo mem > "$RUN_DIR/STOP_REQUESTED"; }
    [ -n "${gpu:-}" ] && [ "$gpu" -ge "$GPU_STOP_C" ] && { alert "STOP gpu ${gpu}C >= ${GPU_STOP_C}"; echo gpu > "$RUN_DIR/STOP_REQUESTED"; }
    if [ "$cpu" -ge "$CPU_STOP_C" ]; then hot=$((hot+1)); else hot=0; fi          # 5 consecutive samples (~2.5 min)
    [ "$hot" -ge 5 ] && { alert "STOP cpu >= ${CPU_STOP_C}C for 5 samples"; echo cpu > "$RUN_DIR/STOP_REQUESTED"; }
    if [ "$STUB" != 1 ] && [ $((SECONDS-last_snap)) -ge "$SNAPSHOT_S" ]; then last_snap=$SECONDS; snapshot "t${SECONDS}"; fi
    sleep "$SAMPLE_S"
  done
}

snapshot() {   # application/infrastructure state; raw files, analysed after the run
  local d=$RUN_DIR/snapshots/$1; mkdir -p "$d"
  docker stats --no-stream --format '{{.Name}},{{.MemUsage}},{{.CPUPerc}}' > "$d/containers.csv" 2>&1
  docker inspect -f '{{.Name}} restarts={{.RestartCount}} oom={{.State.OOMKilled}} health={{if .State.Health}}{{.State.Health.Status}}{{end}}' crawler2-redis-1 crawler2-scylla-1 crawler2-minio-1 fingerprinter-index-qdrant-1 > "$d/restarts.txt" 2>&1
  "${REDIS_CLI[@]}" info memory | grep -E 'used_memory_human|maxmemory_human' > "$d/redis_mem.txt" 2>&1
  "${REDIS_CLI[@]}" info stats | grep -E 'instantaneous_ops_per_sec|total_commands_processed' >> "$d/redis_mem.txt" 2>&1
  local s; for s in urls.discovered.v1 fetch.completed.v1 page.observed.v1 encode.requested.v1 representation.ready.v1 encode.failed.v1 match.found.v1 target.registered.v1; do
    { echo "== $s"; "${REDIS_CLI[@]}" XLEN "events:$s"; "${REDIS_CLI[@]}" XINFO GROUPS "events:$s"; } >> "$d/streams.txt" 2>&1; done
  curl -s -H "api-key: $FINGERPRINTER_INDEX__API_KEY" http://127.0.0.1:16333/collections > "$d/qdrant_collections.json" 2>&1
  docker stats --no-stream --format '{{.MemUsage}}' fingerprinter-index-qdrant-1 > "$d/qdrant_mem.txt" 2>&1
  { fpy match summary; fpy encode status; fpy publisher status; fpy index status; } > "$d/fingerprinter.json" 2>/dev/null
  local r p; for r in "${ROLES[@]}"; do IFS='|' read -r n _ m _ <<<"$r"; p=${m:-}
    [ -z "$p" ] && { case $n in worker-http) p=9101;; worker-browser) p=9102;; extract) p=9104;; relay) p=9105;; admit) p=9106;; resolve) p=9107;; probe) p=9108;; hash) p=9109;; representations) p=9110;; feedback) p=9112;; evidence) p=9113;; esac; }
    [ -n "$p" ] && curl -s -m 5 "http://127.0.0.1:$p/metrics" > "$d/metrics_$n.prom" 2>/dev/null; done
}

run() {
  mkdir -p "$RUN_DIR/logs" "$RUN_DIR/pids" "$RUN_DIR/snapshots"
  if [ "$STUB" != 1 ]; then preflight | tee "$RUN_DIR/preflight.txt" || { echo "preflight failed; nothing started" >&2; exit 1; }; fi
  plan > "$RUN_DIR/plan.txt"; date -u +%FT%TZ > "$RUN_DIR/supervisor_start"
  trap 'log "signal received"; shutdown; exit 0' INT TERM
  trap 'shutdown' EXIT
  monitor & MON_PID=$!
  local r n d m c
  for r in "${ROLES[@]}"; do IFS='|' read -r n d m c <<<"$r"
    if [ "$n" = search ]; then T0=$SECONDS; date -u +%FT%TZ > "$RUN_DIR/crawl_start"; log "crawl window starts (T0); hard stop at +${DURATION_S}s"; fi
    start_role "$n" "$d" "$m" "$c"; sleep "$([ "$STUB" = 1 ] && echo 0.2 || echo 8)"
  done
  while [ $((SECONDS-T0)) -lt "$DURATION_S" ] && [ ! -e "$RUN_DIR/STOP_REQUESTED" ]; do sleep 2; done
  [ -e "$RUN_DIR/STOP_REQUESTED" ] && log "early stop requested: $(cat "$RUN_DIR/STOP_REQUESTED")" || log "duration reached: $((SECONDS-T0))s"
  date -u +%FT%TZ > "$RUN_DIR/crawl_end"; shutdown
}

case "${1:-}" in
  preflight) preflight ;;
  plan) plan ;;
  run) run ;;
  *) echo "usage: $0 {preflight|plan|run}" >&2; exit 64 ;;
esac
