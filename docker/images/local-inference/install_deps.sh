#!/usr/bin/env bash
# Motet — local-inference sibling image dependency install (ADR-0105).
#
# Mirrors docker/images/mcp-manager/install_deps.sh. Adds a C/C++ toolchain + cmake because
# llama-cpp-python may build from source when no prebuilt manylinux wheel matches the
# platform; on platforms with a matching wheel pip uses it and the toolchain is just unused.
set -euo pipefail

export DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-noninteractive}

apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    build-essential \
    cmake
rm -rf /var/lib/apt/lists/*

pip install --no-cache-dir -r /app/docker/images/local-inference/requirements.txt
