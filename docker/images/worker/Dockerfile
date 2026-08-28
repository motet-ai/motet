# Motet worker image — deps stage + thin runtime (Phase A/B).
#
# Local / compose (unchanged UX):
#   docker build -f docker/images/worker/Dockerfile -t motet-worker:latest .
#   Builds the deps stage then layers Motet source on top.
#
# Phase B (EC2 / shared deps):
#   docker build --target deps -t "$ECR/worker-deps:$LOCK" ...
#   docker build --build-arg DEPS_IMAGE="$ECR/worker-deps:$LOCK" -t "$ECR/worker:latest" ...
#   Motet-only rebuilds skip apt/pip/Playwright when DEPS_IMAGE is reused.
#
# DEPS_IMAGE must be declared before the first FROM so BuildKit can substitute it
# into the runtime FROM line (Docker multi-stage ARG rules).

ARG DEPS_IMAGE=deps

FROM python:3.11-slim AS deps

# ADR-0118: opt-in Whisper CLI for video transcript derivation (pulls torch — keep
# off by default; workers do not load models per ADR-0105).
ARG INSTALL_WHISPER=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_HEADLESS=true \
    PLAYWRIGHT_BROWSERS_PATH=/tmp/playwright-browsers \
    CHROME_BIN=/usr/local/bin/chromium \
    CHROME_PATH=/usr/local/bin/chromium \
    CHROME_FLAGS="--no-sandbox --disable-setuid-sandbox --disable-dev-shm-usage --disable-gpu --remote-debugging-port=9222" \
    NODE_ENV=production \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

COPY requirements.txt requirements-local-cpu.txt /app/
COPY docker/images/worker/requirements.txt /app/docker/images/worker/requirements.txt
COPY docker/images/worker/install_deps.sh /tmp/install_deps.sh
RUN --mount=type=cache,target=/root/.cache/pip INSTALL_WHISPER=${INSTALL_WHISPER} bash /tmp/install_deps.sh && rm /tmp/install_deps.sh

# NLTK corpora preload (wordnet / omw-1.4); no Hugging Face embedding bake-in (ADR-0107 M3)
COPY docker/images/worker/preload_hf_models.sh /tmp/preload_hf_models.sh
RUN bash /tmp/preload_hf_models.sh && rm /tmp/preload_hf_models.sh

# Playwright Chromium lives in the deps stage so Motet-only code changes do not
# re-download browsers (Phase A image-layer hygiene).
COPY docker/images/worker/configure_runtime.sh /tmp/configure_runtime.sh
RUN --mount=type=cache,target=/root/.cache/pip bash /tmp/configure_runtime.sh && rm /tmp/configure_runtime.sh

FROM ${DEPS_IMAGE} AS runtime

COPY motet /app/motet
COPY config /app/config
# Agent-facing docs for core.docs_read (same tree as GET /api/v1/developer-docs)
COPY docs/developer_onboarding /app/docs/developer_onboarding
COPY docker /app/docker
COPY external /app/external
COPY scripts /app/scripts
COPY pyproject.toml /app/

COPY docker/images/worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Dedicated scratch for core.worker_exec (not world-writable /tmp); match MOTET_WORKER_EXEC_CWD_ALLOWLIST in compose.
RUN mkdir -p /var/motet/worker-exec && chmod 700 /var/motet/worker-exec

ENTRYPOINT ["/entrypoint.sh"]
CMD ["celery", "-A", "motet.core.workers.tasks", "worker", "--loglevel=info"]
