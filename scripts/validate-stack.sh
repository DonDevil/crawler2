#!/usr/bin/env bash
# Stack validation. P0: compose config, both profiles healthy, service-name
# connectivity from app containers, per-host filesystem isolation.
# P2: schema bootstrap from both hosts at once, storage integration tests on
# every host, concurrent writers converging, crash + Scylla restart recovery.
# Usage: scripts/validate-stack.sh   (requires .env; see .env.example)
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(${COMPOSE:-docker compose})
TIMEOUT=${TIMEOUT:-300}

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

cid() { "${COMPOSE[@]}" ps -q "$1"; }

wait_healthy() {
  local deadline=$((SECONDS + TIMEOUT)) svc id status
  for svc in "$@"; do
    id=$(cid "$svc"); [[ -n "$id" ]] || fail "$svc has no container"
    while :; do
      status=$(docker inspect --format '{{.State.Health.Status}}' "$id")
      [[ "$status" == healthy ]] && { echo "$svc: healthy"; break; }
      (( SECONDS < deadline )) || fail "$svc not healthy after ${TIMEOUT}s (status=$status)"
      sleep 3
    done
  done
}

in_app() { local svc=$1; shift; docker exec "$(cid "$svc")" "$@"; }

step "compose config (single, two-host)"
COMPOSE_PROFILES=single "${COMPOSE[@]}" config -q
COMPOSE_PROFILES=two-host "${COMPOSE[@]}" config -q
echo "config: ok"

step "profile single"
COMPOSE_PROFILES=single "${COMPOSE[@]}" up -d --build
wait_healthy redis scylla minio app
in_app app crawler2-check
in_app app env RUN_INTEGRATION_TESTS=1 python -m pytest -q -p no:cacheprovider tests/integration

step "profile two-host"
COMPOSE_PROFILES=two-host "${COMPOSE[@]}" up -d
wait_healthy app-host-1 app-host-2
for svc in app-host-1 app-host-2; do
  in_app "$svc" crawler2-check
  in_app "$svc" env RUN_INTEGRATION_TESTS=1 python -m pytest -q -p no:cacheprovider tests/integration
done

h1=$(in_app app-host-1 printenv CRAWLER2_HOST_ID)
h2=$(in_app app-host-2 printenv CRAWLER2_HOST_ID)
[[ "$h1" != "$h2" ]] || fail "host ids must differ ($h1)"
echo "host ids: $h1 / $h2"

marker="p0-$RANDOM$RANDOM"
in_app app-host-1 sh -c "echo $marker > /var/lib/crawler2/scratch/marker"
if in_app app-host-2 test -e /var/lib/crawler2/scratch/marker; then
  fail "scratch is shared between simulated hosts"
fi
in_app app-host-1 rm /var/lib/crawler2/scratch/marker
echo "scratch isolation: ok"

if in_app app-host-1 sh -c 'touch /app/should-fail' 2>/dev/null; then
  fail "app root filesystem is writable"
fi
echo "read-only root fs: ok"

step "P2 schema bootstrap: both hosts migrate concurrently (one applies, one waits)"
in_app app-host-1 crawler2-storage migrate & m1=$!
in_app app-host-2 crawler2-storage migrate & m2=$!
wait "$m1" && wait "$m2" || fail "concurrent migrate failed"
in_app app-host-1 crawler2-storage check > /dev/null || fail "schema not current"
echo "schema: current (crawler2 keyspace, raw bucket)"

run="p2$RANDOM$RANDOM"
ks=crawler2_mh
step "P2 concurrent writers (host-1 + host-2, run $run)"
in_app app-host-1 python -m tests.integration.multihost reset --keyspace "$ks"
in_app app-host-1 python -m tests.integration.multihost write --keyspace "$ks" --run "$run" & w1=$!
in_app app-host-2 python -m tests.integration.multihost write --keyspace "$ks" --run "$run" & w2=$!
wait "$w1" && wait "$w2" || fail "concurrent writers failed"
in_app app-host-2 python -m tests.integration.multihost verify --keyspace "$ks" --run "$run"

step "P2 crash between commit and publication, with a Scylla restart"
in_app app-host-1 python -m tests.integration.multihost reset --keyspace "$ks"
in_app app-host-1 python -m tests.integration.multihost crash-produce --keyspace "$ks" --run "$run"
set +e
in_app app-host-1 python -m tests.integration.multihost crash-relay --keyspace "$ks" --run "$run"
code=$?
set -e
[[ $code == 137 ]] || fail "relay should have died with 137 (got $code)"
echo "relay on host-1 killed mid-publication (exit $code)"
"${COMPOSE[@]}" restart scylla
# commitlog replay after a restart can take ~10 min on the dev host's USB HDD
TIMEOUT=${RESTART_TIMEOUT:-900} wait_healthy scylla
in_app app-host-2 crawler2-check --wait 120 > /dev/null
in_app app-host-2 env "CRAWLER2_EVENTS__STREAM_PREFIX=crash-$run:" \
  crawler2-storage relay --once --keyspace "$ks"
in_app app-host-2 python -m tests.integration.multihost crash-verify --keyspace "$ks" --run "$run"

COMPOSE_PROFILES=two-host "${COMPOSE[@]}" stop app-host-1 app-host-2
step "stack validation passed (P0 + P2)"
