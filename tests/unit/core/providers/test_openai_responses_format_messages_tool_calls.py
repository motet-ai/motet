"""
Motet - OpenAI Responses Adapter Formatting Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Unit tests for the OpenAI Responses adapter message formatting.

    Validates that ``tool_calls_canonical`` on Message is translated into
    Responses-native `input` items. Names are passed through; ``model.py``
    applies wire format before the adapter (ADR-0137 / #225).

Dependencies:
    - pytest
    - motet.core.models.adapters.providers.openai_responses._format_messages_for_openai
    - motet.core.types.Message

Usage:
    pytest tests/unit/core/providers/test_openai_responses_format_messages_tool_calls.py
"""

from __future__ import annotations

from typing import Any


def test_openai_responses_format_messages_translates_standardized_tool_calls_to_function_call_items() -> None:
    from motet.core.models.adapters.providers.openai_responses import _format_messages_for_openai
    from motet.core.types import Message, RequestContext

    messages = [
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[
                {
                    "call_id": "call_123",
                    "tool_name": "mcp.google_workspace.list_docs_in_folder",
                    "arguments_json": '{"folder_id": "root", "page_size": 10}',
                    "arguments": {"folder_id": "root", "page_size": 10},
                }
            ],
        ),
        Message(role="tool", content="OK", tool_call_id="call_123"),
    ]

    out_items = _format_messages_for_openai(
        messages=messages,
        model_name="gpt-4.1-mini",
        request_context=RequestContext(enable_multimodal=False),
    )

    # Expect:
    # - assistant message object (role/content)
    # - function_call item derived from tool_calls (wire name + JSON string args)
    # - function_call_output item derived from role="tool"
    assert out_items[0]["role"] == "assistant"
    assert out_items[1]["type"] == "function_call"
    assert out_items[1]["call_id"] == "call_123"
    assert out_items[1]["name"] == "mcp.google_workspace.list_docs_in_folder"
    assert "\"folder_id\": \"root\"" in out_items[1]["arguments"]
    assert out_items[2]["type"] == "function_call_output"
    assert out_items[2]["call_id"] == "call_123"


def test_openai_responses_prefers_verbatim_arguments_json_over_dict() -> None:
    """xAI requires unmodified tool arguments; never emit Python dict repr."""
    from motet.core.models.adapters.providers.openai_responses import _format_messages_for_openai
    from motet.core.types import Message, RequestContext

    verbatim = '{"url":"https://www.cnn.com","timeout":30.0,"extract_strategy":"auto","include_links":true}'
    messages = [
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[
                {
                    "call_id": "call-abc",
                    "tool_name": "core.http_get_browser",
                    "arguments_json": verbatim,
                    # Parsed dict present — must NOT win over arguments_json / become str(dict)
                    "arguments": {
                        "url": "https://www.cnn.com",
                        "timeout": 30.0,
                        "extract_strategy": "auto",
                        "include_links": True,
                    },
                }
            ],
        ),
        Message(role="tool", content="OK", tool_call_id="call-abc"),
    ]

    out_items = _format_messages_for_openai(
        messages=messages,
        model_name="grok-4.5",
        request_context=RequestContext(enable_multimodal=False),
    )

    fc = next(i for i in out_items if i.get("type") == "function_call")
    assert fc["name"] == "core.http_get_browser"
    assert fc["arguments"] == verbatim
    assert "'" not in fc["arguments"]  # no Python repr


def test_openai_responses_skips_provider_executed_tool_calls_on_replay() -> None:
    """Provider builtins (kind='provider') are not function tools; never replay as function_call."""
    from motet.core.models.adapters.providers.openai_responses import _format_messages_for_openai
    from motet.core.types import Message, RequestContext

    messages = [
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[
                {
                    "call_id": "srvtoolu_1",
                    "tool_name": "openai.web_search",
                    "kind": "provider",
                    "arguments_json": '{"query":"latest news"}',
                },
                {
                    "call_id": "call_1",
                    "tool_name": "core.web_search",
                    "arguments_json": '{"query":"latest news"}',
                },
            ],
        ),
        Message(role="tool", content="OK", tool_call_id="call_1"),
    ]

    out_items = _format_messages_for_openai(
        messages=messages,
        model_name="grok-4.5",
        request_context=RequestContext(enable_multimodal=False),
    )

    function_calls = [i for i in out_items if i.get("type") == "function_call"]
    assert len(function_calls) == 1
    assert function_calls[0]["call_id"] == "call_1"
    assert function_calls[0]["name"] == "core.web_search"


def test_openai_responses_stream_emits_reasoning_summary_deltas_once() -> None:
    from motet.core.models.adapters.providers.openai_responses import OpenAIResponsesAdapter
    from motet.core.types import LLMRequest, Message, StopEvent, StopReason, TextDeltaEvent, ThinkingEvent, UsageEvent

    class FakeResponses:
        def create(self, **params: Any) -> list[dict[str, Any]]:
            assert params["reasoning"] == {"effort": "high", "summary": "auto"}
            return [
                {"type": "response.reasoning_summary_text.delta", "delta": "first "},
                {"type": "response.reasoning_summary_text.delta", "delta": "second"},
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {
                                "type": "reasoning",
                                "summary": [{"type": "summary_text", "text": "first second"}],
                            },
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "done"}],
                            },
                        ],
                        "usage": {
                            "input_tokens": 3,
                            "output_tokens": 5,
                            "total_tokens": 8,
                            "output_tokens_details": {"reasoning_tokens": 2},
                        },
                        "stop_reason": "end_turn",
                    },
                },
            ]

    class FakeClient:
        responses = FakeResponses()

    class FakeAdapter(OpenAIResponsesAdapter):
        def _client(self) -> FakeClient:
            return FakeClient()

    adapter = FakeAdapter(provider="openai", adapter_name="responses", credentials={"openai_api_key": "test"})

    events = list(
        adapter.stream(
            LLMRequest(
                messages=[Message(role="user", content="think")],
                model_settings={
                    "model_name": "gpt-5.5",
                    "enable_thinking": True,
                    "reasoning_effort": "high",
                },
            )
        )
    )

    thinking_events = [event for event in events if isinstance(event, ThinkingEvent)]
    assert [(event.text, event.is_complete) for event in thinking_events] == [
        ("first ", False),
        ("second", False),
        ("", True),
    ]

    text_events = [event for event in events if isinstance(event, TextDeltaEvent)]
    assert [event.text for event in text_events] == ["done"]

    usage_events = [event for event in events if isinstance(event, UsageEvent)]
    assert usage_events[0].usage.reasoning_tokens == 2

    stop_events = [event for event in events if isinstance(event, StopEvent)]
    assert stop_events[0].reason == StopReason.NATURAL_STOP

