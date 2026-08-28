#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-noninteractive}

apt-get update
apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    build-essential \
    gcc \
    g++
rm -rf /var/lib/apt/lists/*

pip install --no-cache-dir -r /app/docker/images/scheduler/requirements.txt

