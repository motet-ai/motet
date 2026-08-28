"""
Motet - Gemini Adapter ModelStream Command Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Description:
    Unit tests for verifying that `model_stream` uses the Gemini adapter path
    without making real network calls.

    Stubs mirror `test_model_stream_anthropic_adapter.py` (ADR-0064 routing + registry.build).

Usage:
    pytest tests/unit/core/orchestration/test_model_stream_gemini_adapter.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional
from unittest.mock import patch


@dataclass
class _DummyRedisClient:
    def close(self) -> None:
        return None


@dataclass
class _FakeAgent:
    pass


@dataclass
class _FakeMotet:
    agent: Any = field(default_factory=_FakeAgent)
    vault: Any = None
    stream_key: str = "task:test:response"
    task_id: str = "task-test"
    command_id: str = "cmd-test"
    tenant_id: str = "tenant-test"
    principal_id: str = "principal-test"
    motet_id: str = "default"

    def stream_token(self, _token: str, stream_key: Optional[str] = None) -> None:
        return None

    def flush_token_buffer(self, stream_key: Optional[str] = None) -> None:
        return None

    def stream_event(self, _event_type: str, **_kwargs: Any) -> None:
        return None


class _FakeGeminiAdapter:
    def stream(self, _req: Any) -> Iterator[Any]:
        from motet.core.types import StopEvent, StopReason, TextDeltaEvent

        yield TextDeltaEvent(text="Hello from Gemini.")
        yield StopEvent(reason=StopReason.NATURAL_STOP)


def test_model_stream_uses_gemini_adapter_without_network_calls() -> None:
    from motet.core.models.adapters.routing import AdapterSelection
    from motet.core.commands.command_data_classes import ModelStreamData
    from motet.core.commands.builtin.model import model_stream
    from motet.core.types import Message

    fake_motet = _FakeMotet()
    data = ModelStreamData(
        messages=[Message(role="user", content="hi")],
        stream_key=fake_motet.stream_key,
        model_settings={
            "provider": "gemini",
            "model_name": "gemini-2.5-flash",
            "temperature": 0.2,
            "max_tokens": 50,
        },
    )

    selection = AdapterSelection(
        provider="gemini",
        model_name="gemini-2.5-flash",
        adapter_name="generate_content",
        source="request_override",
    )
    effective_model_settings: Dict[str, Any] = dict(data.model_settings or {})

    with patch(
        "motet.core.commands.builtin.model.get_motet_context",
        return_value=fake_motet,
    ), patch(
        "motet.core.distributed.redis_manager.get_sync_redis_client",
        return_value=_DummyRedisClient(),
    ), patch(
        "motet.core.commands.builtin.model._select_adapter_and_effective_model_settings",
        return_value=(selection, effective_model_settings, None, None),
    ), patch(
        "motet.core.models.adapters.adapter_registry.build",
        return_value=_FakeGeminiAdapter(),
    ) as build_mock:
        out = model_stream.__wrapped__(data)  # type: ignore[attr-defined]

    assert out["inference_backend"] == "adapter"
    assert out["provider"] == "gemini"
    assert out["model_name"] == "gemini-2.5-flash"
    assert out["adapter"] == "gemini:generate_content"
    assert out["adapter_selection_source"] == "request_override"
    assert out["finish_reason"] == "stop"
    assert out["tokens_streamed"] >= 1
    assert "Hello from Gemini." in (out["final_content"] or "")

    build_mock.assert_called()
