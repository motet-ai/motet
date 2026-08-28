#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-noninteractive}

apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates \
    curl
rm -rf /var/lib/apt/lists/*

pip install --no-cache-dir -r /app/docker/images/mcp-manager/requirements.txt
