"""
Motet - Embedding HTTP Client

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    HTTP client backend for the sibling embedding server. It preserves
    the same synchronous text embedding API as the in-process backend while
    sending requests to the server-local FastAPI process.

Dependencies:
    - httpx for connection-pooled HTTP requests to the embedding server
    - motet.core.resilience.retry for exponential retry backoff
    - time for fallback retry sleeps outside worker images
    - structlog for request failure logging
    - typing for API annotations

Usage:
    from motet.core.embedding.client import HttpEmbeddingServiceClient

    client = HttpEmbeddingServiceClient(endpoint="http://127.0.0.1:8091")
    vector = client.embed("hello")

Notes:
    - The client fails loudly on transport and server errors. Callers that need
      degraded retrieval behavior should catch errors at the command/service
      boundary where fallback semantics are explicit.
"""

from __future__ import annotations

from time import monotonic, sleep
from typing import Any, List, Optional

import structlog

from motet.core.resilience.retry import exponential_backoff

from .backends import DEFAULT_EMBEDDING_MODEL

logger = structlog.get_logger(__name__)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.25
DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 3
DEFAULT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS = 30.0


def _embedding_client_sleep(seconds: float) -> None:
    """Sleep between retries without importing worker-only dependencies eagerly."""

    try:
        from motet.core.workers.concurrency_primitives import worker_sleep

        worker_sleep(seconds)
    except ImportError:
        sleep(seconds)


class EmbeddingServerCircuitOpenError(RuntimeError):
    """Raised when the embedding server circuit breaker is open."""


