#!/usr/bin/env bash
#
# Builds and pushes the first :latest image for each service, from your machine.
#
#   ./infra/scripts/bootstrap-images.sh
#
# This solves a genuine ordering problem: the service stack (30-services.yaml)
# creates ECS services whose task definitions reference an image tag, but the
# pipeline that produces those images cannot deploy to services that do not
# exist yet. Something has to break the cycle, and one manual push is the
# simplest way to do it. After this, every image comes from CodePipeline and
# this script is not needed again.
#
# On Windows run this from Git Bash, with Docker Desktop running.

set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-skill-upgradation}"
AWS_REGION="${AWS_REGION:-us-east-1}"
# Same default as the pipeline: the SPA and API share one origin behind the ALB.
VITE_API_BASE_URL="${VITE_API_BASE_URL:-/api/v1}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

command -v aws    >/dev/null || die "AWS CLI not found on PATH."
command -v docker >/dev/null || die "Docker not found on PATH."
aws sts get-caller-identity >/dev/null || die "AWS credentials are not configured."

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
API_REPO="${REGISTRY}/${PROJECT_NAME}-api"
WEB_REPO="${REGISTRY}/${PROJECT_NAME}-web"

# The repositories are created by 10-network.yaml, not here - so a missing repo
# means the network stack has not been deployed, and that is worth saying.
for repo in "${PROJECT_NAME}-api" "${PROJECT_NAME}-web"; do
  aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$repo" >/dev/null 2>&1 \
    || die "ECR repository '${repo}' does not exist. Deploy the network stack first:
    ./infra/scripts/deploy.sh network"
done

log "Logging in to ${REGISTRY}"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

# Local builds have a git checkout, unlike CodeBuild's source zip.
GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo bootstrap)"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

log "Building API image (${GIT_COMMIT})"
docker build \
  --file "${REPO_ROOT}/Python/Dockerfile" \
  --build-arg GIT_COMMIT="$GIT_COMMIT" \
  --build-arg BUILD_TIME="$BUILD_TIME" \
  --tag "${API_REPO}:${GIT_COMMIT}" \
  --tag "${API_REPO}:latest" \
  "${REPO_ROOT}/Python"

log "Building web image (${GIT_COMMIT}), API base '${VITE_API_BASE_URL}'"
docker build \
  --file "${REPO_ROOT}/React/Dockerfile" \
  --build-arg VITE_API_BASE_URL="$VITE_API_BASE_URL" \
  --build-arg GIT_COMMIT="$GIT_COMMIT" \
  --build-arg BUILD_TIME="$BUILD_TIME" \
  --tag "${WEB_REPO}:${GIT_COMMIT}" \
  --tag "${WEB_REPO}:latest" \
  "${REPO_ROOT}/React"

log "Pushing"
docker push "${API_REPO}:${GIT_COMMIT}"
docker push "${API_REPO}:latest"
docker push "${WEB_REPO}:${GIT_COMMIT}"
docker push "${WEB_REPO}:latest"

log "Done. Now deploy the service stack:"
printf '    ./infra/scripts/deploy.sh services\n'
