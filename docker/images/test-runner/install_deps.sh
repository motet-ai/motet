#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-noninteractive}

apt-get update
apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    tmux \
    ffmpeg
rm -rf /var/lib/apt/lists/*

mkdir -p /tmp/tmux-data /tmp/tmux-locks

pip install --no-cache-dir -r /app/docker/images/test-runner/requirements.txt
pip install --no-cache-dir uv
