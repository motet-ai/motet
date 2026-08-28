#!/usr/bin/env bash
set -euo pipefail

if [ -d "/app/external/workspace-mcp" ]; then
    if [ ! -f "/app/external/workspace-mcp/pyproject.toml" ] && [ ! -f "/app/external/workspace-mcp/setup.py" ]; then
        echo "❌ workspace-mcp submodule missing build metadata" >&2
        exit 1
    fi
    cd /app/external/workspace-mcp
    pip install --no-cache-dir -e .
    echo "✅ Installed workspace-mcp fork from /app/external/workspace-mcp"
else
    echo "ℹ️  workspace-mcp fork not found; skipping install for API image"
fi

echo "ℹ️  API runtime does not install Playwright browsers or Node-based MCP helpers."

