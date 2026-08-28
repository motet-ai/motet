"""
Motet - Artifacts Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Description:
    Core artifact storage and management.
    Includes artifact store protocol, Redis/S3-compatible implementations, and scoped store wrapper.
"""

from .types import ArtifactKind, ArtifactMetadata
from .protocol import ArtifactStoreProtocol
from .redis_artifact_store import RedisArtifactStore, get_artifact_store
from .s3_artifact_store import S3ArtifactStore
from .scoped_store import ScopedArtifactStore

__all__ = [
    "ArtifactKind",
    "ArtifactMetadata",
    "ArtifactStoreProtocol",
    "RedisArtifactStore",
    "S3ArtifactStore",
    "get_artifact_store",
    "ScopedArtifactStore",
]
