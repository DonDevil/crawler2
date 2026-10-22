#!/usr/bin/env bash
# P12 evidence test tier, run from the host against the compose stack (like
# scripts/test-feedback.sh): unit tests and the Scylla/MinIO/Redis-backed integration
# tests (docs/phases/p12-evidence-collector/validation.md). Needs `make up` first.
# Uses its own throwaway keyspace (crawler2_it_p12_host), a test bucket and private
# stream prefixes; never a run's keyspace, bucket or streams. Nothing is fetched.
#
#   scripts/test-evidence.sh                                   # everything
#   P12_CASE_OUT=var/p12-fixture-case scripts/test-evidence.sh  # also write the review package
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
DOCKER=${DOCKER:-docker}
scylla_ip=$(sg docker -c "$DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${COMPOSE_PROJECT_NAME:-crawler2}-scylla-1")

export CRAWLER2_REDIS__HOST=127.0.0.1 CRAWLER2_REDIS__PORT=${REDIS_HOST_PORT:-16379}
export CRAWLER2_SCYLLA__CONTACT_POINTS=$scylla_ip
export CRAWLER2_MINIO__ENDPOINT=127.0.0.1:${MINIO_HOST_PORT:-19000}
export CRAWLER2_MINIO__ACCESS_KEY=$MINIO_ROOT_USER CRAWLER2_MINIO__SECRET_KEY=$MINIO_ROOT_PASSWORD
export CRAWLER2_HOST_ID=${CRAWLER2_HOST_ID:-p12-host} RUN_INTEGRATION_TESTS=1

exec env/bin/pytest -p no:cacheprovider tests/unit/evidence \
    tests/integration/storage/test_evidence.py "$@"
