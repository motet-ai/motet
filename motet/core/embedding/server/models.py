"""
Motet - Embedding Server Models

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Pydantic request and response models for the text embedding server.
    These models define the small HTTP/JSON contract used by workers when the
    embedding service runs as a sibling process.

Dependencies:
    - pydantic for request validation and OpenAPI schemas
    - typing for vector payload annotations

Usage:
    from motet.core.embedding.server.models import TextEmbedRequest

Notes:
    - The first implementation is text-only. Multimodal request models should
      be added here later without changing the text endpoint contract.
    - ``/healthz`` includes ``motet_version`` so ``GET /api/v1/version`` can
      compare this process to the API.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class TextEmbedRequest(BaseModel):
    """Request payload for embedding a single text string."""

    text: str = Field(..., description="Text to embed.")
    model: Optional[str] = Field(
        default=None,
        description="Optional embedding model identifier. Defaults to the server text model.",
    )


class TextEmbedResponse(BaseModel):
    """Response payload for a single text embedding."""

    embedding: List[float] = Field(..., description="Embedding vector.")
    model: str = Field(..., description="Model used to generate the embedding.")
    dimension: int = Field(..., description="Embedding vector dimension.")


class TextBatchEmbedRequest(BaseModel):
    """Request payload for embedding multiple texts."""

    texts: List[str] = Field(default_factory=list, description="Texts to embed.")
    model: Optional[str] = Field(
        default=None,
        description="Optional embedding model identifier. Defaults to the server text model.",
    )


class TextBatchEmbedResponse(BaseModel):
    """Response payload for a batch of text embeddings."""

    embeddings: List[List[float]] = Field(..., description="Embedding vectors in input order.")
    model: str = Field(..., description="Model used to generate the embeddings.")
    count: int = Field(..., description="Number of embeddings returned.")
    dimension: int = Field(..., description="Embedding vector dimension.")


class ModelInfoResponse(BaseModel):
    """Metadata for a loaded embedding model."""

    model: str = Field(..., description="Embedding model identifier.")
    dimension: int = Field(..., description="Embedding vector dimension.")
    modality: str = Field(default="text", description="Embedding modality.")


class ModelsResponse(BaseModel):
    """Available embedding models response."""

    models: List[ModelInfoResponse] = Field(..., description="Available embedding models.")
    default_model: str = Field(..., description="Default text embedding model.")


class HealthResponse(BaseModel):
    """Embedding server readiness response."""

    ready: bool = Field(..., description="Whether the text embedding model is ready.")
    model: str = Field(..., description="Default text model identifier.")
    dimension: int = Field(..., description="Default model embedding dimension.")
    motet_version: str = Field(
        ...,
        description="Motet product version of this embedding-server process",
    )
