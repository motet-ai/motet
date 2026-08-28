FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_HEADLESS=true \
    PLAYWRIGHT_BROWSERS_PATH=/tmp/playwright-browsers \
    CHROME_BIN=/usr/bin/chromium \
    CHROME_PATH=/usr/bin/chromium \
    CHROME_FLAGS="--no-sandbox --disable-setuid-sandbox --disable-dev-shm-usage --disable-gpu --remote-debugging-port=9222" \
    NODE_ENV=production \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

COPY requirements.txt requirements-local-cpu.txt /app/
COPY docker/images/vault-init/requirements.txt /app/docker/images/vault-init/requirements.txt
COPY docker/images/vault-init/install_deps.sh /tmp/install_deps.sh
RUN bash /tmp/install_deps.sh && rm /tmp/install_deps.sh

COPY motet /app/motet
COPY motet-sdk /app/motet-sdk
COPY config /app/config
COPY docker /app/docker
COPY external /app/external
COPY scripts /app/scripts
COPY pyproject.toml /app/
RUN pip install --no-cache-dir -e /app/motet-sdk

CMD ["bash", "-c", "python docker/vault/vault_init.py --env-file .env"]

