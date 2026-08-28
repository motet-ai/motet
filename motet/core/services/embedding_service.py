"""
Motet - Embedding Service Compatibility Module

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Re-exports the worker-facing embedding API from ``motet.core.embedding``.

Dependencies:
    - motet.core.embedding for the embedding facade, backends, and HTTP client

Usage:
    from motet.core.services.embedding_service import create_embedding_service

    service = create_embedding_service()
    embedding = service.embed("hello")

Notes:
    - Prefer importing from ``motet.core.embedding`` for new code.
    - Existing call sites can keep importing this module.
"""

from __future__ import annotations

from motet.core.embedding import (
    DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    DEFAULT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    EmbeddingBackend,
    EmbeddingServerCircuitOpenError,
    EmbeddingService,
    HttpEmbeddingServiceClient,
    InProcessEmbeddingService,
    create_embedding_service,
)

__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
    "DEFAULT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_RETRY_BACKOFF_SECONDS",
    "EmbeddingBackend",
    "EmbeddingServerCircuitOpenError",
    "EmbeddingService",
    "HttpEmbeddingServiceClient",
    "InProcessEmbeddingService",
    "create_embedding_service",
]
