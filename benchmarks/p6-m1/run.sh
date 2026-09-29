#!/usr/bin/env bash
# P6 Milestone M1: the closed crawl loop on the live seed set (design §21, Gate F/G).
#
#   benchmarks/p6-m1/run.sh setup           # keyspace crawler2_m1 (V001-V003), bucket, rules
#   benchmarks/p6-m1/run.sh start [QUERIES] # all processes, supervised; QUERIES = operator query file
#   benchmarks/p6-m1/run.sh status          # processes, restarts
#   benchmarks/p6-m1/run.sh stop
#
# Everything M1 writes is its own dataset: Scylla keyspace crawler2_m1, Redis
# namespace m1 and stream prefix m1:events:, MinIO bucket crawler2-m1. Runtime
# state and logs live in git-ignored var/p6-m1/. Processes run on the host
# (browsers live on the host; Scylla is reached at its container IP).
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$PWD
VAR=$ROOT/var/p6-m1
mkdir -p "$VAR/logs" "$VAR/pids"

set -a; . ./.env; set +a
scylla_ip=$(sg docker -c "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${COMPOSE_PROJECT_NAME:-crawler2}-scylla-1")
export CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export CRAWLER2_REDIS__NAMESPACE=m1 CRAWLER2_EVENTS__STREAM_PREFIX=m1:events:
export CRAWLER2_SCYLLA__CONTACT_POINTS=$scylla_ip CRAWLER2_SCYLLA__KEYSPACE=crawler2_m1
export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:${MINIO_HOST_PORT:-19000} CRAWLER2_MINIO__BUCKET_RAW=crawler2-m1
export CRAWLER2_MINIO__ACCESS_KEY=$MINIO_ROOT_USER CRAWLER2_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD
export CRAWLER2_HOST_ID=m1-host CRAWLER2_SCRATCH_DIR=$VAR/scratch
export CRAWLER2_LOGGING__LEVEL=INFO
# M1 worker configuration (recorded in the results): P4 defaults except these.
# http 4: the rate the dev host's HDD-backed Scylla can extract and admit (16 and 8
# outran extraction; see validation.md §4.1).
export CRAWLER2_WORKERS__HTTP__CONCURRENCY=${M1_HTTP_CONCURRENCY:-4}
export CRAWLER2_WORKERS__BROWSER__CONCURRENCY=${M1_BROWSER_CONCURRENCY:-2}
export CRAWLER2_LIMITS__MAX_MEMORY_MB=${M1_MAX_MEMORY_MB:-1024}
# Stream retention: urls.discovered entries are ~17 KB; 100k entries would exceed the
# compose Redis maxmemory (768 MB, noeviction). Consumer lag stays far below 10k.
export CRAWLER2_EVENTS__STREAM_MAXLEN=${M1_STREAM_MAXLEN:-10000}

SEEDS=$ROOT/benchmarks/v1-baseline/seeds.txt
BIN=$ROOT/env/bin

supervise() {  # name, command...: restart on exit, one log line per (re)start
    local name=$1; shift
    (
        trap 'kill "$child" 2>/dev/null; exit 0' TERM INT
        while true; do
            echo "$(date -u +%FT%TZ) start $name" >> "$VAR/restarts.log"
            "$@" >> "$VAR/logs/$name.log" 2>&1 &
            child=$!
            echo "$child" > "$VAR/pids/$name.child"
            if wait "$child"; then code=0; else code=$?; fi
            echo "$(date -u +%FT%TZ) exit $name code=$code" >> "$VAR/restarts.log"
            sleep 5
        done
    ) &
    echo $! > "$VAR/pids/$name.pid"
}

case ${1:-} in
setup)
    "$BIN/crawler2-storage" migrate
    "$BIN/crawler2-filter" --by m1-setup import-builtin
    "$BIN/crawler2-filter" --by m1-setup import-v1 "$ROOT/tests/fixtures/filtering/v1_domain_blacklist.txt"
    "$BIN/crawler2-filter" --by m1-setup import-abp --source easylist --url https://easylist.to/easylist/easylist.txt
    "$BIN/crawler2-filter" --by m1-setup import-abp --source easyprivacy --url https://easylist.to/easylist/easyprivacy.txt
    id=$("$BIN/crawler2-filter" --by m1-setup publish --source built_in@latest --source v1_blacklist@latest \
        --source easylist@latest --source easyprivacy@latest --note "M1 ruleset" | "$BIN/python" -c 'import json,sys; print(json.load(sys.stdin)["ruleset"])')
    "$BIN/crawler2-filter" --by m1-setup activate "$id"
    "$BIN/crawler2-filter" status > "$VAR/ruleset-status.json"
    ;;
