#!/usr/bin/env bash
#
# Resyncs the database password across every place it is stored, in one pass,
# then redeploys and verifies.
#
#   ./infra/free-tier/scripts/resync-db-password.sh            # reuse the SSM value
#   ./infra/free-tier/scripts/resync-db-password.sh --rotate   # generate a new one
#
# Run this from your MACHINE, not the instance. The EC2 instance role carries
# SSM, ECR, S3 and KMS but deliberately not rds:ModifyDBInstance - the host has
# no business being able to change the database's master password.
#
# Why this exists: the password lives in three places, and a mismatch in any of
# them presents identically as MySQL 1045 "Access denied", with nothing saying
# which one is stale:
#
#   1. /<project>/DB_PASSWORD in Parameter Store  - the source of truth
#   2. the RDS master password                    - set from (1) at stack deploy
#   3. /opt/app/.env.runtime on the instance      - written from (1) at deploy
#
# Resetting only (2) is the usual half-fix: (3) still holds the older value, so
# the API keeps failing and it looks like the reset did not work.

set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-skill-upgradation-free}"
AWS_REGION="${AWS_REGION:-us-east-1}"
ROTATE=false
[ "${1:-}" = "--rotate" ] && ROTATE=true

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

command -v aws >/dev/null || die "AWS CLI not found on PATH."
aws sts get-caller-identity >/dev/null || die "AWS credentials are not configured."

DB_ID="${PROJECT_NAME}-mysql"
PARAM="/${PROJECT_NAME}/DB_PASSWORD"

# Tries each generator in turn rather than assuming one. On Windows, `python3`
# is often the Microsoft Store stub that prints an install message and exits
# non-zero, and `python` may not be on PATH outside an activated venv - so a
# hard-coded interpreter turns this script into a coin flip. openssl is the
# backstop; Git Bash ships it.
generate_password() {
  local out
  for py in python python3 py; do
    command -v "$py" >/dev/null 2>&1 || continue
    out="$("$py" -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(32)))" 2>/dev/null || true)"
    # 32 alphanumerics and nothing else, or it was the Store stub talking.
    if printf '%s' "$out" | grep -Eq '^[A-Za-z0-9]{32}$'; then
      printf '%s' "$out"
      return 0
    fi
  done
  if command -v openssl >/dev/null 2>&1; then
    # base64 then strip the non-alphanumerics, taking 32 from a longer draw so
    # the result is still 32 characters after +, / and = are removed.
    out="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c 1-32)"
    if printf '%s' "$out" | grep -Eq '^[A-Za-z0-9]{32}$'; then
      printf '%s' "$out"
      return 0
    fi
  fi
  return 1
}

# ---------------------------------------------------------------- 1. the value
if [ "$ROTATE" = true ]; then
  # Alphanumeric only, deliberately. A generated password containing $ or # is
  # valid for RDS but breaks on the way through: Compose treats # as a comment
  # and $ as interpolation inside an --env-file, so the app would send a
  # different string than the one that was stored. Dropping the symbols removes
  # that entire class of failure for the sake of a few bits of entropy that a
  # 32-character alphanumeric password more than covers.
  PW="$(generate_password)"
  [ -n "$PW" ] || die "Could not generate a password: no working python or openssl found."
  log "Generated a new 32-character password"
  aws ssm put-parameter --region "$AWS_REGION" --name "$PARAM" \
    --type SecureString --value "$PW" --overwrite >/dev/null
  log "Stored in Parameter Store"
else
  PW="$(aws ssm get-parameter --region "$AWS_REGION" --name "$PARAM" \
        --with-decryption --query Parameter.Value --output text 2>/dev/null || echo "")"
  [ -n "$PW" ] || die "$PARAM does not exist. Create it, or re-run with --rotate."
  log "Reusing the value already in Parameter Store"
fi

# Warn rather than fail: RDS itself accepts these, so an existing password may
# legitimately contain them - but it will not survive the env-file round trip.
case "$PW" in
  *'$'*|*'#'*|*' '*|*'"'*|*"'"*)
    warn "The password contains \$, #, a quote or a space."
    warn "Docker Compose will mangle it in --env-file. Re-run with --rotate."
    ;;
esac

# ------------------------------------------------------------------- 2. rds
log "Pointing RDS ($DB_ID) at the same value"
aws rds modify-db-instance --region "$AWS_REGION" \
  --db-instance-identifier "$DB_ID" \
  --master-user-password "$PW" \
  --apply-immediately >/dev/null

log "Waiting for the instance to return to available"
aws rds wait db-instance-available --region "$AWS_REGION" \
  --db-instance-identifier "$DB_ID" \
  || die "Timed out waiting for $DB_ID."

# --------------------------------------------------- 3. instance + verify
# deploy.sh rewrites .env.runtime from Parameter Store, runs its own credential
# preflight, applies migrations, then restarts the containers - so this single
# call fixes copy (3) and proves (1) and (2) now agree.
INSTANCE_ID="$(aws ec2 describe-instances --region "$AWS_REGION" \
  --filters "Name=tag:DeployTarget,Values=${PROJECT_NAME}" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)"

if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "None" ]; then
  warn "No running instance tagged DeployTarget=${PROJECT_NAME}."
  warn "RDS and Parameter Store now agree. Run /opt/app/deploy.sh on the host."
  exit 0
fi

log "Redeploying on $INSTANCE_ID"
CMD_ID="$(aws ssm send-command --region "$AWS_REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --comment "Resync DB password" \
  --timeout-seconds 900 \
  --parameters commands='["/opt/app/deploy.sh"]' \
  --query 'Command.CommandId' --output text)"

STATUS=Pending
for _ in $(seq 1 40); do
  STATUS="$(aws ssm get-command-invocation --region "$AWS_REGION" \
    --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --query Status --output text 2>/dev/null || echo Pending)"
  case "$STATUS" in Success|Failed|Cancelled|TimedOut) break ;; esac
  sleep 10
done

echo
aws ssm get-command-invocation --region "$AWS_REGION" \
  --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
  --query StandardOutputContent --output text || true

if [ "$STATUS" != "Success" ]; then
  echo
  aws ssm get-command-invocation --region "$AWS_REGION" \
    --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --query StandardErrorContent --output text >&2 || true
  die "Redeploy failed with status $STATUS. The output above says why."
fi

log "Done. All three copies agree and migrations have been applied."
log "Confirm with:  curl http://<your-ip>/api/v1/health/ready"
