#!/usr/bin/env bash
#
# Portable CI logic. Every CI system calls THIS; none of them reimplements it.
#
#   ./scripts/ci.sh test                    lint + tests, in a container
#   ./scripts/ci.sh build <tag> [api|web]   build images (default: both)
#   ./scripts/ci.sh push  <tag> [api|web]   push images  (default: both)
#   ./scripts/ci.sh images <tag>            print the image URIs
#   ./scripts/ci.sh all   <tag>             test, build, push
#
# The optional service argument matters: CodePipeline runs one CodeBuild project
# per service in parallel, and without it each project would rebuild both images
# and double the work.
#
# The reason this file exists: build logic was duplicated across three
# buildspecs, and adding GitHub Actions would have made a fourth copy of "how to
# build this project". Copies drift. Vendor CI configs should own only what is
# genuinely vendor-specific - authentication and triggering - and delegate the
# rest here.
#
# Requires only Docker. Registry auth is the caller's job, because that is the
# one part that legitimately differs per platform.
#
# Environment:
#   REGISTRY      required for build/push
#                 e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com
#   PROJECT_NAME  image name prefix (default: skill-upgradation)
#   VITE_API_BASE_URL  baked into the SPA bundle at build time (default: /api/v1)
#   PYTHON_IMAGE / NODE_IMAGE / NGINX_IMAGE
#                 base images, defaulting to the ECR Public mirrors (see below)

set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-skill-upgradation}"
VITE_API_BASE_URL="${VITE_API_BASE_URL:-/api/v1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Base images come from the ECR Public mirror of the Docker Official Images, not
# from Docker Hub. Docker Hub enforces its anonymous pull limit per source IP,
# and CodeBuild egresses through shared NAT addresses, so `python:3.12-slim`
# returns HTTP 429 "Too Many Requests" on a schedule nobody here controls - the
# build fails on traffic from unrelated AWS customers. ECR Public applies no
# such limit to pulls from AWS and serves the same digests.
#
# Exported because docker-compose.ci.yml interpolates them.
export PYTHON_IMAGE="${PYTHON_IMAGE:-public.ecr.aws/docker/library/python:3.12-slim}"
export NODE_IMAGE="${NODE_IMAGE:-public.ecr.aws/docker/library/node:20-alpine}"
export NGINX_IMAGE="${NGINX_IMAGE:-public.ecr.aws/docker/library/nginx:1.27-alpine}"

# Registry reads stay flaky even without a rate limit (TLS resets, 5xx from a
# CDN edge, DNS blips). Retrying the whole docker invocation is safe: a build is
# idempotent, and a partial pull is discarded rather than cached.
retry() {
  local attempts=3 delay=5 attempt=1
  until "$@"; do
    if [ "$attempt" -ge "$attempts" ]; then
      # "$*", not "$1 $2": under `set -u` a single-word command would make the
      # error handler itself die on an unbound $2, hiding the real failure.
      die "'$*' failed after ${attempts} attempts."
    fi
    printf '\033[1;33m..\033[0m attempt %d/%d failed; retrying in %ds\n' \
      "$attempt" "$attempts" "$delay" >&2
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 3))
  done
}

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# Checked per-action rather than at load time, so `--help` still works on a
# machine without Docker installed.
require_docker() {
  command -v docker >/dev/null || die "Docker is required but not on PATH."
}

# Short commit sha is the default tag: immutable, and it ties a running
# container back to a diff without a lookup in the CI tool.
default_tag() {
  git -C "$REPO_ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo "dev"
}

api_image() { printf '%s/%s-api:%s' "${REGISTRY:?REGISTRY is required}" "$PROJECT_NAME" "$1"; }
web_image() { printf '%s/%s-web:%s' "${REGISTRY:?REGISTRY is required}" "$PROJECT_NAME" "$1"; }

# Rejects anything unrecognised rather than silently doing nothing - a typo in a
# CI config should fail loudly, not produce a green build that shipped no image.
check_service() {
  case "$1" in
    api|web|all) return 0 ;;
    *) die "Unknown service '$1'. Use: api | web | all" ;;
  esac
}

wants() {
  [ "$1" = "all" ] || [ "$1" = "$2" ]
}

