#!/usr/bin/env bash
#
# Answers one question: WHY is MySQL rejecting the password?
#
# Run this ON the EC2 instance (Session Manager), as root:
#   sudo /opt/app/diagnose-db-auth.sh
# or from the repo copy, after uploading it.
#
# Two very different faults produce an identical 1045, and guessing between them
# is what makes this loop:
#
#   A. Parameter Store and the RDS master password genuinely differ.
#      -> fix by resetting RDS to the Parameter Store value.
#
#   B. They agree, but the password is corrupted between Parameter Store and the
#      container. Docker Compose reads .env.runtime as a dotenv file: "$" starts
#      an interpolation and "#" can begin a comment, so a password containing
#      either arrives at MySQL as a DIFFERENT string. Resetting RDS cannot fix
#      this - the value that reaches the driver is wrong no matter what RDS holds.
#
# The distinguishing test is to fingerprint the password at each hop and see
# where it changes. Only lengths and truncated SHA-256 digests are printed, never
# the password itself.

set -uo pipefail

APP_DIR=/opt/app
# shellcheck disable=SC1091
source "${APP_DIR}/app.env"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[1;31mBAD\033[0m  %s\n' "$*"; }
info() { printf '       %s\n' "$*"; }

fingerprint() { printf '%s' "$1" | sha256sum | cut -c1-16; }

echo
bold "1. Parameter Store"
SSM_PW="$(aws ssm get-parameter --region "$AWS_REGION" \
  --name "/${PROJECT_NAME}/DB_PASSWORD" --with-decryption \
  --query Parameter.Value --output text 2>/dev/null || echo "")"

if [ -z "$SSM_PW" ]; then
  bad "/${PROJECT_NAME}/DB_PASSWORD is missing or unreadable."
  exit 1
fi
SSM_LEN=${#SSM_PW}
SSM_FP="$(fingerprint "$SSM_PW")"
ok "length ${SSM_LEN}, sha256 ${SSM_FP}"

# Characters that survive RDS but not a dotenv round trip.
RISKY=""
case "$SSM_PW" in *'$'*) RISKY="${RISKY} \$" ;; esac
case "$SSM_PW" in *'#'*) RISKY="${RISKY} #" ;; esac
case "$SSM_PW" in *' '*) RISKY="${RISKY} space" ;; esac
case "$SSM_PW" in *'"'*) RISKY="${RISKY} doublequote" ;; esac
case "$SSM_PW" in *"'"*) RISKY="${RISKY} singlequote" ;; esac
case "$SSM_PW" in *'\'*) RISKY="${RISKY} backslash" ;; esac

if [ -n "$RISKY" ]; then
  bad "contains characters Compose will mangle:${RISKY}"
  info "This alone is enough to cause 1045 even with RDS set correctly."
else
  ok "no Compose-hostile characters"
fi

echo
bold "2. What the container actually receives"
CONTAINER_OUT="$(docker compose \
  --env-file "${APP_DIR}/.env.runtime" \
  -f "${APP_DIR}/docker-compose.prod.yml" \
  run --rm --no-deps api python -c \
  "import os,hashlib; p=os.environ.get('DB_PASSWORD',''); print(len(p), hashlib.sha256(p.encode()).hexdigest()[:16])" \
  2>/dev/null | tr -d '\r' | tail -n 1)"

CON_LEN="$(echo "$CONTAINER_OUT" | awk '{print $1}')"
CON_FP="$(echo "$CONTAINER_OUT" | awk '{print $2}')"

if [ -z "$CON_FP" ]; then
  bad "could not read DB_PASSWORD inside the container"
  info "raw output: ${CONTAINER_OUT:-<empty>}"
  exit 1
fi
info "length ${CON_LEN}, sha256 ${CON_FP}"

MANGLED=false
if [ "$CON_FP" = "$SSM_FP" ]; then
  ok "matches Parameter Store exactly - the value is not being corrupted"
else
  MANGLED=true
  bad "DIFFERS from Parameter Store (${SSM_LEN} -> ${CON_LEN} chars)"
fi

echo
bold "3. Verdict"
if [ "$MANGLED" = true ]; then
  cat <<'MSG'
  Cause B: the password is corrupted on its way into the container.

  Docker Compose is rewriting it while reading .env.runtime. Resetting the RDS
  password will NOT help - the driver never sends the stored value.

  Fix: rotate to an alphanumeric password, which cannot be mangled.
    From your machine:
      PROJECT_NAME=<project> AWS_REGION=<region> \
        ./infra/free-tier/scripts/resync-db-password.sh --rotate
MSG
  exit 2
fi

cat <<'MSG'
  Cause A: Parameter Store and the container agree, so the value reaching MySQL
  is the stored one. MySQL still rejects it, which means the RDS master password
  is something else.

  Fix: reset RDS to the Parameter Store value. From your machine (the instance
  role has no rds:ModifyDBInstance, deliberately):
      PROJECT_NAME=<project> AWS_REGION=<region> \
        ./infra/free-tier/scripts/resync-db-password.sh

  If you have already run that and this persists, check whether a CloudFormation
  stack update ran afterwards and reset the password back:
      aws rds describe-db-instances --db-instance-identifier <project>-mysql \
        --query 'DBInstances[0].[DBInstanceStatus,PendingModifiedValues]'
MSG
exit 3
