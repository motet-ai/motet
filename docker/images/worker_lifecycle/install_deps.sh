#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-noninteractive}

apt-get update
apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    docker.io \
    docker-cli
rm -rf /var/lib/apt/lists/*

pip install --no-cache-dir -r /app/docker/images/worker_lifecycle/requirements.txt
