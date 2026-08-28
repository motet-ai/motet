"""
Motet - Embedding Service Facade

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Worker-facing embedding service facade for the Motet runtime. It preserves
    the existing synchronous text embedding API while selecting either an
    in-process SentenceTransformer backend or the sibling embedding
    server HTTP client.

Dependencies:
    - os for topology and endpoint environment variables
    - motet.core.embedding.backends for the in-process backend and shared protocol
    - motet.core.embedding.client for the sibling-server HTTP client
    - structlog for structured lifecycle logging
    - typing for API annotations

Usage:
    from motet.core.embedding import create_embedding_service

    service = create_embedding_service()
    embedding = service.embed("hello")
    embeddings = service.embed_batch(["hello", "world"])

Notes:
    - `EmbeddingService.embed`, `embed_batch`, and `get_embedding_dimension`
      remain the stable worker-facing API.
    - `MOTET_EMBEDDING_TOPOLOGY=sibling` requires `MOTET_EMBEDDING_ENDPOINT`;
      missing endpoint is treated as misconfiguration and fails loudly.
"""

from __future__ import annotations

import os
from typing import List, Optional

import structlog

from .backends import DEFAULT_EMBEDDING_MODEL, EmbeddingBackend, InProcessEmbeddingService
from .client import (
    DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    DEFAULT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    HttpEmbeddingServiceClient,
)

logger = structlog.get_logger(__name__)


class EmbeddingService:
    """Stable facade that delegates to an in-process or HTTP embedding backend."""

    def __init__(
        self,
        default_model: str = DEFAULT_EMBEDDING_MODEL,
        *,
        topology: Optional[str] = None,
        endpoint: Optional[str] = None,
        request_timeout_seconds: Optional[float] = None,
        max_attempts: Optional[int] = None,
        retry_backoff_seconds: Optional[float] = None,
        circuit_breaker_failure_threshold: Optional[int] = None,
        circuit_breaker_recovery_timeout_seconds: Optional[float] = None,
    ) -> None:
        """
        Initialize the embedding service facade.

        Args:
            default_model: Default model to use for embeddings.
            topology: `in_process`, `sibling`, or `auto`.
            endpoint: Embedding server endpoint for sibling topology.
            request_timeout_seconds: HTTP request timeout for sibling topology.
            max_attempts: Maximum attempts for retryable sibling requests.
            retry_backoff_seconds: Base retry backoff in seconds.
            circuit_breaker_failure_threshold: Consecutive failed operations before opening the circuit.
            circuit_breaker_recovery_timeout_seconds: Seconds before an open circuit allows one probe request.
        """
        self.default_model = default_model
        resolved_topology = _resolve_topology(topology=topology, endpoint=endpoint)
        timeout = (
            float(request_timeout_seconds)
            if request_timeout_seconds is not None
            else _env_float("MOTET_EMBEDDING_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS)
        )
        attempts = (
            int(max_attempts)
            if max_attempts is not None
            else _env_int("MOTET_EMBEDDING_REQUEST_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
        )
        backoff = (
            float(retry_backoff_seconds)
            if retry_backoff_seconds is not None
            else _env_float("MOTET_EMBEDDING_RETRY_BACKOFF_SECONDS", DEFAULT_RETRY_BACKOFF_SECONDS)
        )
        failure_threshold = (
            int(circuit_breaker_failure_threshold)
            if circuit_breaker_failure_threshold is not None
            else _env_int(
                "MOTET_EMBEDDING_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
                DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            )
        )
        recovery_timeout = (
            float(circuit_breaker_recovery_timeout_seconds)
            if circuit_breaker_recovery_timeout_seconds is not None
            else _env_float(
                "MOTET_EMBEDDING_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS",
                DEFAULT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS,
            )
        )
        if resolved_topology == "sibling":
            resolved_endpoint = endpoint or os.getenv("MOTET_EMBEDDING_ENDPOINT", "")
            self._backend: EmbeddingBackend = HttpEmbeddingServiceClient(
                endpoint=resolved_endpoint,
                default_model=default_model,
                timeout_seconds=timeout,
                max_attempts=attempts,
                retry_backoff_seconds=backoff,
                circuit_breaker_failure_threshold=failure_threshold,
                circuit_breaker_recovery_timeout_seconds=recovery_timeout,
            )
        else:
            self._backend = InProcessEmbeddingService(default_model=default_model)

        self.topology = resolved_topology
        logger.info(
            "EmbeddingService initialized",
            topology=self.topology,
            default_model=default_model,
            endpoint=getattr(self._backend, "endpoint", None),
        )

    def embed(self, text: str, model: Optional[str] = None) -> List[float]:
        """Generate embedding for a single text."""

        return self._backend.embed(text, model=model)

    def embed_batch(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        """Generate embeddings for multiple texts in a batch."""

        return self._backend.embed_batch(texts, model=model)

    def get_embedding_dimension(self, model: Optional[str] = None) -> int:
        """Get the dimension of embeddings for a model."""

        return self._backend.get_embedding_dimension(model=model)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _resolve_topology(*, topology: Optional[str], endpoint: Optional[str]) -> str:
    raw_topology = (topology or os.getenv("MOTET_EMBEDDING_TOPOLOGY") or "auto").strip().lower()
    raw_endpoint = endpoint or os.getenv("MOTET_EMBEDDING_ENDPOINT", "")

    if raw_topology in {"in_process", "in-process", "local"}:
        return "in_process"
    if raw_topology == "sibling":
        if not raw_endpoint.strip():
            raise ValueError("MOTET_EMBEDDING_ENDPOINT is required when MOTET_EMBEDDING_TOPOLOGY=sibling")
        return "sibling"
    if raw_topology == "auto":
        return "sibling" if raw_endpoint.strip() else "in_process"
    raise ValueError("MOTET_EMBEDDING_TOPOLOGY must be one of: auto, in_process, sibling")


def create_embedding_service(
    default_model: str = DEFAULT_EMBEDDING_MODEL,
    *,
    topology: Optional[str] = None,
    endpoint: Optional[str] = None,
    request_timeout_seconds: Optional[float] = None,
    max_attempts: Optional[int] = None,
    retry_backoff_seconds: Optional[float] = None,
    circuit_breaker_failure_threshold: Optional[int] = None,
    circuit_breaker_recovery_timeout_seconds: Optional[float] = None,
) -> EmbeddingService:
    """
    Factory function to create the topology-aware embedding service facade.

    Args:
        default_model: Default model to use.
        topology: Optional topology override.
        endpoint: Optional sibling server endpoint override.
        request_timeout_seconds: Optional HTTP request timeout.
        max_attempts: Optional maximum attempts for retryable sibling requests.
        retry_backoff_seconds: Optional base retry backoff in seconds.
        circuit_breaker_failure_threshold: Optional failure threshold before opening the circuit.
        circuit_breaker_recovery_timeout_seconds: Optional recovery timeout for open circuits.

    Returns:
        Configured embedding service facade.
    """
    return EmbeddingService(
        default_model=default_model,
        topology=topology,
        endpoint=endpoint,
        request_timeout_seconds=request_timeout_seconds,
        max_attempts=max_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        circuit_breaker_failure_threshold=circuit_breaker_failure_threshold,
        circuit_breaker_recovery_timeout_seconds=circuit_breaker_recovery_timeout_seconds,
    )
