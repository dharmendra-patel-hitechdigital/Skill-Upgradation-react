# syntax=docker/dockerfile:1

# --- Build stage ---
FROM node:20-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

# Vite inlines VITE_* variables into the bundle at BUILD time, so the API URL
# cannot be supplied later as a container env var - it has to arrive here. The
# pipeline passes it with `--build-arg`, which is why the same commit produces a
# different image per environment.
#
# Default "/api/v1" assumes the SPA and the API sit behind one load balancer
# (which is how infra/cloudformation/30-services.yaml routes them): requests are
# same-origin, so no CORS preflight and no hard-coded hostname in the bundle.
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

COPY . .
RUN npm run build


# --- Serve stage ---
FROM nginx:1.27-alpine AS serve

# Build provenance, stamped by the pipeline. `docker inspect` on a running task
# then answers "which commit is this?" without a lookup in CodePipeline.
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.title="skill-upgradation-web"

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

# nginx's own master process needs root to bind :80 and drops to `nginx` for
# workers; the stock image already does that, so no USER override here.
EXPOSE 80

# Matches the ALB target-group health check path. Kept lightweight on purpose -
# it is polled every 15s per task.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
