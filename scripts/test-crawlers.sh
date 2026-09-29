#!/usr/bin/env bash
# P4 fetch-layer test tier, run from the host against the compose stack.
# Browser workers run on the host (Chromium is not in the app image); the
# host reaches Scylla at its container IP. Needs `make up` first.
#
#   scripts/test-crawlers.sh              # contract + integration (+ browser)
#   scripts/test-crawlers.sh -k leak -s   # extra pytest args
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
DOCKER=${DOCKER:-docker}
scylla_ip=$(sg docker -c "$DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${COMPOSE_PROJECT_NAME:-crawler2}-scylla-1")

export CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export CRAWLER2_SCYLLA__CONTACT_POINTS=$scylla_ip
export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:${MINIO_HOST_PORT:-19000}
export CRAWLER2_MINIO__ACCESS_KEY=$MINIO_ROOT_USER CRAWLER2_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD
export RUN_INTEGRATION_TESTS=1 RUN_BROWSER_TESTS=${RUN_BROWSER_TESTS:-1}

exec env/bin/pytest -p no:cacheprovider tests/contract/crawlers tests/integration/crawlers "$@"
