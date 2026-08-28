# Motet worker-lifecycle — deps + thin runtime (Phase B).

ARG DEPS_IMAGE=deps

FROM python:3.11-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

COPY requirements.txt requirements-local-cpu.txt /app/
COPY docker/images/worker_lifecycle/requirements.txt /app/docker/images/worker_lifecycle/requirements.txt
COPY docker/images/worker_lifecycle/install_deps.sh /tmp/install_deps.sh
RUN --mount=type=cache,target=/root/.cache/pip bash /tmp/install_deps.sh && rm /tmp/install_deps.sh

COPY docker/images/worker_lifecycle/configure_runtime.sh /tmp/configure_runtime.sh
RUN --mount=type=cache,target=/root/.cache/pip bash /tmp/configure_runtime.sh && rm /tmp/configure_runtime.sh

FROM ${DEPS_IMAGE} AS runtime

COPY motet /app/motet
COPY config /app/config
COPY docker /app/docker
COPY external /app/external
COPY scripts /app/scripts
COPY pyproject.toml /app/

RUN mkdir -p /var/motet/worker-exec && chmod 700 /var/motet/worker-exec

CMD ["celery", "-A", "motet.core.workers.tasks", "worker", "--loglevel=info"]
