#!/usr/bin/env bash
# P13 analytics test tier, run from the host against the compose stack (like
# scripts/test-evidence.sh): the unit tests (synthetic fixtures, no store) and the
# Scylla-backed integration test (docs/phases/p13-analytics/validation.md). Needs `make up`.
# Uses its own throwaway keyspace (crawler2_it_p13_host), a test bucket and private
# stream prefixes; never a run's keyspace. Nothing is fetched; exports go to pytest tmp dirs.
#
#   scripts/test-analytics.sh            # everything
#   scripts/test-analytics.sh -k export  # a subset
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
DOCKER=${DOCKER:-docker}
scylla_ip=$(sg docker -c "$DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${COMPOSE_PROJECT_NAME:-crawler2}-scylla-1")

export CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export CRAWLER2_SCYLLA__CONTACT_POINTS=$scylla_ip
export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:${MINIO_HOST_PORT:-19000}
export CRAWLER2_MINIO__ACCESS_KEY=$MINIO_ROOT_USER CRAWLER2_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD
export CRAWLER2_HOST_ID=${CRAWLER2_HOST_ID:-p13-host} RUN_INTEGRATION_TESTS=1

exec env/bin/pytest -p no:cacheprovider tests/unit/analytics \
    tests/integration/storage/test_analytics.py "$@"
