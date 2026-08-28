FROM node:20-alpine

WORKDIR /app

# Install dependencies first (cached layer)
COPY motet/interfaces/frontend/apps/ops-dashboard/package.json ./
RUN npm install

# Copy the shared package (for path alias resolution)
COPY motet/interfaces/frontend/packages /packages

# Copy source code (will be overridden by volume mount in dev)
COPY motet/interfaces/frontend/apps/ops-dashboard .

EXPOSE 5174

CMD ["npm", "run", "dev", "--", "--host", "--port", "5174"]
