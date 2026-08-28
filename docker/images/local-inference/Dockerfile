# Local Inference Manager — sibling control-plane image (ADR-0105 hoist).
#
# Mirrors docker/images/mcp-manager/Dockerfile and docker/images/embedding-server/Dockerfile:
# a small, dedicated image for one hoisted sibling service rather than reusing the heavy
# worker image (browser/Playwright/celery stack). This image carries only the GGUF inference
# runtime (llama.cpp) plus the motet core needed to run the manager and talk to Redis Streams.
#
# GGUF model weights are NOT baked into the image; they are mounted at runtime
# (MOTET_LOCAL_MODEL_DIR, default /app/models) so the image stays small and model-agnostic.
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    MOTET_LOCAL_INFERENCE_ENABLED=true \
    MOTET_LOCAL_MODEL_DIR=/app/models

WORKDIR /app

COPY docker/images/local-inference/requirements.txt /app/docker/images/local-inference/requirements.txt
COPY docker/images/local-inference/install_deps.sh /tmp/install_deps.sh
RUN --mount=type=cache,target=/root/.cache/pip bash /tmp/install_deps.sh && rm /tmp/install_deps.sh

COPY motet /app/motet
COPY config /app/config
COPY pyproject.toml /app/

# Default model mount point (overridden by a read-only volume in compose / k8s).
RUN mkdir -p /app/models

# manager_id is supplied at runtime via --manager-id / MOTET_LOCAL_INFERENCE_MANAGER_ID.
CMD ["python", "-m", "motet.core.models.local.inference_manager"]
