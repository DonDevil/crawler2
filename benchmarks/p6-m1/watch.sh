#!/usr/bin/env bash
# M1 alert loop (used during run 2): emits one line per event worth acting on: a non-relay process exit, a relay
# exit storm (> 20 in 10 min), Redis memory > 700 MB, or a failing `run.sh check` (DOWN / stale monitor).
cd "$(dirname "$0")/../.." || exit 1
VAR=var/p6-m1
tail -n0 -F "$VAR/restarts.log" 2>/dev/null \
    | grep --line-buffered " exit " | grep --line-buffered -v " exit relay " \
    | sed -u 's/^/EXIT: /' &
last_alert=""
while true; do
    if ! out=$(benchmarks/p6-m1/run.sh check 2>&1); then
        msg=$(echo "$out" | tail -1)
        [[ $msg != "$last_alert" ]] && echo "CHECK: $msg"
        last_alert=$msg
    else
        last_alert=""
    fi
    since=$(date -u -d '10 min ago' +%FT%TZ)
    n=$(awk -v s="$since" '$1 >= s && / exit relay /' "$VAR/restarts.log" | wc -l)
    (( n > 20 )) && echo "RELAY STORM: $n relay exits in the last 10 min"
    mem=$(redis-cli -p "${REDIS_HOST_PORT:-16379}" info memory 2>/dev/null | awk -F: '/^used_memory:/{printf "%d", $2/1048576}')
    if [[ -n $mem ]] && (( mem > 700 )); then
        [[ $mem_alerted != 1 ]] && echo "REDIS MEMORY: ${mem} MB of 768 MB maxmemory (noeviction)"
        mem_alerted=1
    else
        mem_alerted=0
    fi
    sleep 60
done
