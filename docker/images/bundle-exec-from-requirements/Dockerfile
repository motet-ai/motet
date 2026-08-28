# Motet - Minimal bundle exec image from requirements.txt (Phase 3 CI)
#
# Copyright (c) 2024-2026 Motet Contributors
# Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
#
# Build context must be a directory containing requirements.txt at its root
# (CI can copy bundle exec/requirements.txt there). Example:
#
#   SHA=$(python3 -c "import hashlib, pathlib, sys; \\
#         print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())" ctx/requirements.txt)
#   docker build --build-arg REQUIREMENTS_SHA256="$SHA" \\
#     -f docker/images/bundle-exec-from-requirements/Dockerfile -t your/registry/bundle-exec:tag ctx/

ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG REQUIREMENTS_SHA256=
ARG BASE_IMAGE_STACK=
LABEL motet.bundle.exec.requirements.sha256="${REQUIREMENTS_SHA256}" \
      motet.bundle.exec.base_image_stack="${BASE_IMAGE_STACK}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /work

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
