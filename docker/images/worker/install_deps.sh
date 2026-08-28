#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-noninteractive}

apt-get update
apt-get install -y --no-install-recommends \
    curl \
    gnupg2 \
    ca-certificates \
    wget \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    libgcc-s1 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libstdc++6 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    lsb-release \
    xdg-utils \
    xvfb \
    xauth \
    libgles2 \
    libegl1 \
    libxkbcommon0 \
    libdrm2 \
    libgbm1 \
    libxshmfence1 \
    ffmpeg
    # docker.io docker-cli  # Uncomment only if using Docker lifecycle backend (ADR-0067)
    # poppler-utils removed (ADR-0080: PDF rendering via pypdfium2 only)
rm -rf /var/lib/apt/lists/*

# Node.js is intentionally NOT installed in the worker image (ADR-0105).
# MCP servers run out-of-process: the `mcp-manager` sibling spawns each server in
# its own Docker sidecar image (see config/mcp_instance_manager.yaml exec_image, and
# MOTET_MCP_DOCKER_IMAGE=node:20-bookworm-slim for npx servers). Node-interpreter
# skills likewise run in containers via core.worker_exec (MOTET_EXEC_BACKEND=docker).
# Python Playwright (the in-worker http_get_browser tool) bundles its own Node driver,
# so it does not need system Node.
# Restore here ONLY if you run a worker with MOTET_MCP_EXEC_BACKEND=subprocess (no docker
# socket; e.g. CI stub) and need node-based MCP servers in-process.

# Chromium: installed by Playwright in configure_runtime.sh (single version, symlinked to /usr/local/bin/chromium)

# NOTE: the C/C++ build toolchain (build-essential/cmake/gcc/g++) was only needed to
# compile llama-cpp-python, which moved to the dedicated `local-inference` image
# (ADR-0105). All remaining worker deps install from prebuilt wheels (verified on
# linux/arm64 + linux/amd64 manylinux), so no compiler is required at build time.
pip install --no-cache-dir -r /app/docker/images/worker/requirements.txt
pip install --no-cache-dir --upgrade playwright

# ADR-0118: Whisper CLI for video transcript derivation (whisper_cli backend).
# Opt-in via build arg because openai-whisper pulls torch (~2 GB); the default
# worker image stays model-free per ADR-0105.
if [ "${INSTALL_WHISPER:-0}" = "1" ]; then
    pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu openai-whisper
fi

