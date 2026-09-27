#!/usr/bin/env bash
# P0 stack validation: compose config, both profiles healthy, service-name
# connectivity from app containers, per-host filesystem isolation.
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

COMPOSE_PROFILES=two-host "${COMPOSE[@]}" stop app-host-1 app-host-2
step "P0 stack validation passed"
