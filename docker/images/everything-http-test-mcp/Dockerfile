FROM node:20-bookworm-slim

ENV NODE_ENV=production

# Preinstall MCP everything test server to avoid runtime npx/npm installs.
RUN npm install -g --omit=dev @modelcontextprotocol/server-everything \
    && npm cache clean --force
