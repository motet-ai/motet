"""
Motet - Core Services

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Compatibility package that re-exports the embedding subsystem. Live
    memory and vector paths live under ``motet.core.memory`` and
    ``motet.core.embedding``.

Dependencies:
    - motet.core.embedding for EmbeddingService and create_embedding_service

Usage:
    from motet.core.services import EmbeddingService, create_embedding_service

    embedding_service = create_embedding_service()
    vector = embedding_service.embed("hello")

Notes:
    - Prefer importing from motet.core.embedding for new code.
    - embedding_service.py remains as a re-export for existing call sites.
    - Memory consolidation is a separate path from this package.
"""

from motet.core.embedding import EmbeddingService, create_embedding_service

__all__ = [
    "EmbeddingService",
    "create_embedding_service",
]
