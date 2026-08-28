"""
Motet - OpenAI Encrypted Reasoning Replay Tests (ADR-0064 R10)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Unit tests for stateless reasoning replay in the OpenAI Responses adapter:
    store=false + include=["reasoning.encrypted_content"] request params, verbatim
    capture of output items into reasoning_blocks, validated verbatim replay in
    message rendering (with summary fallback), streaming ThinkingEvent.blocks
    propagation into model_stream state, and xAI non-inheritance of the policy.
    Also covers normalization of reasoning.effort onto OpenAI's accepted vocabulary.

Dependencies:
    - pytest: Test framework
    - motet.core.models.adapters.providers.openai_responses: adapter + helpers
    - motet.core.models.adapters.providers.xai_responses: xAI override contract
    - motet.core.types: canonical Message / ThinkingEvent

Usage:
    pytest tests/unit/core/providers/test_openai_encrypted_reasoning.py
"""

from __future__ import annotations

from typing import Any, Dict, List

from motet.core.models.adapters.providers.openai_responses import (
    OpenAIResponsesAdapter,
    _extract_reasoning_replay_items,
    _format_messages_for_openai,
    _resolve_openai_reasoning_effort,
    _valid_replay_items_for_message,
)
from motet.core.models.adapters.providers.xai_responses import XAIResponsesAdapter
from motet.core.types import LLMRequest, Message, ThinkingEvent


def test_openai_reasoning_effort_maps_supported_rungs() -> None:
    """Non-5.6 OpenAI models accept low|medium|high|xhigh; Motet max clamps to xhigh."""
    for rung in ("low", "medium", "high", "xhigh"):
        assert (
            _resolve_openai_reasoning_effort(
                {"model_name": "gpt-5.5", "reasoning_effort": rung}
            )
            == rung
        )
    assert (
        _resolve_openai_reasoning_effort(
            {"model_name": "gpt-5.5", "reasoning_effort": "max"}
        )
        == "xhigh"
    )


def test_openai_reasoning_effort_allows_max_on_gpt_56_responses() -> None:
    """gpt-5.6 Responses accepts Motet max; aliases/tiers must not clamp it away."""
    for model in ("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        assert (
            _resolve_openai_reasoning_effort(
                {"model_name": model, "reasoning_effort": "max"}
            )
            == "max"
        )
        assert (
            _resolve_openai_reasoning_effort(
                {"model_name": model, "reasoning_effort": "xhigh"}
            )
            == "xhigh"
        )


def test_openai_reasoning_effort_normalizes_unusable_values() -> None:
    """OpenAI 400s on off-list values, so overrides are normalized before they are sent."""
    assert _resolve_openai_reasoning_effort({}) == "medium"
    assert _resolve_openai_reasoning_effort({"reasoning_effort": "XHIGH"}) == "xhigh"
    assert _resolve_openai_reasoning_effort({"reasoning_effort": "banana"}) == "medium"
    assert _resolve_openai_reasoning_effort({"reasoning_effort": 7}) == "medium"
    # Without a gpt-5.6 model id, max still clamps (safe default for older models).
    assert _resolve_openai_reasoning_effort({"reasoning_effort": "max"}) == "xhigh"


def _reasoning_item(encrypted: bool = True) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "type": "reasoning",
        "id": "rs_abc",
        "summary": [{"type": "summary_text", "text": "thinking about it"}],
        "status": None,
    }
    if encrypted:
        item["encrypted_content"] = "gAAAAA-opaque"
    return item


def _function_call_item(call_id: str = "call_1") -> Dict[str, Any]:
    return {
        "type": "function_call",
        "id": "fc_abc",
        "call_id": call_id,
        "name": "get_weather",
        "arguments": '{"city": "Paris"}',
        "status": "completed",
    }


def _message_item(text: str = "Checking the weather.") -> Dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_abc",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


# ------------------------------------------------------------------------------------
# Request params: store=false + include
# ------------------------------------------------------------------------------------


def test_openai_finalize_sets_store_false_always() -> None:
    adapter = OpenAIResponsesAdapter(provider="openai", adapter_name="responses")
    params = adapter._finalize_responses_params({"model": "gpt-5.6-luna"}, LLMRequest(messages=[]))
    assert params["store"] is False
    assert "include" not in params


def test_openai_finalize_requests_encrypted_reasoning_when_reasoning_enabled() -> None:
    adapter = OpenAIResponsesAdapter(provider="openai", adapter_name="responses")
    params = adapter._finalize_responses_params(
        {"model": "gpt-5.6-luna", "reasoning": {"effort": "low", "summary": "auto"}},
        LLMRequest(messages=[]),
    )
    assert params["store"] is False
    assert params["include"] == ["reasoning.encrypted_content"]


def test_xai_finalize_does_not_inherit_store_or_include() -> None:
    adapter = XAIResponsesAdapter(
        provider="xai", adapter_name="responses", credentials={"xai_api_key": "k"}
    )
    params = adapter._finalize_responses_params({"model": "grok-4.5"}, LLMRequest(messages=[]))
    assert "store" not in params
    assert "include" not in params


# ------------------------------------------------------------------------------------
# Capture: _extract_reasoning_replay_items
# ------------------------------------------------------------------------------------


