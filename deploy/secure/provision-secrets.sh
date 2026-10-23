#!/usr/bin/env bash
# Generate strong random credentials for the data services and render the Redis ACL.
# Writes ONLY under $SECRETS_DIR (default ./secrets, mode 0700; files 0600). Never prints a secret.
# The directory must be git-ignored (it is) and must not be copied into images or logs.
#
#   deploy/secure/provision-secrets.sh            # creates missing secrets, keeps existing ones
#   deploy/secure/provision-secrets.sh --rotate   # replaces them (restart consumers afterwards)
set -euo pipefail
cd "$(dirname "$0")/../.."
SECRETS_DIR=${SECRETS_DIR:-secrets}
rotate=0; [ "${1:-}" = "--rotate" ] && rotate=1
umask 077
mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"

gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
ensure() { # name
  local f="$SECRETS_DIR/$1"
  if [ "$rotate" = 1 ] || [ ! -s "$f" ]; then gen > "$f"; chmod 600 "$f"; echo "generated $1" >&2; fi
}
for s in redis_app_password redis_monitor_password redis_operator_password \
         scylla_app_password scylla_admin_password minio_app_secret minio_root_password; do ensure "$s"; done
[ -s "$SECRETS_DIR/minio_app_access_key" ] || { echo "crawler2-svc" > "$SECRETS_DIR/minio_app_access_key"; }

# Render the ACL file (the ACL file format has no comment syntax: strip the template's)
grep -v '^[[:space:]]*#' deploy/secure/redis.acl.template | sed -e "s|@REDIS_APP_PASSWORD@|$(cat "$SECRETS_DIR/redis_app_password")|" \
    -e "s|@REDIS_MONITOR_PASSWORD@|$(cat "$SECRETS_DIR/redis_monitor_password")|" \
    -e "s|@REDIS_OPERATOR_PASSWORD@|$(cat "$SECRETS_DIR/redis_operator_password")|" \
    > "$SECRETS_DIR/redis.acl"
chmod 600 "$SECRETS_DIR/redis.acl"

# Environment file for the application (CRAWLER2_*), consumed via EnvironmentFile= / --env-file
{
  echo "CRAWLER2_REDIS__USERNAME=crawler2"
  echo "CRAWLER2_REDIS__PASSWORD=$(cat "$SECRETS_DIR/redis_app_password")"
  echo "CRAWLER2_SCYLLA__USERNAME=crawler2_app"
  echo "CRAWLER2_SCYLLA__PASSWORD=$(cat "$SECRETS_DIR/scylla_app_password")"
  echo "CRAWLER2_MINIO__ACCESS_KEY=$(cat "$SECRETS_DIR/minio_app_access_key")"
  echo "CRAWLER2_MINIO__SECRET_KEY=$(cat "$SECRETS_DIR/minio_app_secret")"
} > "$SECRETS_DIR/crawler2.secrets.env"
chmod 600 "$SECRETS_DIR/crawler2.secrets.env"
echo "secrets ready in $SECRETS_DIR (not printed)" >&2
