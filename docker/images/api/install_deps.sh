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
    wget \
    libpq-dev \
    libffi-dev \
    libssl-dev \
    pkg-config \
    git
rm -rf /var/lib/apt/lists/*

pip install --no-cache-dir -r /app/docker/images/api/requirements.txt
pip install --no-cache-dir uv