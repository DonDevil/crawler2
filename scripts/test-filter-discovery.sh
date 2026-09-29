#!/usr/bin/env bash
# P6 filter + discovery test tier, run from the host against the compose stack
# (like scripts/test-extraction.sh): unit tests, the Scylla-backed rule store and
# discovery rows, the M1 fixture loop and the browser interception test.
# Needs `make up` first; Chromium for the browser test (RUN_BROWSER_TESTS=0 skips it).
#
#   scripts/test-filter-discovery.sh            # everything
#   scripts/test-filter-discovery.sh -k m1      # extra pytest args
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
DOCKER=${DOCKER:-docker}
scylla_ip=$(sg docker -c "$DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${COMPOSE_PROJECT_NAME:-crawler2}-scylla-1")

export CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export CRAWLER2_SCYLLA__CONTACT_POINTS=$scylla_ip
export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:${MINIO_HOST_PORT:-19000}
export CRAWLER2_MINIO__ACCESS_KEY=$MINIO_ROOT_USER CRAWLER2_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD
# Own throwaway keyspace/bucket (crawler2_it_p6_host), never an app container's.
export CRAWLER2_HOST_ID=${CRAWLER2_HOST_ID:-p6-host} RUN_INTEGRATION_TESTS=1
export RUN_BROWSER_TESTS=${RUN_BROWSER_TESTS:-1}

exec env/bin/pytest -p no:cacheprovider tests/unit/filtering tests/unit/discovery \
    tests/integration/storage/test_filter_discovery.py tests/integration/storage/test_m1_loop.py \
    tests/integration/crawlers/test_interception.py "$@"
