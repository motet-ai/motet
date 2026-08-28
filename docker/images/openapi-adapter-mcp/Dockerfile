# Motet - OpenAPI adapter MCP stdio sidecar (generic, lightweight)
#
# Copyright (c) 2024-2026 Motet Contributors
# Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
#
# Author: Matt Chisholm <matt@motet.dev>
# Created: 2026-04-07
# Last Modified: 2026-04-07
#
# Description:
#   Small image for running openapi_adapter_server.py under Motet MCP Docker stdio.
#   Only fastmcp + httpx + PyYAML; no full Motet runtime. OpenAPI spec Redis caching
#   is skipped automatically (lazy import of motet.core.distributed.redis_manager fails).
#
# Dependencies:
#   - requirements.txt in this directory
#   - motet/core/tools/mcp_adapters/openapi_adapter_server.py (copied at build)
#
# Build (repo root):
#   docker build -t motet-openapi-adapter-mcp:local \
#     -f docker/images/openapi-adapter-mcp/Dockerfile .
#
# Use in config/mcp_instance_manager.yaml:
#   exec_image: motet-openapi-adapter-mcp:local
#   command: python
#   args:
#     - /app/motet/core/tools/mcp_adapters/openapi_adapter_server.py
#     - --openapi-url ...
#     - --base-url ...

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

COPY docker/images/openapi-adapter-mcp/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

RUN mkdir -p /app/motet/core/tools/mcp_adapters
COPY motet/core/tools/mcp_adapters/openapi_adapter_server.py \
    /app/motet/core/tools/mcp_adapters/openapi_adapter_server.py
