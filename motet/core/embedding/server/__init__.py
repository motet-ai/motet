"""
Motet - Embedding Server Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Runtime package for the sibling embedding server. It exposes a
    FastAPI app that workers can call over HTTP while preserving the existing
    in-process text embedding implementation for the server backend.

Dependencies:
    - motet.core.embedding.server.app for the FastAPI application

Usage:
    python -m motet.core.embedding.server --host 0.0.0.0 --port 8091

Notes:
    - Text embeddings are implemented first. Multimodal endpoints remain
      follow-on work for.
"""

from __future__ import annotations

from .app import app

__all__ = ["app"]
