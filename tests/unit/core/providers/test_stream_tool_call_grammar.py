"""
Motet - Canonical Stream Tool-Call Grammar Fixtures (ADR-0137)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Golden fixtures: Chat Completions ``delta.tool_calls`` fragments and
    Responses ``output_item`` + ``function_call_arguments.delta`` (no call_id,
    name null) emit ``tool_call_delta`` then ``tool_call_complete`` with one
    stable ``call_id`` (regression of ADR-0064 v1.6.1).

Dependencies:
    - motet.core.models.adapters.providers.chat_completions_deltas
    - motet.core.models.adapters.providers.openai_responses
    - tests.fixtures.canonical_adapter_contract.assert_canonical_stream_events

Usage:
    pytest tests/unit/core/providers/test_stream_tool_call_grammar.py
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from motet.core.models.adapters.providers.chat_completions_deltas import (
    ChatCompletionsToolCallAssembler,
)
from motet.core.models.adapters.providers.openai_responses import OpenAIResponsesAdapter
from motet.core.types import (
    LLMRequest,
    Message,
    StopEvent,
    ToolCallCompleteEvent,
    ToolCallDeltaEvent,
)
from tests.fixtures.canonical_adapter_contract import assert_canonical_stream_events


def test_chat_completions_delta_fragments_stable_call_id() -> None:
    assembler = ChatCompletionsToolCallAssembler()
    first = assembler.apply_delta(
        SimpleNamespace(
            index=0,
            id="call_abc",
            type="function",
            function=SimpleNamespace(name="mcp__github__list_repos", arguments=None),
        )
    )
    assert first is not None
    assert first.call_id == "call_abc"
    assert first.tool_name == "mcp.github.list_repos"

    second = assembler.apply_delta(
        SimpleNamespace(
            index=0,
            id=None,
            type=None,
            function=SimpleNamespace(name=None, arguments='{"org":'),
        )
    )
    assert second is not None
    assert second.call_id == "call_abc"
    assert second.arguments_delta == '{"org":'

    third = assembler.apply_delta(
        SimpleNamespace(
            index=0,
            id=None,
            type=None,
            function=SimpleNamespace(name=None, arguments='"x"}'),
        )
    )
    assert third is not None
    assert third.call_id == "call_abc"

    completes = list(assembler.complete())
    assert len(completes) == 1
    assert completes[0].call_id == "call_abc"
    assert completes[0].tool_name == "mcp.github.list_repos"
    assert completes[0].arguments_json == '{"org":"x"}'


def test_ingest_complete_calls_maps_wire_name_and_keeps_parallel_indexes() -> None:
    assembler = ChatCompletionsToolCallAssembler()
    reqs = assembler.ingest_complete_calls(
        [
            SimpleNamespace(
                id="call_a",
                type="function",
                function=SimpleNamespace(
                    name="mcp__test__add_two_numbers", arguments='{"a": 7}'
                ),
            ),
            SimpleNamespace(
                id="call_b",
                type="builtin_function",
                function=SimpleNamespace(name="$web_search", arguments="{}"),
            ),
        ]
    )
    assert [r.call_id for r in reqs] == ["call_a", "call_b"]
    assert reqs[0].tool_name == "mcp.test.add_two_numbers"
    assert reqs[0].kind is None
    assert reqs[1].tool_name == "$web_search"
    assert reqs[1].kind == "provider"


def test_responses_item_id_map_emits_delta_with_mapped_call_id() -> None:
    """function_call_arguments.delta has no call_id and name is null (ADR-0064 v1.6.1)."""

    class FakeResponses:
        def create(self, **params: Any) -> list[dict[str, Any]]:
            return [
                {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "fc_item_1",
                        "type": "function_call",
                        "call_id": "call_mapped",
                        "name": "mcp__github__list_repos",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_item_1",
                    "delta": '{"org":"x"}',
                    "call_id": None,
                    "name": None,
                },
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": "fc_item_1",
                    "arguments": '{"org":"x"}',
                    "call_id": None,
                    "name": None,
                },
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {
                                "type": "function_call",
                                "id": "fc_item_1",
                                "call_id": "call_mapped",
                                "name": "mcp__github__list_repos",
                                "arguments": '{"org":"x"}',
                            }
                        ],
                        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                    },
                },
            ]

    class FakeClient:
        responses = FakeResponses()

    class FakeAdapter(OpenAIResponsesAdapter):
        def _client(self) -> FakeClient:
            return FakeClient()

    adapter = FakeAdapter(
        provider="openai",
        adapter_name="responses",
        credentials={"openai_api_key": "test"},
    )
    events = list(
        adapter.stream(
            LLMRequest(
                messages=[Message(role="user", content="list")],
                model_settings={"model_name": "gpt-4.1-mini"},
            )
        )
    )
    assert_canonical_stream_events(events)
    deltas = [e for e in events if isinstance(e, ToolCallDeltaEvent)]
    completes = [e for e in events if isinstance(e, ToolCallCompleteEvent)]
    assert deltas, "expected tool_call_delta with mapped call_id"
    assert all(e.call_id == "call_mapped" for e in deltas)
    assert deltas[0].tool_name == "mcp.github.list_repos"
    assert len(completes) == 1
    assert completes[0].call_id == "call_mapped"
    assert completes[0].tool_name == "mcp.github.list_repos"
    assert any(isinstance(e, StopEvent) for e in events)
