FROM node:20-bookworm-slim

ENV NODE_ENV=production

# Preinstall weather MCP server to avoid runtime npx/npm installs.
RUN npm install -g --omit=dev @timlukahorstmann/mcp-weather \
    && npm cache clean --force
