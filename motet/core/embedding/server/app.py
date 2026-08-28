"""
Motet - Embedding Server Application

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    FastAPI application for the sibling embedding server. The first
    implementation exposes text embedding endpoints backed by the in-process
    sentence-transformers implementation and reserves the service boundary for
    future multimodal endpoints.

Dependencies:
    - FastAPI for HTTP request handling
    - motet.core.embedding.backends for the in-process text backend
    - motet.core.config for default embedding settings
    - structlog for structured logs

Usage:
    uvicorn motet.core.embedding.server.app:app --host 0.0.0.0 --port 8091

Notes:
    - Health checks load and probe the configured text model, so readiness only
      succeeds once text embeddings are available.
    - ``/healthz`` includes ``motet_version`` for stack version inspection.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

import structlog
from fastapi import FastAPI

from motet._version import get_version
from motet.core.config import Config
from motet.core.embedding.backends import InProcessEmbeddingService

from .models import (
    HealthResponse,
    ModelInfoResponse,
    ModelsResponse,
    TextBatchEmbedRequest,
    TextBatchEmbedResponse,
    TextEmbedRequest,
    TextEmbedResponse,
)

logger = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def get_embedding_backend() -> InProcessEmbeddingService:
    """Create and cache the server-local text embedding backend."""

    cfg = Config()
    default_model = getattr(cfg, "embedding_text_model", None) or cfg.embedding_model
    backend = InProcessEmbeddingService(default_model=default_model)
    # Force model load during readiness/server startup path.
    backend.get_embedding_dimension()
    logger.info("embedding_server_backend_ready", model=default_model)
    return backend


app = FastAPI(title="Motet Embedding Server", version=get_version())


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    """Return readiness for the text embedding backend."""

    backend = get_embedding_backend()
    return HealthResponse(
        ready=True,
        model=backend.default_model,
        dimension=backend.get_embedding_dimension(),
        motet_version=get_version(),
    )


@app.get("/model_info", response_model=ModelInfoResponse)
def model_info(model: Optional[str] = None) -> ModelInfoResponse:
    """Return metadata for an embedding model."""

    backend = get_embedding_backend()
    model_name = model or backend.default_model
    return ModelInfoResponse(
        model=model_name,
        dimension=backend.get_embedding_dimension(model=model_name),
        modality="text",
    )


@app.get("/embedding_dimension")
def embedding_dimension(model: Optional[str] = None) -> dict[str, int | str]:
    """Return the embedding dimension for compatibility callers."""

    info = model_info(model=model)
    return {"model": info.model, "dimension": info.dimension}


@app.get("/models", response_model=ModelsResponse)
def models() -> ModelsResponse:
    """Return models served by this embedding server instance."""

    backend = get_embedding_backend()
    default_info = model_info(model=backend.default_model)
    return ModelsResponse(models=[default_info], default_model=backend.default_model)


@app.post("/embed/text", response_model=TextEmbedResponse)
def embed_text(request: TextEmbedRequest) -> TextEmbedResponse:
    """Generate an embedding for one text string."""

    backend = get_embedding_backend()
    model_name = request.model or backend.default_model
    embedding = backend.embed(request.text, model=model_name)
    return TextEmbedResponse(
        embedding=embedding,
        model=model_name,
        dimension=len(embedding),
    )


@app.post("/embed/text/batch", response_model=TextBatchEmbedResponse)
def embed_text_batch(request: TextBatchEmbedRequest) -> TextBatchEmbedResponse:
    """Generate embeddings for multiple text strings."""

    backend = get_embedding_backend()
    model_name = request.model or backend.default_model
    embeddings = backend.embed_batch(request.texts, model=model_name)
    dimension = len(embeddings[0]) if embeddings else backend.get_embedding_dimension(model=model_name)
    return TextBatchEmbedResponse(
        embeddings=embeddings,
        model=model_name,
        count=len(embeddings),
        dimension=dimension,
    )


@app.post("/embed", response_model=TextEmbedResponse)
def embed_alias(request: TextEmbedRequest) -> TextEmbedResponse:
    """Compatibility alias for single text embedding."""

    return embed_text(request)


@app.post("/embed_batch", response_model=TextBatchEmbedResponse)
def embed_batch_alias(request: TextBatchEmbedRequest) -> TextBatchEmbedResponse:
    """Compatibility alias for batch text embedding."""

    return embed_text_batch(request)
