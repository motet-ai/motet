"""
Motet - Embedding Subsystem Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Unit tests for the ADR-0107 embedding subsystem package layout, topology
    selection, compatibility exports, and server route behavior.

Dependencies:
    - pytest for test execution and monkeypatch fixtures
    - motet.core.embedding for the embedding facade and server functions

Usage:
    pytest tests/unit/core/test_embedding_subsystem.py

Notes:
    - Tests avoid loading real Hugging Face models by inspecting lazy backends
      and monkeypatching the embedding server backend.
"""

from __future__ import annotations

import importlib

import pytest

from motet._version import get_version
from motet.core import embedding
from motet.core.embedding import server
from motet.core.embedding import client as embedding_client
from motet.core.embedding.backends import InProcessEmbeddingService
from motet.core.embedding.client import EmbeddingServerCircuitOpenError, HttpEmbeddingServiceClient
from motet.core.embedding.server.models import TextBatchEmbedRequest, TextEmbedRequest
from motet.core.memory.pgvector_store import PGVectorStore
from motet.core.services import embedding_service as compatibility_module

server_app = importlib.import_module("motet.core.embedding.server.app")


class _FakeEmbeddingBackend:
    default_model = "test-model"

    def embed(self, text: str, model: str | None = None) -> list[float]:
        return [float(len(text)), 1.0]

    def embed_batch(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        return [self.embed(text, model=model) for text in texts]

    def get_embedding_dimension(self, model: str | None = None) -> int:
        return 2


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


class _FakeHttpClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, path: str, **kwargs) -> _FakeResponse:
        self.calls.append(("post", path, kwargs))
        return self._next_outcome()

    def get(self, path: str, **kwargs) -> _FakeResponse:
        self.calls.append(("get", path, kwargs))
        return self._next_outcome()

    def _next_outcome(self) -> _FakeResponse:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_compatibility_module_reexports_embedding_api() -> None:
    """The old services import path should keep pointing at the new subsystem."""

    assert compatibility_module.EmbeddingService is embedding.EmbeddingService
    assert compatibility_module.create_embedding_service is embedding.create_embedding_service
    assert compatibility_module.InProcessEmbeddingService is InProcessEmbeddingService


def test_embedding_service_uses_in_process_backend_without_endpoint(monkeypatch) -> None:
    """Auto topology should remain in-process for standalone/dev deployments."""

    monkeypatch.delenv("MOTET_EMBEDDING_ENDPOINT", raising=False)
    monkeypatch.setenv("MOTET_EMBEDDING_TOPOLOGY", "auto")

    service = embedding.create_embedding_service(default_model="test-model")

    assert service.topology == "in_process"
    assert isinstance(service._backend, InProcessEmbeddingService)


def test_embedding_service_uses_sibling_backend_with_endpoint(monkeypatch) -> None:
    """Auto topology should use the HTTP client when an endpoint is configured."""

    monkeypatch.setenv("MOTET_EMBEDDING_ENDPOINT", "http://127.0.0.1:8091")
    monkeypatch.setenv("MOTET_EMBEDDING_TOPOLOGY", "auto")

    service = embedding.create_embedding_service(default_model="test-model")

    assert service.topology == "sibling"
    assert isinstance(service._backend, HttpEmbeddingServiceClient)
    assert service._backend.endpoint == "http://127.0.0.1:8091"


