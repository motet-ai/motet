"""
Motet - Embedding Subsystem

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Embedding subsystem exports for the Motet runtime. This package owns the
    worker-facing embedding facade, local and remote embedding backends, and the
    sibling embedding server introduced by.

Dependencies:
    - motet.core.embedding.service for the stable worker-facing facade
    - motet.core.embedding.backends for in-process embedding generation
    - motet.core.embedding.client for sibling-server HTTP access

Usage:
    from motet.core.embedding import EmbeddingService, create_embedding_service

Notes:
    - `motet.core.services.embedding_service` remains as a compatibility import
      path for existing code.
"""

from __future__ import annotations

from .backends import DEFAULT_EMBEDDING_MODEL, EmbeddingBackend, InProcessEmbeddingService
from .client import (
    DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    DEFAULT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    EmbeddingServerCircuitOpenError,
    HttpEmbeddingServiceClient,
)
from .service import EmbeddingService, create_embedding_service

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
