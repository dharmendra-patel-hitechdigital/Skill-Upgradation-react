#!/usr/bin/env bash
#
# Applies Alembic migrations by running a one-off ECS task in the private
# subnets, then waits for it and surfaces its logs.
#
#   ./scripts/aws-migrate.sh [image-uri]
#
# Why a task and not a step in the CI runner: RDS is not publicly accessible, so
# a GitHub-hosted runner simply cannot reach it. Rather than punching a hole in
# the security group for GitHub's IP ranges, the migration runs inside the VPC
# using the API image that is about to be deployed - the same code, the same
# driver versions, the same network position.
#
# Keep migrations ADDITIVE. An ECS rolling update runs old and new tasks side by
# side for a few minutes, so the schema has to satisfy both. Drop a column in a
# later release than the one that stops writing to it.
#
# Environment:
#   PROJECT_NAME  stack/export prefix (default: skill-upgradation)
#   AWS_REGION    defaults to the CLI's configured region

set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-skill-upgradation}"
IMAGE_OVERRIDE="${1:-}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

command -v aws >/dev/null || die "AWS CLI not found on PATH."

# Topology comes from stack exports, not from duplicated CI variables - one
# source of truth, so a rebuilt network stack does not silently desync CI.
export_value() {
  aws cloudformation list-exports \
    --query "Exports[?Name=='${PROJECT_NAME}-$1'].Value" --output text
}

CLUSTER="$(export_value ClusterName)"
SUBNET_A="$(export_value PrivateSubnetA)"
SUBNET_B="$(export_value PrivateSubnetB)"
SECURITY_GROUP="$(export_value ServiceSecurityGroupId)"

for pair in "ClusterName:$CLUSTER" "PrivateSubnetA:$SUBNET_A" \
            "PrivateSubnetB:$SUBNET_B" "ServiceSecurityGroupId:$SECURITY_GROUP"; do
  [ -n "${pair#*:}" ] || die "Missing stack export ${PROJECT_NAME}-${pair%%:*}. Is the network stack deployed?"
done

TASK_FAMILY="${PROJECT_NAME}-api"

# Build the container override. Passing the new image explicitly means the
# migration runs the code being deployed, not the revision currently live.
if [ -n "$IMAGE_OVERRIDE" ]; then
  OVERRIDES=$(printf '{"containerOverrides":[{"name":"api","image":"%s","command":["alembic","upgrade","head"]}]}' "$IMAGE_OVERRIDE")
  log "Migrating with image ${IMAGE_OVERRIDE}"
else
  OVERRIDES='{"containerOverrides":[{"name":"api","command":["alembic","upgrade","head"]}]}'
  log "Migrating with the task definition's current image"
fi

NETWORK=$(printf 'awsvpcConfiguration={subnets=[%s,%s],securityGroups=[%s],assignPublicIp=DISABLED}' \
  "$SUBNET_A" "$SUBNET_B" "$SECURITY_GROUP")

TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$TASK_FAMILY" \
  --launch-type FARGATE \
  --network-configuration "$NETWORK" \
  --overrides "$OVERRIDES" \
  --started-by "migrate-$(date -u +%s)" \
  --query 'tasks[0].taskArn' --output text)

[ -n "$TASK_ARN" ] && [ "$TASK_ARN" != "None" ] || die "run-task returned no task ARN."

TASK_ID="${TASK_ARN##*/}"
log "Task ${TASK_ID} started; waiting for it to stop"

# The waiter polls for up to ~10 minutes. A migration slower than that needs
# rethinking rather than a longer timeout.
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  || die "Timed out waiting for the migration task."

read -r EXIT_CODE STOP_REASON <<<"$(aws ecs describe-tasks \
  --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].[containers[0].exitCode,stoppedReason]' --output text)"

log "Migration logs:"
# The task definition logs to this group with an "api" stream prefix.
aws logs get-log-events \
  --log-group-name "/ecs/${PROJECT_NAME}/api" \
  --log-stream-name "api/api/${TASK_ID}" \
  --query 'events[*].message' --output text 2>/dev/null | sed 's/^/    /' \
  || echo "    (log stream not available yet)"

if [ "$EXIT_CODE" != "0" ]; then
  die "Migration failed with exit code ${EXIT_CODE}. Reason: ${STOP_REASON}"
fi

log "Migrations applied"