def test_extract_captures_items_and_strips_none_values() -> None:
    raw = {"output": [_reasoning_item(), _function_call_item()]}
    items = _extract_reasoning_replay_items(raw)
    assert items is not None
    assert [it["type"] for it in items] == ["reasoning", "function_call"]
    # None values (e.g. status: None on the reasoning item) must be stripped for replay
    assert "status" not in items[0]
    assert items[0]["encrypted_content"] == "gAAAAA-opaque"


def test_extract_returns_none_without_encrypted_content() -> None:
    raw = {"output": [_reasoning_item(encrypted=False), _message_item()]}
    assert _extract_reasoning_replay_items(raw) is None


def test_extract_skips_provider_executed_items() -> None:
    raw = {
        "output": [
            _reasoning_item(),
            {"type": "web_search_call", "id": "ws_1", "status": "completed"},
            _message_item(),
        ]
    }
    items = _extract_reasoning_replay_items(raw)
    assert items is not None
    assert [it["type"] for it in items] == ["reasoning", "message"]


# ------------------------------------------------------------------------------------
# Validation: _valid_replay_items_for_message
# ------------------------------------------------------------------------------------


def _assistant_with_blocks(
    blocks: List[Dict[str, Any]],
    *,
    content: str = "",
    tool_calls: Any = None,
) -> Message:
    return Message(
        role="assistant",
        content=content,
        tool_calls_canonical=tool_calls,
        reasoning_blocks=blocks,
    )


def test_valid_replay_happy_path_tool_call_turn() -> None:
    blocks = [_reasoning_item(), _function_call_item("call_1")]
    m = _assistant_with_blocks(
        blocks, tool_calls=[{"call_id": "call_1", "tool_name": "get_weather", "arguments_json": "{}"}]
    )
    assert _valid_replay_items_for_message(m) == blocks


def test_valid_replay_rejects_call_id_mismatch() -> None:
    # History sanitizers can prune tool calls; verbatim replay must not resurrect them.
    blocks = [_reasoning_item(), _function_call_item("call_1")]
    m = _assistant_with_blocks(blocks, tool_calls=[])
    assert _valid_replay_items_for_message(m) is None


def test_valid_replay_rejects_non_openai_shapes() -> None:
    # e.g. Anthropic-style thinking blocks stored by another provider mid-conversation
    m = _assistant_with_blocks([{"type": "thinking", "thinking": "...", "signature": "sig"}])
    assert _valid_replay_items_for_message(m) is None


def test_valid_replay_rejects_blocks_without_encrypted_content() -> None:
    m = _assistant_with_blocks([_reasoning_item(encrypted=False), _message_item()])
    assert _valid_replay_items_for_message(m) is None


def test_valid_replay_requires_message_item_when_content_nonempty() -> None:
    m = _assistant_with_blocks([_reasoning_item()], content="Some visible text")
    assert _valid_replay_items_for_message(m) is None


# ------------------------------------------------------------------------------------
# Rendering: verbatim replay vs summary fallback
# ------------------------------------------------------------------------------------


def test_format_messages_replays_verbatim_items() -> None:
    blocks = [_reasoning_item(), _function_call_item("call_1")]
    messages = [
        Message(role="user", content="What's the weather in Paris?"),
        _assistant_with_blocks(
            blocks, tool_calls=[{"call_id": "call_1", "tool_name": "get_weather", "arguments_json": "{}"}]
        ),
        Message(role="tool", content='{"temp": 21}', tool_call_id="call_1"),
    ]
    items = _format_messages_for_openai(
        messages=messages, model_name="gpt-5.6-luna", request_context=None
    )
    types = [it.get("type") or ("message" if "role" in it else "?") for it in items]
    assert types == ["message", "reasoning", "function_call", "function_call_output"]
    # Verbatim: ids and encrypted content preserved; no reconstructed duplicates
    assert items[1]["id"] == "rs_abc"
    assert items[1]["encrypted_content"] == "gAAAAA-opaque"
    assert items[2]["call_id"] == "call_1"
    assert items[3]["call_id"] == "call_1"


def test_format_messages_falls_back_to_summary_without_blocks() -> None:
    messages = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello", reasoning_content="thought about greeting"),
    ]
    items = _format_messages_for_openai(
        messages=messages, model_name="gpt-5.6-luna", request_context=None
    )
    reasoning_items = [it for it in items if it.get("type") == "reasoning"]
    assert len(reasoning_items) == 1
    assert reasoning_items[0]["summary"][0]["text"] == "thought about greeting"
    assert "encrypted_content" not in reasoning_items[0]


# ------------------------------------------------------------------------------------
# Streaming: ThinkingEvent.blocks propagation
# ------------------------------------------------------------------------------------


def test_thinking_event_carries_blocks() -> None:
    blocks = [_reasoning_item()]
    ev = ThinkingEvent(text="", is_complete=True, blocks=blocks)
    assert ev.blocks == blocks
    # Default remains None so existing adapters are unaffected
    assert ThinkingEvent(text="t").blocks is None


def test_stream_event_result_captures_blocks() -> None:
    from unittest.mock import Mock

    from motet.core.commands.builtin.model import StreamEventResult, _handle_stream_event

    state = StreamEventResult()
    motet = Mock()
    blocks = [_reasoning_item()]
    ev = ThinkingEvent(text="final reasoning", is_complete=True, blocks=blocks)
    out = _handle_stream_event(
        ev=ev,
        motet=motet,
        usage_data={},
        state=state,
        allow_citations=True,
        error_label="OpenAI",
        stream_key="test-stream",
    )
    assert out.reasoning_blocks == blocks
    assert out.reasoning_delta == "final reasoning"
