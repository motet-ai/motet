# Motet mcp-manager — deps + thin runtime (Phase B).

ARG DEPS_IMAGE=deps

FROM python:3.11-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    MCP_INSTANCE_MANAGER_CONFIG=/app/config/mcp_instance_manager.yaml \
    MOTET_MCP_EXEC_BACKEND=docker

WORKDIR /app

COPY docker/images/mcp-manager/requirements.txt /app/docker/images/mcp-manager/requirements.txt
COPY docker/images/mcp-manager/install_deps.sh /tmp/install_deps.sh
RUN --mount=type=cache,target=/root/.cache/pip bash /tmp/install_deps.sh && rm /tmp/install_deps.sh

FROM ${DEPS_IMAGE} AS runtime

COPY motet /app/motet
COPY config /app/config
COPY pyproject.toml /app/

CMD ["python", "-m", "motet.core.tools.mcp_motet.proxy.mcp_instance_manager"]
