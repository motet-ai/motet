FROM mcr.microsoft.com/playwright:v1.58.2-noble

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# Microsoft Playwright MCP (stdio). Headless + image-responses are set via
# CLI flags in start-playwright-mcp.sh (@playwright/mcp does not read env).

# Preinstall the MCP server so worker startup does not rely on runtime npx/npm.
# Binary: playwright-mcp (cli.js). Browsers come from the base image.
RUN npm install -g --omit=dev @playwright/mcp@0.0.78 \
    && npm cache clean --force

# Default context config: desktop Chrome UA + viewport (see config.json).
# start-playwright-mcp.sh passes --config unless the caller overrides it.
COPY docker/images/playwright-mcp/config.json /etc/playwright-mcp/config.json
COPY docker/images/playwright-mcp/start-playwright-mcp.sh /usr/local/bin/start-playwright-mcp.sh
RUN chmod +x /usr/local/bin/start-playwright-mcp.sh
