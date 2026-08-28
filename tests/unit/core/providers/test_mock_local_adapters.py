"""
Motet - Mock/Local Adapter Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
Unit tests for the MockAdapter and LocalAdapter implementations to ensure
canonical request handling, streaming behavior, and content_parts flattening.

Dependencies:
- pytest: Test framework
- motet.core.types: Canonical request/response types
- motet.core.models.adapters.providers: Adapter implementations

Usage:
pytest tests/unit/core/providers/test_mock_local_adapters.py

Notes:
- Local adapter tests stub LocalInferenceClient to avoid Redis dependencies.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from motet.core.models.adapters.providers.local import LocalAdapter
from motet.core.models.adapters.providers.mock import MockAdapter
from motet.core.types import (
    LLMRequest,
    MediaPart,
    Message,
    OutputContract,
    StopEvent,
    StopReason,
    TextDeltaEvent,
    TextPart,
)


_GENUI_SCHEMA = {
    "type": "object",
    "properties": {"component": {"type": "string"}, "title": {"type": "string"}},
    "required": ["component", "title"],
}


class _StubLocalClient:
    def __init__(self) -> None:
        self.messages: List[dict[str, Any]] = []
        self.last_kwargs: dict[str, Any] = {}

    def infer_sync(self, *, model_id: str, messages: List[dict[str, Any]], temperature: float, max_tokens: int, **kwargs: Any) -> dict:
        self.messages = messages
        self.last_kwargs = kwargs
        return {"success": True, "text": "local ok", "elapsed_seconds": 0.01}

    def infer_stream(self, *, model: str, messages: List[dict[str, Any]], temperature: float, max_tokens: int, **kwargs: Any):
        self.messages = messages
        self.last_kwargs = kwargs
        for token in ["local", "stream"]:
            yield token


def test_mock_adapter_complete_echo() -> None:
    adapter = MockAdapter(provider="mock", adapter_name="mock")
    req = LLMRequest(messages=[Message(role="user", content="Hello")], model_settings={"model_name": "mock-small"})
    resp = adapter.complete(req)
    assert resp.output_text is not None
    assert "Hello" in resp.output_text
    assert resp.stop_reason == StopReason.NATURAL_STOP


def test_mock_adapter_stream_emits_tokens_and_stop() -> None:
    adapter = MockAdapter(provider="mock", adapter_name="mock")
    req = LLMRequest(messages=[Message(role="user", content="Hello")], model_settings={"model_name": "mock-small"})
    events = list(adapter.stream(req))
    assert any(isinstance(ev, TextDeltaEvent) for ev in events)
    assert any(isinstance(ev, StopEvent) for ev in events)


def test_local_adapter_flattens_content_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubLocalClient()
    monkeypatch.setattr(
        "motet.core.models.adapters.providers.local._get_client",
        lambda: stub,
    )

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[
            Message(
                role="user",
                content="fallback",
                content_parts=[TextPart(text="from parts")],
            )
        ],
        model_settings={"model_name": "local-1"},
    )
    resp = adapter.complete(req)
    assert resp.output_text == "local ok"
    assert stub.messages[0]["content"] == "from parts"


def test_local_adapter_summarizes_media_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubLocalClient()
    monkeypatch.setattr(
        "motet.core.models.adapters.providers.local._get_client",
        lambda: stub,
    )

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[
            Message(
                role="user",
                content="",
                content_parts=[
                    MediaPart(media_type="image", mime_type="image/png", base64_data="AA==")
                ],
            )
        ],
        model_settings={"model_name": "local-1"},
    )
    list(adapter.stream(req))
    assert stub.messages[0]["content"] == "[media]"


def test_local_adapter_forwards_json_schema_when_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0114: a JSON output_contract is mapped to json_schema for a structured-capable model."""
    stub = _StubLocalClient()
    monkeypatch.setattr(
        "motet.core.models.adapters.providers.local._get_client",
        lambda: stub,
    )

    adapter = LocalAdapter(provider="local", adapter_name="local")
    # phi-4-mini is registered with CAP_STRUCTURED_OUTPUT in the spec registry.
    req = LLMRequest(
        messages=[Message(role="user", content="render a card")],
        model_settings={"model_name": "phi-4-mini"},
        output_contract=OutputContract(format="json", json_schema=_GENUI_SCHEMA, strict=True),
    )
    adapter.complete(req)
    assert stub.last_kwargs.get("json_schema") == _GENUI_SCHEMA

    # Streaming path forwards the schema too.
    stub.last_kwargs = {}
    list(adapter.stream(req))
    assert stub.last_kwargs.get("json_schema") == _GENUI_SCHEMA


def test_local_adapter_degrades_without_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model without CAP_STRUCTURED_OUTPUT must not be forced into a grammar (ADR-0114 degradation)."""
    stub = _StubLocalClient()
    monkeypatch.setattr(
        "motet.core.models.adapters.providers.local._get_client",
        lambda: stub,
    )

    adapter = LocalAdapter(provider="local", adapter_name="local")
    # "local-1" has no spec -> no CAP_STRUCTURED_OUTPUT -> degrade to unconstrained.
    req = LLMRequest(
        messages=[Message(role="user", content="render a card")],
        model_settings={"model_name": "local-1"},
        output_contract=OutputContract(format="json", json_schema=_GENUI_SCHEMA, strict=True),
    )
    adapter.complete(req)
    assert "json_schema" not in stub.last_kwargs


def test_local_adapter_text_contract_is_not_constrained(monkeypatch: pytest.MonkeyPatch) -> None:
    """A text-format contract (or none) must not forward a schema."""
    stub = _StubLocalClient()
    monkeypatch.setattr(
        "motet.core.models.adapters.providers.local._get_client",
        lambda: stub,
    )

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "phi-4-mini"},
        output_contract=OutputContract(format="text"),
    )
    adapter.complete(req)
    assert "json_schema" not in stub.last_kwargs
