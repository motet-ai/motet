# Motet scheduler (Celery beat) — deps + thin runtime (Phase B).

ARG DEPS_IMAGE=deps

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

COPY requirements.txt /app/
COPY docker/images/scheduler/requirements.txt /app/docker/images/scheduler/requirements.txt
COPY docker/images/scheduler/install_deps.sh /tmp/install_deps.sh
RUN bash /tmp/install_deps.sh && rm /tmp/install_deps.sh

FROM ${DEPS_IMAGE} AS runtime

COPY motet /app/motet
COPY config /app/config
COPY docker /app/docker
COPY external /app/external
COPY scripts /app/scripts
COPY pyproject.toml /app/

CMD ["celery", "-A", "motet.core.workers.tasks", "beat", "--loglevel=info"]
