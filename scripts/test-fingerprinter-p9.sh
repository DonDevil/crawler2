#!/usr/bin/env bash
# P9 cross-service tier (design §16, P9-D19): crawler2's P8 requester/projection
# against the P9 fingerprinter running as separate processes from its own repo
# (P9_FINGERPRINTER_DIR, default ~/anti_piracy/fingerprinter). Needs `make up`.
# Throwaway keyspaces (crawler2_it_p9_xs, fingerprinter_it_xs), private stream
# prefixes and buckets; never a run's keyspace, streams or buckets.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
DOCKER=${DOCKER:-docker}
scylla_ip=$(sg docker -c "$DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${COMPOSE_PROJECT_NAME:-crawler2}-scylla-1")

export CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export CRAWLER2_SCYLLA__CONTACT_POINTS=$scylla_ip
export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:${MINIO_HOST_PORT:-19000}
export CRAWLER2_MINIO__ACCESS_KEY=$MINIO_ROOT_USER CRAWLER2_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD
export CRAWLER2_HOST_ID=${CRAWLER2_HOST_ID:-p9-xs} RUN_INTEGRATION_TESTS=1
export CRAWLER2_METRICS__ENABLED=false

exec ${PYTEST:-env/bin/pytest} -p no:cacheprovider tests/integration/fingerprinter "$@"