class HttpEmbeddingServiceClient:
    """HTTP client for the ADR-0107 sibling embedding server."""

    def __init__(
        self,
        *,
        endpoint: str,
        default_model: str = DEFAULT_EMBEDDING_MODEL,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        circuit_breaker_failure_threshold: int = DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        circuit_breaker_recovery_timeout_seconds: float = DEFAULT_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS,
    ) -> None:
        """
        Initialize the HTTP embedding client.

        Args:
            endpoint: Base URL for the embedding server.
            default_model: Default embedding model to request.
            timeout_seconds: Per-request timeout.
            max_attempts: Maximum attempts for retryable request failures.
            retry_backoff_seconds: Base retry backoff in seconds.
            circuit_breaker_failure_threshold: Consecutive failed operations before opening the circuit.
            circuit_breaker_recovery_timeout_seconds: Seconds before an open circuit allows one probe request.
        """
        if not endpoint or not endpoint.strip():
            raise ValueError("Embedding endpoint is required for sibling topology")
        self.endpoint = endpoint.rstrip("/")
        self.default_model = default_model
        self.timeout_seconds = float(timeout_seconds)
        self.max_attempts = max(1, int(max_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.circuit_breaker_failure_threshold = max(1, int(circuit_breaker_failure_threshold))
        self.circuit_breaker_recovery_timeout_seconds = max(0.0, float(circuit_breaker_recovery_timeout_seconds))
        self._circuit_state = "closed"
        self._failure_count = 0
        self._opened_at: Optional[float] = None
        self._client = None
        logger.info(
            "HttpEmbeddingServiceClient initialized",
            endpoint=self.endpoint,
            default_model=default_model,
            timeout_seconds=self.timeout_seconds,
            max_attempts=self.max_attempts,
            retry_backoff_seconds=self.retry_backoff_seconds,
            circuit_breaker_failure_threshold=self.circuit_breaker_failure_threshold,
            circuit_breaker_recovery_timeout_seconds=self.circuit_breaker_recovery_timeout_seconds,
        )

    def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(base_url=self.endpoint, timeout=self.timeout_seconds)
        return self._client

    def embed(self, text: str, model: Optional[str] = None) -> List[float]:
        """Generate embedding for one text through the sibling server."""

        payload = {"text": text, "model": model or self.default_model}
        data = self._request_json(
            "post",
            "/embed/text",
            operation="embed_text",
            json=payload,
            log_context={"model": payload["model"], "text_length": len(text)},
        )
        return [float(value) for value in data["embedding"]]

    def embed_batch(self, texts: List[str], model: Optional[str] = None) -> List[List[float]]:
        """Generate embeddings for multiple texts through the sibling server."""

        if not texts:
            return []
        payload = {"texts": texts, "model": model or self.default_model}
        data = self._request_json(
            "post",
            "/embed/text/batch",
            operation="embed_text_batch",
            json=payload,
            log_context={"model": payload["model"], "batch_size": len(texts)},
        )
        return [[float(value) for value in embedding] for embedding in data["embeddings"]]

    def get_embedding_dimension(self, model: Optional[str] = None) -> int:
        """Return embedding dimension from sibling server metadata."""

        model_name = model or self.default_model
        data = self._request_json(
            "get",
            "/model_info",
            operation="model_info",
            params={"model": model_name},
            log_context={"model": model_name},
        )
        return int(data["dimension"])

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        log_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Execute a JSON request with retries and circuit-breaker accounting."""

        self._ensure_circuit_allows_request(operation=operation)
        context = log_context or {}
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                request_kwargs: dict[str, Any] = {"params": params}
                if json is not None:
                    request_kwargs["json"] = json
                response = getattr(self._get_client(), method)(path, **request_kwargs)
                response.raise_for_status()
                data = response.json()
                self._record_success(operation=operation)
                return data
            except Exception as e:
                last_error = e
                retryable = self._is_retryable_error(e)
                if not retryable or attempt >= self.max_attempts:
                    self._record_failure(operation=operation, error=e)
                    logger.error(
                        "Embedding server request failed",
                        endpoint=self.endpoint,
                        operation=operation,
                        path=path,
                        attempt=attempt,
                        max_attempts=self.max_attempts,
                        retryable=retryable,
                        circuit_state=self._circuit_state,
                        failure_count=self._failure_count,
                        error=str(e),
                        error_type=type(e).__name__,
                        exc_info=True,
                        **context,
                    )
                    raise RuntimeError(f"Embedding server {operation} request failed: {e}") from e

                delay = exponential_backoff(attempt, base=self.retry_backoff_seconds, cap=2.0, jitter=0.05)
                logger.warning(
                    "Embedding server request failed, retrying",
                    endpoint=self.endpoint,
                    operation=operation,
                    path=path,
                    attempt=attempt,
                    max_attempts=self.max_attempts,
                    retry_delay_seconds=delay,
                    error=str(e),
                    error_type=type(e).__name__,
                    **context,
                )
                _embedding_client_sleep(delay)

        raise RuntimeError(f"Embedding server {operation} request failed: {last_error}") from last_error

    def _ensure_circuit_allows_request(self, *, operation: str) -> None:
        """Raise if the local circuit breaker should block the request."""

        if self._circuit_state != "open":
            return

        elapsed = monotonic() - (self._opened_at or 0.0)
        if elapsed >= self.circuit_breaker_recovery_timeout_seconds:
            self._circuit_state = "half_open"
            logger.warning(
                "Embedding server circuit entering half-open state",
                endpoint=self.endpoint,
                operation=operation,
                recovery_timeout_seconds=self.circuit_breaker_recovery_timeout_seconds,
            )
            return

        raise EmbeddingServerCircuitOpenError(
            "Embedding server circuit is open "
            f"(endpoint={self.endpoint}, retry_after_seconds="
            f"{self.circuit_breaker_recovery_timeout_seconds - elapsed:.2f})"
        )

    def _record_success(self, *, operation: str) -> None:
        """Close the circuit and clear failure counters after a successful request."""

        previous_state = self._circuit_state
        self._circuit_state = "closed"
        self._failure_count = 0
        self._opened_at = None
        if previous_state != "closed":
            logger.info(
                "Embedding server circuit closed",
                endpoint=self.endpoint,
                operation=operation,
                previous_state=previous_state,
            )

    def _record_failure(self, *, operation: str, error: Exception) -> None:
        """Record a failed operation and open the circuit when the threshold is reached."""

        self._failure_count += 1
        if self._failure_count < self.circuit_breaker_failure_threshold:
            return

        previous_state = self._circuit_state
        self._circuit_state = "open"
        self._opened_at = monotonic()
        logger.error(
            "Embedding server circuit opened",
            endpoint=self.endpoint,
            operation=operation,
            previous_state=previous_state,
            failure_count=self._failure_count,
            failure_threshold=self.circuit_breaker_failure_threshold,
            error=str(error),
            error_type=type(error).__name__,
        )

    def _is_retryable_error(self, error: Exception) -> bool:
        """Return whether a request error should be retried."""

        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code is None:
            return True
        if status_code == 429:
            return True
        return not (400 <= int(status_code) < 500)
