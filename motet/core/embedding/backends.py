"""
Motet - Embedding Backends

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-04

Description:
    Local embedding backend implementations for the Motet runtime. The initial
    backend preserves the existing synchronous SentenceTransformer text embedding
    behavior while the embedding subsystem grows a sibling-server topology.

Dependencies:
    - sentence-transformers for in-process text embedding generation
    - structlog for structured backend lifecycle and error logging
    - typing for API annotations

Usage:
    from motet.core.embedding.backends import InProcessEmbeddingService

    backend = InProcessEmbeddingService()
    vector = backend.embed("hello")

Notes:
    - This module intentionally keeps the in-process implementation separate
      from the facade and HTTP client so future multimodal backends can be added
      without expanding the worker-facing service class.
"""

from __future__ import annotations

from typing import Any, List, Optional, Protocol

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L12-v2"


class EmbeddingBackend(Protocol):
    """Protocol implemented by text embedding backends."""

    default_model: str

    def embed(self, text: str, model: Optional[str] = None) -> List[float]:
        """Generate one text embedding."""

    def embed_batch(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        """Generate text embeddings in input order."""

    def get_embedding_dimension(self, model: Optional[str] = None) -> int:
        """Return the embedding dimension for a model."""


class InProcessEmbeddingService:
    """Synchronous in-process embedding service using sentence-transformers."""

    def __init__(self, default_model: str = DEFAULT_EMBEDDING_MODEL):
        """
        Initialize the in-process embedding service.

        Args:
            default_model: Default model to use for embeddings.
        """
        self.default_model = default_model
        self._models: dict[str, Any] = {}
        logger.info("InProcessEmbeddingService initialized", default_model=default_model)

    def _get_model(self, model_name: str) -> Any:
        if model_name not in self._models:
            logger.info("Loading embedding model", model=model_name)
            from sentence_transformers import SentenceTransformer

            self._models[model_name] = SentenceTransformer(model_name)
            logger.info("Embedding model loaded successfully", model=model_name)
        return self._models[model_name]

    def embed(self, text: str, model: Optional[str] = None) -> List[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Text to embed.
            model: Model to use. Defaults to the configured default model.

        Returns:
            List of floats representing the embedding vector.
        """
        model_name = model or self.default_model
        try:
            model_instance = self._get_model(model_name)
            embedding = model_instance.encode(text, convert_to_tensor=False)
            if hasattr(embedding, "tolist"):
                embedding = embedding.tolist()
            embedding_list = [float(value) for value in embedding]
            logger.debug(
                "Generated embedding",
                text_length=len(text),
                embedding_dim=len(embedding_list),
                model=model_name,
            )
            return embedding_list
        except Exception as e:
            logger.error(
                "Failed to generate embedding",
                error=str(e),
                error_type=type(e).__name__,
                model=model_name,
                exc_info=True,
            )
            raise RuntimeError(f"Embedding generation failed: {e}") from e

    def embed_batch(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        """
        Generate embeddings for multiple texts in a batch.

        Args:
            texts: List of texts to embed.
            model: Model to use. Defaults to the configured default model.

        Returns:
            List of embedding vectors, one per input text.
        """
        model_name = model or self.default_model
        if not texts:
            return []
        try:
            model_instance = self._get_model(model_name)
            embeddings = model_instance.encode(texts, convert_to_tensor=False, show_progress_bar=False)
            if hasattr(embeddings, "tolist"):
                embeddings = embeddings.tolist()
            embedding_lists = [[float(value) for value in embedding] for embedding in embeddings]
            logger.debug(
                "Generated batch embeddings",
                batch_size=len(texts),
                embedding_dim=len(embedding_lists[0]) if embedding_lists else 0,
                model=model_name,
            )
            return embedding_lists
        except Exception as e:
            logger.error(
                "Failed to generate batch embeddings",
                error=str(e),
                error_type=type(e).__name__,
                model=model_name,
                batch_size=len(texts),
                exc_info=True,
            )
            raise RuntimeError(f"Batch embedding generation failed: {e}") from e

    def get_embedding_dimension(self, model: Optional[str] = None) -> int:
        """
        Get the embedding dimension for a model.

        Args:
            model: Model to check. Defaults to the configured default model.

        Returns:
            Embedding dimension.
        """
        model_name = model or self.default_model
        return int(self._get_model(model_name).get_sentence_embedding_dimension())
