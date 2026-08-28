"""
Motet - Artifact RAG Types

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Defines retrieval-side models used by artifact RAG indexing and retrieval.
    Stored chunk fields now come from the PreparedArtifactChunk
    contract; this module adds retrieval scope and scoring wrappers.

Dependencies:
    - enum for retrieval scope values
    - pydantic for retrieval scoring fields
    - motet.core.artifacts.preparation for PreparedArtifactChunk

Usage:
    result = ArtifactChunkSearchResult(source_artifact_id="source", content_text="...", ...)

Notes:
    - `role` defaults to "user" until richer principal-role propagation is
      available in command context metadata.
"""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field

from ..artifacts.preparation import PreparedArtifactChunk


class ArtifactRetrievalScope(str, Enum):
    """Supported artifact RAG retrieval scopes."""

    CONVERSATION = "conversation"
    PRINCIPAL = "principal"
    MOTET = "motet"


class ArtifactChunkSearchResult(PreparedArtifactChunk):
    """A retrieved artifact chunk with vector, lexical, and hybrid scoring metadata."""

    vector_distance: float = Field(default=0.0, description="Backend-reported vector distance")
    similarity: float = Field(default=1.0, description="Normalized similarity score where higher is better")
    lexical_score: float = Field(default=0.0, description="Keyword/phrase match score where higher is better")
    hybrid_score: float = Field(default=0.0, description="Combined retrieval score where higher is better")


class ArtifactRagSelection(BaseModel):
    """Selected chunks and formatted context for injection into a model turn."""

    chunks: list[ArtifactChunkSearchResult] = Field(default_factory=list, description="Retrieved chunks")
    context_text: str = Field(default="", description="Citation-ready formatted context")
    token_budget: int = Field(default=0, description="Approximate token budget applied during selection")