cmd_test() {
  require_docker
  # Where the container drops its junit report (bind-mounted in
  # docker-compose.ci.yml) for the CI system to publish. Created here, not by
  # Docker: a bind-mount directory Docker creates is root-owned, and the test
  # stage runs as uid 1001, so pytest could not write into it. Cleared first so
  # a crashed run cannot republish the previous run's results as this one's.
  REPORT_DIR="${REPO_ROOT}/test-reports"
  mkdir -p "$REPORT_DIR"
  chmod 777 "$REPORT_DIR"
  rm -f "$REPORT_DIR/junit.xml"

  log "Base images: ${PYTHON_IMAGE} | ${NODE_IMAGE} | ${NGINX_IMAGE}"

  # Built as its own step, and retried, so a registry hiccup is reported as
  # "could not fetch the base image" rather than as a failed test suite. Without
  # this split, `run` does the pull and a 429 looks identical to a red build.
  log "Building the test image"
  retry docker compose -f "${REPO_ROOT}/docker-compose.ci.yml" build tests

  log "Running lint + tests in a container"
  # --rm so a failed run leaves nothing for the next build to trip over. Not
  # retried: a failing suite must fail once, not three times.
  docker compose -f "${REPO_ROOT}/docker-compose.ci.yml" run --rm tests
  log "Verifying the SPA bundle compiles"
  retry docker compose -f "${REPO_ROOT}/docker-compose.ci.yml" build web
}

build_api() {
  log "Building API image $1"
  retry docker build \
    --file "${REPO_ROOT}/Python/Dockerfile" \
    --target runtime \
    --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" \
    --build-arg GIT_COMMIT="$1" \
    --build-arg BUILD_TIME="$2" \
    --tag "$(api_image "$1")" \
    --tag "$(api_image latest)" \
    "${REPO_ROOT}/Python"
}

build_web() {
  log "Building web image $1 (API base ${VITE_API_BASE_URL})"
  retry docker build \
    --file "${REPO_ROOT}/React/Dockerfile" \
    --build-arg VITE_API_BASE_URL="$VITE_API_BASE_URL" \
    --build-arg NODE_IMAGE="$NODE_IMAGE" \
    --build-arg NGINX_IMAGE="$NGINX_IMAGE" \
    --build-arg GIT_COMMIT="$1" \
    --build-arg BUILD_TIME="$2" \
    --tag "$(web_image "$1")" \
    --tag "$(web_image latest)" \
    "${REPO_ROOT}/React"
}

cmd_build() {
  require_docker
  local tag="${1:-$(default_tag)}"
  local service="${2:-all}"
  check_service "$service"
  local build_time
  build_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  wants "$service" api && build_api "$tag" "$build_time"
  wants "$service" web && build_web "$tag" "$build_time"
  return 0
}

cmd_push() {
  require_docker
  local tag="${1:-$(default_tag)}"
  local service="${2:-all}"
  check_service "$service"
  log "Pushing ${service} @ ${tag}"

  # The sha tag is what deployments reference; `latest` is a human convenience
  # and is never deployed from.
  # Retried: a push that dies partway through is resumable, and a transient ECR
  # 5xx after a green suite is the most wasteful way for a pipeline to fail.
  if wants "$service" api; then
    retry docker push "$(api_image "$tag")"
    retry docker push "$(api_image latest)"
  fi
  if wants "$service" web; then
    retry docker push "$(web_image "$tag")"
    retry docker push "$(web_image latest)"
  fi
  log "Pushed"
  return 0
}

cmd_images() {
  # Machine-readable, for a CI step that needs to pass the URIs onward.
  local tag="${1:-$(default_tag)}"
  printf 'api=%s\nweb=%s\n' "$(api_image "$tag")" "$(web_image "$tag")"
}

main() {
  local action="${1:-}"
  case "$action" in
    test)   cmd_test ;;
    build)  cmd_build "${2:-}" "${3:-all}" ;;
    push)   cmd_push  "${2:-}" "${3:-all}" ;;
    images) cmd_images "${2:-}" ;;
    all)
      cmd_test
      cmd_build "${2:-}"
      cmd_push "${2:-}"
      ;;
    ""|-h|--help)
      sed -n '3,28p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
      ;;
    *) die "Unknown action '${action}'. Try: test | build | push | images | all" ;;
  esac
}

main "$@"
