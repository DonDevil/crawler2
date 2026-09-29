#!/usr/bin/env bash
# P5 extraction test tier, run from the host against the compose stack (like
# scripts/test-crawlers.sh): unit tests plus the Scylla/MinIO/Redis storage
# suite, which includes the page-intelligence loop. Needs `make up` first.
#
#   scripts/test-extraction.sh            # unit + storage integration
#   scripts/test-extraction.sh -k loop    # extra pytest args
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
DOCKER=${DOCKER:-docker}
scylla_ip=$(sg docker -c "$DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${COMPOSE_PROJECT_NAME:-crawler2}-scylla-1")

export CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export CRAWLER2_SCYLLA__CONTACT_POINTS=$scylla_ip
export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:${MINIO_HOST_PORT:-19000}
export CRAWLER2_MINIO__ACCESS_KEY=$MINIO_ROOT_USER CRAWLER2_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD
# Own throwaway keyspace/bucket (crawler2_it_p5_host), never an app container's.
export CRAWLER2_HOST_ID=${CRAWLER2_HOST_ID:-p5-host} RUN_INTEGRATION_TESTS=1

exec env/bin/pytest -p no:cacheprovider tests/unit/extraction tests/integration/storage "$@"
