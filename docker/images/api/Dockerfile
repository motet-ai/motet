# Motet API image — frontend + python deps + thin runtime (Phase B).
#
# Local / compose:
#   docker build -f docker/images/api/Dockerfile -t motet-api:latest .
#
# EC2 / shared deps:
#   docker build --target frontend-builder -t "$ECR/motet-api-frontend:$FHASH" ...
#   docker build --target deps -t "$ECR/motet-api-deps:$DHASH" ...
#   docker build \
#     --build-arg DEPS_IMAGE="$ECR/motet-api-deps:$DHASH" \
#     --build-arg FRONTEND_IMAGE="$ECR/motet-api-frontend:$FHASH" \
#     -t "$ECR/motet-api:latest" ...
#
# Note: COPY --from does not expand ARGs; alias the frontend via
# FROM ${FRONTEND_IMAGE} AS frontend-assets (BuildKit workaround).

ARG DEPS_IMAGE=deps
ARG FRONTEND_IMAGE=frontend-builder

# Frontend assets are architecture-independent; build on the native platform so
# esbuild is not run under amd64 emulation on Apple Silicon.
FROM --platform=$BUILDPLATFORM node:20-slim AS frontend-builder
WORKDIR /build
COPY motet/interfaces/frontend /build/frontend
WORKDIR /build/frontend
RUN npm ci && npm run build

FROM python:3.11-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_HEADLESS=true \
    PLAYWRIGHT_BROWSERS_PATH=/tmp/playwright-browsers \
    CHROME_BIN=/usr/bin/chromium \
    CHROME_PATH=/usr/bin/chromium \
    CHROME_FLAGS="--no-sandbox --disable-setuid-sandbox --disable-dev-shm-usage --disable-gpu --remote-debugging-port=9222" \
    NODE_ENV=production \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

COPY requirements.txt requirements-local-cpu.txt /app/
COPY docker/images/api/requirements.txt /app/docker/images/api/requirements.txt
COPY docker/images/api/install_deps.sh /tmp/install_deps.sh
RUN bash /tmp/install_deps.sh && rm /tmp/install_deps.sh

# workspace-mcp editable install (configure_runtime) needs external/ in deps so
# Motet-only rebuilds do not re-run pip -e.
COPY external /app/external
COPY docker/images/api/configure_runtime.sh /tmp/configure_runtime.sh
RUN bash /tmp/configure_runtime.sh && rm /tmp/configure_runtime.sh

# Resolve frontend either from the local frontend-builder stage or a published
# motet-api-frontend:<hash> image (Phase B).
FROM ${FRONTEND_IMAGE} AS frontend-assets

FROM ${DEPS_IMAGE} AS runtime

# Copy built Vite apps so API can serve them at /chat-explorer/ and /manage/ (e.g. AWS).
# Local dev: volume .:/app overwrites this; use vite-dev and vite-admin instead.
COPY --from=frontend-assets /build/frontend/apps/chat-explorer/dist /app/static/chat-explorer
COPY --from=frontend-assets /build/frontend/apps/ops-dashboard/dist /app/static/manage

COPY motet /app/motet
COPY config /app/config
# Developer onboarding docs for ops-dashboard Documentation page (/api/v1/developer-docs)
COPY docs/developer_onboarding /app/docs/developer_onboarding
COPY docker /app/docker
COPY scripts /app/scripts
COPY pyproject.toml /app/

# Dedicated scratch for core.worker_exec when API falls back to local tool execution; match MOTET_WORKER_EXEC_CWD_ALLOWLIST.
RUN mkdir -p /var/motet/worker-exec && chmod 700 /var/motet/worker-exec

EXPOSE 8000
CMD ["python", "-m", "motet.server", "--host", "0.0.0.0", "--port", "8000"]
