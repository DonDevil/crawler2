#!/usr/bin/env bash
# P8 media-registry test tier, run from the host against the compose stack (like
# scripts/test-intelligence.sh): unit tests and the real Scylla/Redis resolver,
# probe/hash and encode suites (docs/phases/p08-media-registry/design.md §10).
# Needs `make up` first. Uses its own throwaway keyspace (crawler2_it_p8_host) and
# private Redis key/stream prefixes; never a run's keyspace or Redis keys.
#
#   scripts/test-media.sh            # everything
#   scripts/test-media.sh -k c6      # extra pytest args
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
DOCKER=${DOCKER:-docker}
scylla_ip=$(sg docker -c "$DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${COMPOSE_PROJECT_NAME:-crawler2}-scylla-1")

export CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export CRAWLER2_SCYLLA__CONTACT_POINTS=$scylla_ip
export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:${MINIO_HOST_PORT:-19000}
export CRAWLER2_MINIO__ACCESS_KEY=$MINIO_ROOT_USER CRAWLER2_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD
export CRAWLER2_HOST_ID=${CRAWLER2_HOST_ID:-p8-host} RUN_INTEGRATION_TESTS=1
export CRAWLER2_METRICS__ENABLED=false

exec ${PYTEST:-env/bin/pytest} -p no:cacheprovider tests/unit/media tests/integration/storage/test_media_*.py "$@"
