FROM node:20-alpine

WORKDIR /app

# Install dependencies first (cached layer)
COPY motet/interfaces/frontend/apps/chat-explorer/package*.json ./
RUN npm ci

# Copy the shared package (for path alias resolution)
COPY motet/interfaces/frontend/packages /packages

# Copy source code (will be overridden by volume mount in dev)
COPY motet/interfaces/frontend/apps/chat-explorer .

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "--port", "5173"]
