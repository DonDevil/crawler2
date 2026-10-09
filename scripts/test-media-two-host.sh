#!/usr/bin/env bash
# P8 on the two-host compose profile (plan B.4 #8, P8 design §10.1 C-11 and §10).
# Rebuilds the app image (new code in app-host-1/app-host-2), then runs the drivers
# of tests/integration/media_multihost.py in both containers against the shared
# Scylla/Redis. Throwaway keyspace crawler2_p8_mh; never a run's keyspace.
#
#   scripts/test-media-two-host.sh
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKER=${DOCKER:-docker}
ks=crawler2_p8_mh
run="p8$RANDOM$RANDOM"

dc() { sg docker -c "COMPOSE_PROFILES=two-host $DOCKER compose $*"; }
in_app() { local c=$1; shift; sg docker -c "$DOCKER compose exec -T $c $(printf '%q ' "$@")"; }
step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

step "two-host profile: rebuild and start app-host-1, app-host-2"
dc up -d --build app-host-1 app-host-2
in_app app-host-1 crawler2-check --wait 180 > /dev/null
in_app app-host-2 crawler2-check --wait 180 > /dev/null

step "C-11: resolver lease handover between hosts (run $run)"
in_app app-host-1 python -m tests.integration.media_multihost resolve-setup --keyspace "$ks" --run "$run"
set +e
in_app app-host-1 python -m tests.integration.media_multihost resolve --keyspace "$ks" --run "$run" --die-after 20 &
h1=$!
sleep 3  # host-1 takes the lease first; host-2 then stands by
in_app app-host-2 python -m tests.integration.media_multihost resolve --keyspace "$ks" --run "$run" &
h2=$!
wait "$h1"; c1=$?
wait "$h2"; c2=$?
set -e
[[ $c1 == 137 ]] || fail "host-1 should have died mid-bucket with 137 (got $c1)"
[[ $c2 == 0 ]] || fail "host-2 resolver failed ($c2)"
in_app app-host-2 python -m tests.integration.media_multihost resolve-verify --keyspace "$ks" --run "$run"

step "P8 two-host checks passed"