start)
    queries=${2:-}
    date -u +%FT%TZ > "$VAR/started_at"
    env | grep '^CRAWLER2_' | grep -v -i 'secret\|access_key\|password' | sort > "$VAR/config.env"
    supervise relay env CRAWLER2_METRICS__PORT=9301 "$BIN/crawler2-storage" relay
    supervise extract env CRAWLER2_METRICS__ENABLED=false "$BIN/crawler2-extract"
    supervise extract2 env CRAWLER2_METRICS__ENABLED=false "$BIN/crawler2-extract"
    supervise admit env CRAWLER2_METRICS__PORT=9302 "$BIN/crawler2-discover" admit
    supervise http env CRAWLER2_METRICS__ENABLED=false "$BIN/crawler2-worker" --pool http
    supervise browser env CRAWLER2_METRICS__ENABLED=false "$BIN/crawler2-worker" --pool browser
    supervise seeds env CRAWLER2_METRICS__ENABLED=false "$BIN/crawler2-discover" seeds "$SEEDS" \
        --source w52-v1-seeds --meta file=benchmarks/v1-baseline/seeds.txt --meta phase=p6-m1 --every 21600
    if [[ -n $queries ]]; then
        cp "$queries" "$VAR/queries.txt"
        supervise search env CRAWLER2_METRICS__ENABLED=false "$BIN/crawler2-discover" search \
            --queries "$VAR/queries.txt" --every 86400
    fi
    supervise monitor "$BIN/python" "$ROOT/benchmarks/p6-m1/monitor.py" --interval 60
    echo "M1 started; state in $VAR"
    ;;
start-search)  # add the search process to a running M1: start-search QUERIES
    cp "${2:?query file}" "$VAR/queries.txt"
    echo "$(date -u +%FT%TZ) add search queries=$(sha256sum "$VAR/queries.txt" | cut -c1-16)" >> "$VAR/restarts.log"
    supervise search env CRAWLER2_METRICS__ENABLED=false "$BIN/crawler2-discover" search \
        --queries "$VAR/queries.txt" --every 86400
    ;;
add-extract)  # another page.observed consumer in the same group: add-extract NAME
    name=${2:?name, e.g. extract2}
    echo "$(date -u +%FT%TZ) add $name" >> "$VAR/restarts.log"
    supervise "$name" env CRAWLER2_METRICS__ENABLED=false "$BIN/crawler2-extract"
    ;;
restart-relay)  # apply a new M1_STREAM_MAXLEN (the relay trims streams on publish)
    kill "$(cat "$VAR/pids/relay.pid")" 2>/dev/null || true
    kill "$(cat "$VAR/pids/relay.child")" 2>/dev/null || true
    echo "$(date -u +%FT%TZ) reconfigure relay stream_maxlen=$CRAWLER2_EVENTS__STREAM_MAXLEN" >> "$VAR/restarts.log"
    supervise relay env CRAWLER2_METRICS__PORT=9301 "$BIN/crawler2-storage" relay
    env | grep '^CRAWLER2_' | grep -v -i 'secret\|access_key\|password' | sort > "$VAR/config.env"
    ;;
restart-http)  # apply a new M1_HTTP_CONCURRENCY to the http pool only
    kill "$(cat "$VAR/pids/http.pid")" 2>/dev/null || true
    kill "$(cat "$VAR/pids/http.child")" 2>/dev/null || true
    echo "$(date -u +%FT%TZ) reconfigure http concurrency=$CRAWLER2_WORKERS__HTTP__CONCURRENCY" >> "$VAR/restarts.log"
    supervise http env CRAWLER2_METRICS__ENABLED=false "$BIN/crawler2-worker" --pool http
    env | grep '^CRAWLER2_' | grep -v -i 'secret\|access_key\|password' | sort > "$VAR/config.env"
    ;;
status)
    for pid in "$VAR"/pids/*.pid; do
        name=$(basename "$pid" .pid)
        if kill -0 "$(cat "$pid")" 2>/dev/null; then echo "$name up"; else echo "$name DOWN"; fi
    done
    echo "restarts: $(grep -c ' start ' "$VAR/restarts.log" 2>/dev/null || echo 0) starts"
    ;;
stop)
    for pid in "$VAR"/pids/*.pid; do kill "$(cat "$pid")" 2>/dev/null || true; done
    for child in "$VAR"/pids/*.child; do kill "$(cat "$child")" 2>/dev/null || true; done
    ;;
*)
    sed -n '2,12p' "$0"; exit 2 ;;
esac
