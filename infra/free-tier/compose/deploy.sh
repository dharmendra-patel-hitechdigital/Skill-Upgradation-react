#!/bin/bash
#
# Runs ON the EC2 instance. Invoked two ways:
#   * by the pipeline, through SSM Run Command, with the image tag as $1
#   * by systemd (app-stack.service) after a reboot, with no argument
#
# The pipeline uploads this file to S3 alongside the compose bundle, so editing
# it in the repo is enough - no instance replacement needed.
#
# Environment specifics come from /opt/app/app.env, written by the instance's
# UserData at launch. This script stays environment-agnostic.

set -euo pipefail

APP_DIR=/opt/app
# shellcheck disable=SC1091
source "${APP_DIR}/app.env"

# On a systemd restart there is no argument, and the tag that is already
# deployed is the one we want back - not whatever "latest" now points at.
IMAGE_TAG="${1:-}"
if [ -z "$IMAGE_TAG" ] && [ -f "${APP_DIR}/.image_tag" ]; then
  IMAGE_TAG="$(cat "${APP_DIR}/.image_tag")"
fi
IMAGE_TAG="${IMAGE_TAG:-latest}"

log() { printf '[deploy %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

log "Deploying tag ${IMAGE_TAG}"

# Pull the current compose bundle. --delete keeps the directory honest, but the
# excludes protect the two files this script generates itself.
aws s3 sync "s3://${DEPLOY_BUCKET}/compose/" "$APP_DIR/" --delete \
  --exclude 'app.env' --exclude '.env.runtime' --exclude '.image_tag'

# Written by the build. Carries PUBLIC_ORIGIN, which is a build-time fact rather
# than an instance-launch one, so it does not belong in app.env.
if [ -f "${APP_DIR}/build.env" ]; then
  # shellcheck disable=SC1091
  source "${APP_DIR}/build.env"
fi

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

# Secrets are read at deploy time from Parameter Store rather than baked into
# the image or the compose file. OPENAI_API_KEY is optional - the API falls back
# to its offline analyzer when it is absent, so a missing parameter must not be
# fatal here.
get_param() {
  aws ssm get-parameter --region "$AWS_REGION" \
    --name "/${PROJECT_NAME}/$1" --with-decryption \
    --query Parameter.Value --output text 2>/dev/null || echo ""
}

DB_PASSWORD="$(get_param DB_PASSWORD)"
SECRET_KEY="$(get_param SECRET_KEY)"
OPENAI_API_KEY="$(get_param OPENAI_API_KEY)"

if [ -z "$DB_PASSWORD" ] || [ -z "$SECRET_KEY" ]; then
  echo "Missing /${PROJECT_NAME}/DB_PASSWORD or /${PROJECT_NAME}/SECRET_KEY in" \
       "Parameter Store. See infra/free-tier/README.md - CloudFormation cannot" \
       "create SecureString parameters, so they are created by hand." >&2
  exit 1
fi

# 077 so the env file holding these values is never world-readable.
umask 077
cat > "${APP_DIR}/.env.runtime" <<EOF
API_IMAGE=${ECR_REGISTRY}/${PROJECT_NAME}-api:${IMAGE_TAG}
WEB_IMAGE=${ECR_REGISTRY}/${PROJECT_NAME}-web:${IMAGE_TAG}
DB_HOST=${DB_HOST}
DB_PASSWORD=${DB_PASSWORD}
SECRET_KEY=${SECRET_KEY}
OPENAI_API_KEY=${OPENAI_API_KEY}
PUBLIC_ORIGIN=${PUBLIC_ORIGIN:-}
EOF

compose() {
  docker compose \
    --env-file "${APP_DIR}/.env.runtime" \
    -f "${APP_DIR}/docker-compose.prod.yml" "$@"
}

log "Pulling images"
compose pull

# Migrations run before the new containers take over, mirroring the Migrate
# stage of the Fargate pipeline. A failure here stops the deploy with the old
# containers still serving.
log "Applying migrations"
compose run --rm --no-deps api alembic upgrade head

log "Starting stack"
compose up -d --remove-orphans

echo "$IMAGE_TAG" > "${APP_DIR}/.image_tag"

# A 20 GB disk and a 500 MB ECR allowance neither of them tolerate an unbounded
# pile of old layers.
log "Pruning old images"
docker image prune -af --filter "until=168h" || true

log "Done - tag ${IMAGE_TAG} is live"
