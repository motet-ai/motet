"""
Motet - Artifact Preparation Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Provides artifact preparation primitives: canonical models,
    deterministic strategy selection, execution, hashing helpers, and built-in
    text, JSON, and office-document strategies.

Dependencies:
    - pydantic preparation models
    - built-in strategy modules for core artifact families

Usage:
    from motet.core.artifacts.preparation import ArtifactPrepSelector, ArtifactPrepExecutor

Notes:
    - Retrieval and Valkey Search indexing remain in motet.core.rag and consume
      PreparedArtifactChunk records from this package.
"""

from __future__ import annotations

from .executor import ArtifactPrepExecutor
from .hashing import canonical_json_hash, chunk_cache_key, structured_content_hash, text_content_hash
from .models import (
    ArtifactFeatureMatch,
    ArtifactIndexState,
    ArtifactModality,
    ArtifactPayloadInfo,
    ArtifactPrepHints,
    ArtifactPrepManifest,
    ArtifactPrepPlan,
    ArtifactPrepResult,
    ArtifactPrepState,
    ArtifactPrepStep,
    ChunkCoordinate,
    ChunkKind,
    CodeCoord,
    DerivedSetStatus,
    JsonCoord,
    MediaCoord,
    PreparedArtifactChunk,
    PrepDecisionSource,
    TableCoord,
    TextCoord,
)
from .selector import ArtifactPrepSelection, ArtifactPrepSelector, manifest_matches
from .strategy import ArtifactPrepContext, ArtifactPrepStrategy

__all__ = [
    "ArtifactFeatureMatch",
    "ArtifactIndexState",
    "ArtifactModality",
    "ArtifactPayloadInfo",
    "ArtifactPrepContext",
    "ArtifactPrepExecutor",
    "ArtifactPrepHints",
    "ArtifactPrepManifest",
    "ArtifactPrepPlan",
    "ArtifactPrepResult",
    "ArtifactPrepSelection",
    "ArtifactPrepSelector",
    "ArtifactPrepState",
    "ArtifactPrepStep",
    "ArtifactPrepStrategy",
    "ChunkCoordinate",
    "ChunkKind",
    "CodeCoord",
    "DerivedSetStatus",
    "JsonCoord",
    "MediaCoord",
    "PrepDecisionSource",
    "PreparedArtifactChunk",
    "TableCoord",
    "TextCoord",
    "canonical_json_hash",
    "chunk_cache_key",
    "manifest_matches",
    "structured_content_hash",
    "text_content_hash",
]

