# Motet embedding-server — torch/HF deps + thin runtime (Phase B).
#
# Local: docker build -f docker/images/embedding-server/Dockerfile -t ...
# EC2:   --target deps → embedding-server-deps:<lockhash>, then
#        --build-arg DEPS_IMAGE=... → embedding-server:latest

ARG DEPS_IMAGE=deps

FROM python:3.11-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/opt/imf_hf_cache \
    TRANSFORMERS_CACHE=/opt/imf_hf_cache/transformers \
    SENTENCE_TRANSFORMERS_HOME=/opt/imf_hf_cache/sentence_transformers

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY docker/images/embedding-server/requirements.txt /app/docker/images/embedding-server/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu 'torch>=2.0.0' \
    && pip install --no-cache-dir -r /app/docker/images/embedding-server/requirements.txt

# Model preload lives in deps so Motet-only rebuilds do not re-download HF weights.
COPY docker/images/embedding-server/preload_hf_models.sh /tmp/preload_hf_models.sh
RUN bash /tmp/preload_hf_models.sh && rm /tmp/preload_hf_models.sh

FROM ${DEPS_IMAGE} AS runtime

COPY motet /app/motet
COPY config /app/config
COPY pyproject.toml /app/

EXPOSE 8091

CMD ["python", "-m", "motet.core.embedding.server", "--host", "0.0.0.0", "--port", "8091"]
