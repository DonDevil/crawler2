#!/usr/bin/env bash
# Create the least-privilege MinIO service user (P14, TC-065). The application no longer needs the
# root credentials: it may read, write and list the named buckets and nothing else - no delete, no
# bucket creation, no admin API. Buckets are created here (as root) once.
#
#   MINIO_ROOT_USER=... MINIO_ROOT_PASSWORD=... \
#   deploy/secure/provision-minio.sh http://127.0.0.1:19000 crawler2-raw [more-buckets...]
#
# Credentials of the service user are read from $SECRETS_DIR (default ./secrets; see
# provision-secrets.sh). Dev (compose) uses the `mc` shipped in the MinIO container; on a real
# host install `mc` and set MC=mc.
set -euo pipefail
cd "$(dirname "$0")/../.."
ENDPOINT=${1:?endpoint url}; shift
[ "$#" -ge 1 ] || { echo "usage: $0 ENDPOINT BUCKET..." >&2; exit 64; }
SECRETS_DIR=${SECRETS_DIR:-secrets}
USER_NAME=$(cat "$SECRETS_DIR/minio_app_access_key")
USER_SECRET=$(cat "$SECRETS_DIR/minio_app_secret")
POLICY_NAME=${POLICY_NAME:-crawler2-app}
MC=${MC:-docker exec -e MC_CONFIG_DIR=/tmp/mc -i ${COMPOSE_PROJECT_NAME:-crawler2}-minio-1 mc}
INNER_ENDPOINT=${INNER_ENDPOINT:-http://127.0.0.1:9000}   # as seen from where mc runs

resources=""
for b in "$@"; do resources="$resources\"arn:aws:s3:::$b\",\"arn:aws:s3:::$b/*\","; done
policy=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation"],
  "Resource":[$(for b in "$@"; do printf '"arn:aws:s3:::%s",' "$b"; done | sed 's/,$//')]},
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],
  "Resource":[$(for b in "$@"; do printf '"arn:aws:s3:::%s/*",' "$b"; done | sed 's/,$//')]}
]}
JSON
)
$MC alias set local "$INNER_ENDPOINT" "${MINIO_ROOT_USER:?}" "${MINIO_ROOT_PASSWORD:?}" >/dev/null
for b in "$@"; do $MC mb --ignore-existing "local/$b" >/dev/null; done
echo "$policy" | $MC pipe /tmp/crawler2-policy.json >/dev/null 2>&1 || true
# `mc admin policy create` reads a file inside the container running mc
if [[ "$MC" == docker* ]]; then
  echo "$policy" | docker exec -i "${COMPOSE_PROJECT_NAME:-crawler2}-minio-1" sh -c 'cat > /tmp/crawler2-policy.json'
else
  echo "$policy" > /tmp/crawler2-policy.json
fi
$MC admin policy create local "$POLICY_NAME" /tmp/crawler2-policy.json >/dev/null
$MC admin user add local "$USER_NAME" "$USER_SECRET" >/dev/null
$MC admin policy attach local "$POLICY_NAME" --user "$USER_NAME" >/dev/null
echo "service user '$USER_NAME' limited to: $* (get/put/list only)" >&2
