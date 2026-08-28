#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-noninteractive}

apt-get update
apt-get install -y --no-install-recommends \
    curl \
    gnupg2 \
    ca-certificates \
    build-essential \
    gcc \
    g++ \
    libpq-dev
rm -rf /var/lib/apt/lists/*

pip install --no-cache-dir -r /app/docker/images/migrator/requirements.txt

