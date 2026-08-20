#!/usr/bin/env bash
#
# Deploys the CloudFormation stacks in dependency order.
#
#   ./infra/scripts/deploy.sh network
#   ./infra/scripts/deploy.sh data
#   ./infra/scripts/deploy.sh services
#   ./infra/scripts/deploy.sh pipeline
#   ./infra/scripts/deploy.sh all
#
# On Windows run this from Git Bash. Requires the AWS CLI v2, authenticated.
#
# Order matters and is not merely conventional: each stack imports exports from
# the one before it, so deploying out of order fails with an unresolved-export
# error rather than a helpful message.

set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-skill-upgradation}"
AWS_REGION="${AWS_REGION:-us-east-1}"
TEMPLATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../cloudformation" && pwd)"

# Required by the pipeline stack only.
GITHUB_OWNER="${GITHUB_OWNER:-}"
GITHUB_REPO="${GITHUB_REPO:-}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
CODESTAR_CONNECTION_ARN="${CODESTAR_CONNECTION_ARN:-}"
NOTIFICATION_EMAIL="${NOTIFICATION_EMAIL:-}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

deploy_stack() {
  local suffix="$1" template="$2"; shift 2
  local stack="${PROJECT_NAME}-${suffix}"

  log "Deploying ${stack} from $(basename "$template")"
  # --no-fail-on-empty-changeset so re-running the script is a no-op rather than
  # an error, which makes it safe to use as "make sure everything is applied".
  aws cloudformation deploy \
    --region "$AWS_REGION" \
    --stack-name "$stack" \
    --template-file "$template" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --parameter-overrides "ProjectName=${PROJECT_NAME}" "$@"

  log "${stack} done"
}

outputs() {
  aws cloudformation describe-stacks \
    --region "$AWS_REGION" \
    --stack-name "${PROJECT_NAME}-$1" \
    --query 'Stacks[0].Outputs' \
    --output table
}

deploy_network()  { deploy_stack network  "${TEMPLATE_DIR}/10-network.yaml"  "${@}"; }
deploy_data()     { deploy_stack data     "${TEMPLATE_DIR}/20-data.yaml"     "${@}"; }

deploy_services() {
  # The service stack references images by tag. If ECR is still empty the tasks
  # cannot start and the stack sits in CREATE_IN_PROGRESS until it times out -
  # so fail fast with an actionable message instead.
  local repo="${PROJECT_NAME}-api"
  if ! aws ecr describe-images --region "$AWS_REGION" \
        --repository-name "$repo" --image-ids imageTag=latest >/dev/null 2>&1; then
    die "No ':latest' image in ECR repo '${repo}'.
    Run ./infra/scripts/bootstrap-images.sh first, or let the pipeline build
    once before deploying this stack."
  fi
  deploy_stack services "${TEMPLATE_DIR}/30-services.yaml" "${@}"
}

deploy_pipeline() {
  [ -n "$GITHUB_OWNER" ] || die "GITHUB_OWNER is required for the pipeline stack."
  [ -n "$GITHUB_REPO" ]  || die "GITHUB_REPO is required for the pipeline stack."

  local params=(
    "GitHubOwner=${GITHUB_OWNER}"
    "GitHubRepo=${GITHUB_REPO}"
    "GitHubBranch=${GITHUB_BRANCH}"
    "CodeStarConnectionArn=${CODESTAR_CONNECTION_ARN}"
    "NotificationEmail=${NOTIFICATION_EMAIL}"
  )
  deploy_stack pipeline "${TEMPLATE_DIR}/40-pipeline.yaml" "${params[@]}" "${@}"

  if [ -z "$CODESTAR_CONNECTION_ARN" ]; then
    warn "A new CodeConnections connection was created in PENDING state."
    warn "CloudFormation cannot complete the GitHub OAuth handshake for you."
    warn "Finish it here, then release a change to start the first run:"
    warn "  https://${AWS_REGION}.console.aws.amazon.com/codesuite/settings/connections?region=${AWS_REGION}"
  fi
}

main() {
  command -v aws >/dev/null || die "AWS CLI not found on PATH."
  aws sts get-caller-identity >/dev/null \
    || die "AWS credentials are not configured or have expired."

  case "${1:-all}" in
    network)  deploy_network  "${@:2}" ;;
    data)     deploy_data     "${@:2}" ;;
    services) deploy_services "${@:2}" ;;
    pipeline) deploy_pipeline "${@:2}" ;;
    all)
      deploy_network
      deploy_data
      deploy_services
      deploy_pipeline
      log "Application URL:"
      outputs network | grep -i loadbalancerdns || true
      ;;
    outputs)  outputs "${2:-network}" ;;
    *) die "Unknown target '${1}'. Use: network | data | services | pipeline | all | outputs <stack>" ;;
  esac
}

main "$@"