def test_embedding_service_passes_retry_configuration_to_http_backend(monkeypatch) -> None:
    """Retry and circuit settings should flow from environment into the HTTP backend."""

    monkeypatch.setenv("MOTET_EMBEDDING_ENDPOINT", "http://127.0.0.1:8091")
    monkeypatch.setenv("MOTET_EMBEDDING_TOPOLOGY", "auto")
    monkeypatch.setenv("MOTET_EMBEDDING_REQUEST_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("MOTET_EMBEDDING_RETRY_BACKOFF_SECONDS", "0.75")
    monkeypatch.setenv("MOTET_EMBEDDING_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "7")
    monkeypatch.setenv("MOTET_EMBEDDING_CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS", "11")

    service = embedding.create_embedding_service(default_model="test-model")
    backend = service._backend

    assert isinstance(backend, HttpEmbeddingServiceClient)
    assert backend.max_attempts == 5
    assert backend.retry_backoff_seconds == 0.75
    assert backend.circuit_breaker_failure_threshold == 7
    assert backend.circuit_breaker_recovery_timeout_seconds == 11.0


def test_http_embedding_client_retries_retryable_failures(monkeypatch) -> None:
    """Transient request failures should retry before surfacing to callers."""

    monkeypatch.setattr(embedding_client, "_embedding_client_sleep", lambda seconds: None)
    fake_http = _FakeHttpClient(
        [
            RuntimeError("temporary outage"),
            _FakeResponse({"embedding": [1.0, 2.0], "model": "test-model", "dimension": 2}),
        ]
    )
    client = HttpEmbeddingServiceClient(
        endpoint="http://embedding-server:8091",
        default_model="test-model",
        max_attempts=2,
        retry_backoff_seconds=0.0,
    )
    client._client = fake_http

    assert client.embed("hello") == [1.0, 2.0]
    assert len(fake_http.calls) == 2
    assert client._failure_count == 0
    assert client._circuit_state == "closed"


def test_http_embedding_client_get_requests_do_not_send_json_body() -> None:
    """GET metadata calls should not pass unsupported JSON kwargs to httpx."""

    fake_http = _FakeHttpClient(
        [
            _FakeResponse(
                {
                    "model": "test-model",
                    "dimension": 2,
                    "modality": "text",
                    "status": "ready",
                }
            ),
        ]
    )
    client = HttpEmbeddingServiceClient(
        endpoint="http://embedding-server:8091",
        default_model="test-model",
    )
    client._client = fake_http

    assert client.get_embedding_dimension() == 2
    assert fake_http.calls == [
        ("get", "/model_info", {"params": {"model": "test-model"}})
    ]


def test_http_embedding_client_opens_circuit_after_failures(monkeypatch) -> None:
    """Repeated final request failures should open the local circuit."""

    monkeypatch.setattr(embedding_client, "_embedding_client_sleep", lambda seconds: None)
    fake_http = _FakeHttpClient(
        [
            RuntimeError("outage 1"),
            RuntimeError("outage 2"),
        ]
    )
    client = HttpEmbeddingServiceClient(
        endpoint="http://embedding-server:8091",
        default_model="test-model",
        max_attempts=1,
        circuit_breaker_failure_threshold=2,
        circuit_breaker_recovery_timeout_seconds=60.0,
    )
    client._client = fake_http

    with pytest.raises(RuntimeError, match="outage 1"):
        client.embed("hello")
    with pytest.raises(RuntimeError, match="outage 2"):
        client.embed("hello again")

    assert client._circuit_state == "open"
    assert len(fake_http.calls) == 2

    with pytest.raises(EmbeddingServerCircuitOpenError):
        client.embed("blocked")
    assert len(fake_http.calls) == 2


def test_pgvector_store_uses_injected_embedding_function() -> None:
    """PGVectorStore should not require a local model when embedding_fn is provided."""

    calls = []

    def embed(text: str) -> list[float]:
        calls.append(text)
        return [1.0, 2.0, 3.0]

    store = PGVectorStore.__new__(PGVectorStore)
    store._embedding_fn = embed
    store._embedder = None
    store._init_cache(enable_embedding_cache=True, enable_result_cache=False)

    assert store._embed_text("hello") == [1.0, 2.0, 3.0]
    assert store._embed_text("hello") == [1.0, 2.0, 3.0]
    assert calls == ["hello"]


def test_embedding_server_routes_use_cached_backend(monkeypatch) -> None:
    """Server route functions should expose text embedding and model metadata."""

    fake_backend = _FakeEmbeddingBackend()
    monkeypatch.setattr(server_app, "get_embedding_backend", lambda: fake_backend)

    assert server.app is server_app.app

    health = server_app.healthz()
    assert health.ready is True
    assert health.model == "test-model"
    assert health.dimension == 2
    assert health.motet_version == get_version()

    model_info = server_app.model_info()
    assert model_info.model == "test-model"
    assert model_info.dimension == 2
    assert model_info.modality == "text"

    dimension = server_app.embedding_dimension()
    assert dimension == {"model": "test-model", "dimension": 2}

    models = server_app.models()
    assert models.default_model == "test-model"
    assert len(models.models) == 1

    single = server_app.embed_text(TextEmbedRequest(text="abc"))
    assert single.embedding == [3.0, 1.0]
    assert single.dimension == 2

    batch = server_app.embed_text_batch(TextBatchEmbedRequest(texts=["a", "abcd"]))
    assert batch.embeddings == [[1.0, 1.0], [4.0, 1.0]]
    assert batch.count == 2
