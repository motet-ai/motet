"""
Motet - Artifact RAG Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Provides artifact retrieval over PreparedArtifactChunk records.
    The package stores embeddings in a tenant-scoped Valkey Search index and
    formats retrieved chunks for context injection.

Dependencies:
    - motet.core.distributed.redis_manager for Valkey/Redis-family access
    - motet.core.artifacts for source and derived artifact metadata
    - pydantic for structured retrieval and indexing models

Usage:
    from motet.core.rag import ArtifactChunkRepository, ArtifactRagRetriever

    repository = ArtifactChunkRepository()

Notes:
    - Preparation and chunking live in motet.core.artifacts.preparation.
    - Chunk search is fail-closed when required isolation fields are missing.
"""

from __future__ import annotations

from .repository import ArtifactChunkRepository
from .retriever import ArtifactRagRetriever
from .types import ArtifactChunkSearchResult, ArtifactRetrievalScope

__all__ = [
    "ArtifactChunkRepository",
    "ArtifactChunkSearchResult",
    "ArtifactRagRetriever",
    "ArtifactRetrievalScope",
]
