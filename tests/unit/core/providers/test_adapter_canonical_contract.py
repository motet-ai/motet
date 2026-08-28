"""
Motet - Canonical Protocol Contract Tests (ADR-0064)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-23

Description:
    Contract tests that adapters emit only canonical ADR-0064 types:

    - MockAdapter complete/stream event grammar (thinking on/off)
    - Vision capability honesty: CAP_VISION models advertise vision; non-vision
      models do not
    - Format-path smoke: OpenAI / DeepSeek message formatters never emit provider
      wire tool dicts into ToolCallRequest-shaped structures

    Cloud complete()/stream() network paths remain covered by per-provider unit
    tests and gated live suites; this file is the shared CI contract.

Dependencies:
    - pytest
    - motet.core.models.adapters / types / specs
    - tests.unit.core.providers.canonical_contract

Usage:
    pytest tests/unit/core/providers/test_adapter_canonical_contract.py
"""

from __future__ import annotations

import pytest

from motet.core.models.adapters import adapter_registry
from motet.core.models.adapters.providers.deepseek_chat_completions import (
    _format_messages_for_deepseek,
)
from motet.core.models.adapters.providers.openai_chat_completions import (
    _format_messages_for_openai,
)
from motet.core.models.specs import CAP_REASONING, CAP_VISION, ModelSpec
from motet.core.types import (
    LLMRequest,
    MediaPart,
    Message,
    StopEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ToolCallRequest,
    UsageEvent,
)

from tests.fixtures.canonical_adapter_contract import (
    _dummy_credentials,
    assert_canonical_llm_response,
    assert_canonical_stream_events,
    collect_stream,
    iter_registry_cases,
)


_CASES = iter_registry_cases()


def _case_id(case: tuple[str, str, ModelSpec]) -> str:
    provider, model_name, _spec = case
    return f"{provider}/{model_name}"


@pytest.fixture
def mock_adapter():
    return adapter_registry.build(
        "mock",
        "mock",
        credentials=_dummy_credentials("mock"),
    )


def test_mock_complete_is_canonical(mock_adapter) -> None:
    resp = mock_adapter.complete(
        LLMRequest(
            messages=[Message(role="user", content="hello contract")],
            model_settings={"model_name": "mock-small"},
        )
    )
    assert_canonical_llm_response(resp)
    assert resp.output_text
    assert all(not isinstance(item, ToolCallRequest) or item.call_id for item in resp.output_items)


def test_mock_stream_event_grammar_without_thinking(mock_adapter) -> None:
    events = collect_stream(
        mock_adapter.stream(
            LLMRequest(
                messages=[Message(role="user", content="stream me")],
                model_settings={"model_name": "mock-small", "enable_thinking": False},
            )
        )
    )
    assert_canonical_stream_events(events, allow_thinking=False)
    assert any(isinstance(ev, TextDeltaEvent) for ev in events)
    assert any(isinstance(ev, UsageEvent) for ev in events)
    assert isinstance(events[-1], StopEvent)
    assert not any(isinstance(ev, ThinkingEvent) for ev in events)


def test_mock_stream_emits_thinking_when_enabled(mock_adapter) -> None:
    events = collect_stream(
        mock_adapter.stream(
            LLMRequest(
                messages=[Message(role="user", content="think then answer")],
                model_settings={"model_name": "mock-small", "enable_thinking": True},
            )
        )
    )
    assert_canonical_stream_events(events, allow_thinking=True)
    thinking = [ev for ev in events if isinstance(ev, ThinkingEvent)]
    assert thinking, "enable_thinking=True must emit ThinkingEvent"
    assert any(ev.is_complete for ev in thinking), "thinking stream must complete"


@pytest.mark.parametrize("case", _CASES, ids=[_case_id(c) for c in _CASES])
def test_vision_cap_matches_descriptor(case: tuple[str, str, ModelSpec]) -> None:
    provider, model_name, spec = case
    adapter = adapter_registry.build(
        provider,
        spec.default_adapter,
        credentials=_dummy_credentials(provider),
    )
    caps = adapter.capabilities(model=model_name)
    has_vision = CAP_VISION in spec.capabilities
    assert caps.supports_vision is has_vision


@pytest.mark.parametrize("case", _CASES, ids=[_case_id(c) for c in _CASES])
def test_reasoning_cap_matches_descriptor(case: tuple[str, str, ModelSpec]) -> None:
    provider, model_name, spec = case
    adapter = adapter_registry.build(
        provider,
        spec.default_adapter,
        credentials=_dummy_credentials(provider),
    )
    caps = adapter.capabilities(model=model_name)
    assert caps.supports_reasoning is (CAP_REASONING in spec.capabilities)


def test_deepseek_non_vision_flattens_or_omits_image_parts() -> None:
    """DeepSeek V4 is text-only: image MediaParts must not become image_url blocks."""
    messages = [
        Message(
            role="user",
            content="what is this?",
            content_parts=[
                MediaPart(
                    media_type="image",
                    mime_type="image/png",
                    base64_data="AAAA",
                ),
                # TextPart via content string when multimodal off
            ],
        )
    ]
    # Multimodal disabled / no request_context → formatter flattens to text content
    formatted = _format_messages_for_openai(
        messages,
        model_name="deepseek-v4-pro",
        request_context=None,
        provider="deepseek",
    )
    assert formatted
    content = formatted[0].get("content")
    # Must not be an OpenAI multimodal content array with image_url
    if isinstance(content, list):
        assert not any(
            isinstance(block, dict) and block.get("type") == "image_url" for block in content
        )


def test_deepseek_format_preserves_reasoning_not_wire_tool_shape() -> None:
    messages = [
        Message(
            role="assistant",
            content="",
            reasoning_content="plan",
            tool_calls_canonical=[
                {
                    "call_id": "call_1",
                    "tool_name": "lookup",
                    "arguments_json": "{}",
                }
            ],
        )
    ]
    formatted = _format_messages_for_deepseek(
        messages,
        model_name="deepseek-v4-flash",
        request_context=None,
    )
    assert formatted[0]["reasoning_content"] == "plan"
    assert formatted[0]["tool_calls"][0]["function"]["name"] == "lookup"
    # Wire format is OK inside provider-bound formatted messages; canonical
    # ToolCallRequest is what complete()/stream() must return to orchestration.
    assert "tool_name" not in formatted[0]["tool_calls"][0]


def test_canonical_stream_event_type_set_is_complete() -> None:
    """Guardrail: if ADR-0064 adds an event type, update the contract allowlist."""
    from motet.core.types import LLMStreamEvent
    from typing import get_args

    from tests.fixtures.canonical_adapter_contract import CANONICAL_STREAM_EVENT_TYPES

    # LLMStreamEvent is a Union of event models; each has a Literal type field.
    union_args = get_args(LLMStreamEvent)
    discovered = {getattr(cls, "model_fields")["type"].default for cls in union_args}
    assert discovered == CANONICAL_STREAM_EVENT_TYPES
