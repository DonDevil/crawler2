#!/usr/bin/env bash
# P8 exit-gate benchmark (synthetic dedup) from the host against the compose stack.
# Throwaway keyspace crawler2_p8_bench and a private Redis namespace; needs ffmpeg.
#
#   benchmarks/p8-media/run.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

set -a; . ./.env; set +a
DOCKER=${DOCKER:-docker}
scylla_ip=$(sg docker -c "$DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${COMPOSE_PROJECT_NAME:-crawler2}-scylla-1")

export CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export CRAWLER2_SCYLLA__CONTACT_POINTS=$scylla_ip
export CRAWLER2_HOST_ID=${CRAWLER2_HOST_ID:-p8-bench} CRAWLER2_METRICS__ENABLED=false
export CRAWLER2_LOGGING__LEVEL=WARNING PYTHONPATH=.

exec env/bin/python benchmarks/p8-media/dedup_bench.py "$@"
