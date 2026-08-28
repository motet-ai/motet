#!/usr/bin/env bash
set -euo pipefail

# ADR-0105: MCP servers are NOT built into the worker image. Each MCP server runs in its
# own Docker sidecar (config/mcp_instance_manager.yaml exec_image: motet-google-workspace-mcp,
# motet-playwright-mcp, ...) spawned by the `mcp-manager` sibling; npx servers use
# MOTET_MCP_DOCKER_IMAGE=node:20-bookworm-slim. So the worker no longer installs Node,
# the workspace-mcp fork (pip -e), or builds web-search-mcp (npm/tsc).
# (If you ever run a worker with MOTET_MCP_EXEC_BACKEND=subprocess, build those MCP servers
# in a derived image instead of here.)

# Install Chromium once via Python Playwright only.
# The MCP server uses PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH (set in mcp_instance_manager.yaml)
# to point at the symlink /usr/local/bin/chromium, so it skips its own version-specific
# browser discovery and reuses the same binary.
python -m playwright install chromium

# Symlink so CHROME_BIN/CHROME_PATH point at Playwright's Chromium (shared by http_get_browser and MCP)
# Prefer classic chromium (chrome); fall back to headless_shell (newer Playwright on Linux)
PLAYWRIGHT_BROWSERS="${PLAYWRIGHT_BROWSERS_PATH:-/tmp/playwright-browsers}"
PLAYWRIGHT_CHROME=$(find "$PLAYWRIGHT_BROWSERS" -path '*chromium*/chrome-linux/chrome' -type f 2>/dev/null | head -1)
if [ -z "$PLAYWRIGHT_CHROME" ]; then
    PLAYWRIGHT_CHROME=$(find "$PLAYWRIGHT_BROWSERS" -path '*chromium*/chrome-linux/headless_shell' -type f 2>/dev/null | head -1)
fi
if [ -n "$PLAYWRIGHT_CHROME" ]; then
    mkdir -p /usr/local/bin
    ln -sf "$PLAYWRIGHT_CHROME" /usr/local/bin/chromium
    echo "✅ Linked Playwright Chromium to /usr/local/bin/chromium ($PLAYWRIGHT_CHROME)"
else
    echo "⚠️ Playwright Chromium not found; CHROME_BIN may need to be set at runtime"
fi
echo "✅ Playwright browser installation complete"

