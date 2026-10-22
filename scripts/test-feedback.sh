#!/usr/bin/env bash
# P11 match-feedback test tier, run from the host against the compose stack (like
# scripts/test-intelligence.sh): unit tests and the Scylla/Redis-backed M4 replay
# (docs/phases/p11-feedback-loop/validation.md). Needs `make up` first. Uses its own
# throwaway keyspace (crawler2_it_p11_host), private stream prefixes and frontier
# namespaces (Redis db 9); never a run's keyspace or Redis keys.
#
#   scripts/test-feedback.sh            # everything
#   scripts/test-feedback.sh -k replay  # extra pytest args
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
DOCKER=${DOCKER:-docker}
scylla_ip=$(sg docker -c "$DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${COMPOSE_PROJECT_NAME:-crawler2}-scylla-1")

export CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export CRAWLER2_SCYLLA__CONTACT_POINTS=$scylla_ip
export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:${MINIO_HOST_PORT:-19000}
export CRAWLER2_MINIO__ACCESS_KEY=$MINIO_ROOT_USER CRAWLER2_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD
export CRAWLER2_HOST_ID=${CRAWLER2_HOST_ID:-p11-host} RUN_INTEGRATION_TESTS=1

exec env/bin/pytest -p no:cacheprovider tests/unit/intelligence/test_feedback.py \
    tests/integration/storage/test_feedback_loop.py "$@"
