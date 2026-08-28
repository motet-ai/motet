# Motet - python-office image stack
#
# Copyright (c) 2024-2026 Motet Contributors
# Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
#
# Curated base for document/PDF skill scripts: LibreOffice (soffice), Poppler
# (pdftoppm), Ghostscript, pandoc, Node (docx npm), and common Python office wheels.
#
# Build (from repo root):
#   docker build -f docker/images/python-office/Dockerfile \
#     -t motet/python-office:dev docker/images/python-office
#
# Pin for Motet workers/API:
#   MOTET_IMAGE_STACK_PYTHON_OFFICE=motet/python-office:dev

FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="motet-python-office" \
      org.opencontainers.image.description="Motet python-office image stack" \
      org.opencontainers.image.source="https://github.com/motet-ai/motet-eval" \
      motet.image_stack="python-office" \
      motet.oss_notices="/usr/share/motet/NOTICES"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# LibreOffice headless + PDF/render tools + pandoc + Node for docx-js skills.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        default-jre-headless \
        fonts-dejavu-core \
        fonts-liberation \
        ghostscript \
        libreoffice-calc \
        libreoffice-common \
        libreoffice-impress \
        libreoffice-writer \
        nodejs \
        npm \
        pandoc \
        poppler-utils \
        unzip \
        zip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && mkdir -p /opt/motet/npm \
    && cd /opt/motet/npm \
    && npm init -y >/dev/null \
    && npm install docx \
    && npm cache clean --force

# Debian Node does not put global modules on the default require path; pin NODE_PATH.
ENV NODE_PATH=/opt/motet/npm/node_modules

# Distribution compliance: this image aggregates copyleft Debian packages
# (ghostscript AGPL-3.0, poppler-utils GPL-2.0, pandoc GPL-2.0+). NOTICES
# points at corresponding source (snapshot.debian.org); the manifests record
# the exact versions shipped.
COPY NOTICES /usr/share/motet/NOTICES
RUN dpkg-query -W -f='${Package} ${Version} ${Source}\n' | sort > /usr/share/motet/DEBIAN_PACKAGES.txt \
    && pip list --format=freeze > /usr/share/motet/PYTHON_PACKAGES.txt \
    && cd /opt/motet/npm \
    && npm ls --json > /usr/share/motet/NPM_PACKAGES.json

# Fail the build early if critical binaries are missing.
RUN soffice --version \
    && pdftoppm -v 2>&1 | head -n 1 \
    && gs --version \
    && pandoc --version | head -n 1 \
    && node -e "require('docx'); console.log('docx-npm-ok')" \
    && python -c "import pypdf, docx, openpyxl, pptx; print('python-office-wheels-ok')"

WORKDIR /work
