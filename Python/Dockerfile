# No `# syntax=` directive on purpose. It makes every build resolve
# docker.io/docker/dockerfile:1 first, which is one more anonymous Docker Hub
# request per build on top of the base images - and Docker Hub rate-limits by
# source IP, which in CodeBuild is a shared NAT address. Nothing here needs a
# newer frontend than the one BuildKit ships with; if that changes, prefer
# `--build-arg BUILDKIT_SYNTAX=<mirrored ref>` over reintroducing the pull.

# ---------------------------------------------------------------- base image
# Pulled from the ECR Public mirror of Docker Official Images rather than Docker
# Hub directly. Docker Hub's anonymous pull limit is enforced per source IP, and
# CodeBuild egresses through shared NAT addresses, so an unauthenticated
# `python:3.12-slim` pull fails with HTTP 429 unpredictably - a red pipeline
# caused by someone else's traffic. ECR Public imposes no such limit on pulls
# from AWS, and the digests are identical to Docker Hub's.
#
# Overridable so nothing is locked to one registry: point this at
# `python:3.12-slim` for a plain Docker Hub pull, or at an ECR pull-through
# cache repository if you have one.
#
# Declared before the first FROM, which makes it global and therefore usable by
# both the builder and the runtime stage below.
ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim

# ---------------------------------------------------------------- builder
# Dependencies are built into a wheelhouse in a throwaway stage so compilers and
# build headers never reach the runtime image.
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copied alone so the layer is cached until the dependency list itself changes.
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime

# Build provenance, stamped by the pipeline. `docker inspect` on a running task
# then answers "which commit is this?" without a lookup in CodePipeline.
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.title="skill-upgradation-api"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Run as a non-root user: a container escape should not start from uid 0.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home app

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY --chown=app:app alembic.ini pyproject.toml ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app
COPY --chown=app:app db ./db

# Local-storage mount point. Declared as a volume so uploaded documents survive
# a container replacement - though multi-replica deployments should set
# STORAGE_BACKEND=s3 instead, since local disk is not shared between instances.
RUN mkdir -p /app/var/documents && chown -R app:app /app/var
VOLUME ["/app/var"]

USER app

EXPOSE 8000

# Hits the readiness endpoint, so an unreachable database marks the container
# unhealthy rather than merely "running".
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=4).status == 200 else 1)"

# One uvicorn worker per container; scale with replicas and let the orchestrator
# handle placement. Multiple in-process workers would each run their own
# background task runner and compete for the same documents.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]


# ---------------------------------------------------------------- test
# Built with `--target test`. Extends the runtime image rather than starting
# fresh, so the suite exercises the exact interpreter, wheels and layout that
# ship to production - the usual "passes in CI, fails in the image" gap comes
# from testing somewhere the app never actually runs.
#
# Not part of the default build: `docker build` without --target stops at
# runtime, so none of this reaches the deployed image.
FROM runtime AS test

USER root

COPY requirements-dev.txt .
# requirements.txt is already satisfied by the runtime stage; this resolves to
# just the dev additions (pytest, ruff, mypy).
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY --chown=app:app tests ./tests

USER app

# Overridden by docker-compose.ci.yml to run lint in the same pass.
CMD ["pytest", "-q"]
