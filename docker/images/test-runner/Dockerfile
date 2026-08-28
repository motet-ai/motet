FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

COPY docker/images/test-runner/requirements.txt /app/docker/images/test-runner/requirements.txt
COPY docker/images/test-runner/install_deps.sh /tmp/install_deps.sh
RUN --mount=type=cache,target=/root/.cache/pip bash /tmp/install_deps.sh && rm /tmp/install_deps.sh

COPY motet /app/motet
COPY config /app/config
COPY docker /app/docker
COPY external /app/external
COPY scripts /app/scripts
COPY tests /app/tests
COPY docs/developer_onboarding /app/docs/developer_onboarding
COPY pyproject.toml /app/
COPY docker/images/test-runner/configure_runtime.sh /tmp/configure_runtime.sh
RUN --mount=type=cache,target=/root/.cache/pip bash /tmp/configure_runtime.sh && rm /tmp/configure_runtime.sh

RUN mkdir -p /var/motet/worker-exec && chmod 700 /var/motet/worker-exec

CMD ["python", "-m", "pytest", "tests/", "-v", "--tb=short"]

