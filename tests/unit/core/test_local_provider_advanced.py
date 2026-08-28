"""
Motet - Local Provider Advanced Feature Parity Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-13

Description:
    Unit tests for the local provider's canonical-protocol parity work (ADR-0064 /
    ADR-0115): the pure reasoning/tool-call/usage helpers and the LocalAdapter's
    mapping of manager output onto canonical responses and stream events.

    These tests are asset-free (no GGUF weights, no Redis): the pure helpers are
    exercised directly and the adapter is driven against a stub LocalInference
    client, so they run in CI without the distributed stack.

Dependencies:
    - pytest: Test framework
    - motet.core.models.local.reasoning: pure helpers under test
    - motet.core.models.adapters.providers.local: LocalAdapter under test
    - motet.core.types: canonical protocol models

Usage:
    pytest tests/unit/core/test_local_provider_advanced.py

Notes:
    - Tool/reasoning capability gating is exercised against the real spec registry
      (qwen3-8b-instruct, Gemma 4 E4B, and Hermes 4 advertise CAP_REASONING;
      phi-4-mini is tool-capable via system-message injection; gemma-4-26b-a4b is
      tool-capable but does not currently expose separable reasoning; gemma-3-4b
      is not tool-capable), so the tests also guard the spec capability set.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest

from motet.core.models.local.reasoning import (
    ThinkStreamRouter,
    ToolCallStreamGate,
    extract_tool_calls_from_text,
    looks_like_unmatched_tool_call_markup,
    map_finish_reason,
    map_usage,
    parse_tool_calls,
    split_reasoning,
)
from motet.core.models.adapters.providers.local import LocalAdapter
from motet.core.models.local.profiles import profile_for_model
from motet.core.types import (
    CanonicalToolSchema,
    LLMRequest,
    Message,
    StopEvent,
    StopReason,
    TextDeltaEvent,
    ThinkingEvent,
    ToolCallCompleteEvent,
    ToolCallDeltaEvent,
    ToolCallRequest,
    UsageEvent,
)


# Profile seams under test. Production code (the manager's inference paths)
# calls these directly on the family profile; these shims keep the test bodies
# readable after the manager's redundant module-level wrappers were removed.
def _apply_tool_injection(
    messages: List[Dict[str, Any]], model_id: str, request: Dict[str, Any]
) -> List[Dict[str, Any]]:
    return profile_for_model(model_id).apply_tool_schemas(messages, request)


def _apply_thinking_control(
    messages: List[Dict[str, Any]], model_id: str, *, enable_thinking: bool
) -> List[Dict[str, Any]]:
    return profile_for_model(model_id).apply_thinking_control(messages, enable_thinking)


def _normalize_chat_messages(
    messages: List[Dict[str, Any]], model_id: str
) -> List[Dict[str, Any]]:
    return profile_for_model(model_id).normalize_messages(messages)


# --------------------------------------------------------------------------- #
# Pure helper: split_reasoning (non-stream <think> separation)
# --------------------------------------------------------------------------- #


def test_split_reasoning_basic() -> None:
    clean, reasoning = split_reasoning("<think>plan it</think>Hello world")
    assert clean == "Hello world"
    assert reasoning == "plan it"


def test_split_reasoning_no_block() -> None:
    clean, reasoning = split_reasoning("just an answer")
    assert clean == "just an answer"
    assert reasoning is None


def test_split_reasoning_unclosed_block() -> None:
    clean, reasoning = split_reasoning("<think>truncated mid reasoning")
    assert clean == ""
    assert reasoning == "truncated mid reasoning"


def test_split_reasoning_multiple_blocks() -> None:
    clean, reasoning = split_reasoning("a<think>r1</think>b<think>r2</think>c")
    assert clean == "abc"
    assert reasoning == "r1\nr2"


def test_split_reasoning_gemma4_empty_thought_channel_marker() -> None:
    clean, reasoning = split_reasoning(
        "<|channel>thought\n<channel|>I have taken a screenshot."
    )
    assert clean == "I have taken a screenshot."
    assert reasoning is None


def test_split_reasoning_gemma4_thought_channel() -> None:
    clean, reasoning = split_reasoning(
        "<|channel>thought\nNeed to summarize the tool result.\n<channel|>Done."
    )
    assert clean == "Done."
    assert reasoning == "Need to summarize the tool result."


def test_split_reasoning_empty() -> None:
    assert split_reasoning("") == ("", None)


# --------------------------------------------------------------------------- #
# Pure helper: ThinkStreamRouter (streaming <think> routing)
# --------------------------------------------------------------------------- #


def _drain(router: ThinkStreamRouter, chunks: List[str]) -> List[tuple]:
    out: List[tuple] = []
    for c in chunks:
        out += router.feed(c)
    out += router.flush()
    return out


def test_stream_router_tag_split_across_chunks() -> None:
    router = ThinkStreamRouter()
    out = _drain(router, ["<th", "ink>secret", " plan</thi", "nk>visible", " text"])
    assert out == [
        ("thinking", "secret"),
        ("thinking", " plan"),
        ("text", "visible"),
        ("text", " text"),
    ]


def test_stream_router_plain_text() -> None:
    router = ThinkStreamRouter()
    out = _drain(router, ["hello ", "world"])
    assert out == [("text", "hello "), ("text", "world")]


def test_stream_router_all_thinking_then_flush() -> None:
    router = ThinkStreamRouter()
    out = _drain(router, ["<think>still thinking"])
    assert out == [("thinking", "still thinking")]


def test_stream_router_partial_tag_held_then_resolves_as_text() -> None:
    # A trailing "<" that turns out to be ordinary text must be emitted on flush.
    router = ThinkStreamRouter()
    out = _drain(router, ["a <"])
    assert out == [("text", "a "), ("text", "<")]


# --------------------------------------------------------------------------- #
# Pure helpers: finish_reason / usage mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("stop", StopReason.NATURAL_STOP),
        ("length", StopReason.LENGTH_LIMIT),
        ("tool_calls", StopReason.TOOL_CALLS),
        ("content_filter", StopReason.SAFETY_FILTER),
        ("weird-unknown", StopReason.NATURAL_STOP),
        (None, StopReason.NATURAL_STOP),
    ],
)
def test_map_finish_reason(raw: Any, expected: StopReason) -> None:
    assert map_finish_reason(raw) == expected


def test_map_finish_reason_tool_calls_wins() -> None:
    assert map_finish_reason("stop", has_tool_calls=True) == StopReason.TOOL_CALLS


def test_map_usage_computes_total() -> None:
    usage = map_usage({"prompt_tokens": 3, "completion_tokens": 5})
    assert usage is not None
    assert usage.prompt_tokens == 3
    assert usage.output_tokens == 5
    assert usage.total_tokens == 8


def test_map_usage_none_when_empty() -> None:
    assert map_usage(None) is None
    assert map_usage({}) is None


# --------------------------------------------------------------------------- #
# Pure helper: parse_tool_calls
# --------------------------------------------------------------------------- #


def test_parse_tool_calls_json_string_args() -> None:
    calls = parse_tool_calls(
        [{"id": "c1", "function": {"name": "get_weather", "arguments": '{"city": "NYC"}'}}]
    )
    assert len(calls) == 1
    call = calls[0]
    assert isinstance(call, ToolCallRequest)
    assert call.call_id == "c1"
    assert call.tool_name == "get_weather"
    assert call.arguments == {"city": "NYC"}
    assert call.tool_call_index == 0


def test_parse_tool_calls_dict_args_and_missing_id() -> None:
    calls = parse_tool_calls([{"function": {"name": "do", "arguments": {"a": 1}}}])
    assert len(calls) == 1
    assert calls[0].call_id == "call_0"
    assert calls[0].arguments_json == '{"a": 1}'


def test_parse_tool_calls_skips_nameless_and_handles_bad_json() -> None:
    calls = parse_tool_calls(
        [
            {"id": "x", "function": {"arguments": "{}"}},  # no name -> skipped
            {"id": "y", "function": {"name": "f", "arguments": "not json"}},
        ]
    )
    assert [c.tool_name for c in calls] == ["f"]
    assert calls[0].arguments is None  # unparseable -> best-effort None


def test_parse_tool_calls_empty() -> None:
    assert parse_tool_calls(None) == []
    assert parse_tool_calls([]) == []


# --------------------------------------------------------------------------- #
# Pure helper: extract_tool_calls_from_text (per-family text formats)
# --------------------------------------------------------------------------- #


def test_extract_qwen_tool_call_tags() -> None:
    text = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "SF"}}\n</tool_call>'
    clean, calls = extract_tool_calls_from_text(text)
    assert clean == ""
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "get_weather"
    assert calls[0]["function"]["arguments"] == '{"city": "SF"}'


def test_extract_qwen_tool_call_keeps_surrounding_text() -> None:
    text = 'Sure!<tool_call>{"name": "f", "arguments": {"a": 1}}</tool_call>'
    clean, calls = extract_tool_calls_from_text(text)
    assert clean == "Sure!"
    assert len(calls) == 1


def test_extract_mistral_tool_calls() -> None:
    text = '[TOOL_CALLS][{"name": "search", "arguments": {"q": "x"}}]'
    clean, calls = extract_tool_calls_from_text(text)
    assert clean == ""
    assert calls[0]["function"]["name"] == "search"


def test_extract_ministral_bare_name_json_call() -> None:
    text = 'get_weather{"city": "San Francisco"}'
    clean, calls = extract_tool_calls_from_text(text, tool_names=["get_weather"])
    assert clean == ""
    parsed = parse_tool_calls(calls)
    assert parsed[0].tool_name == "get_weather"
    assert parsed[0].arguments == {"city": "San Francisco"}


def test_extract_llama_bare_json_object() -> None:
    text = '{"name": "lookup", "parameters": {"id": 5}}'
    clean, calls = extract_tool_calls_from_text(text)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "lookup"


def test_extract_no_tool_call_returns_text() -> None:
    clean, calls = extract_tool_calls_from_text("just a normal answer")
    assert clean == "just a normal answer"
    assert calls == []


def test_extract_gemma_tool_code_block() -> None:
    # Gemma emits a ```tool_code``` Python block calling the tool.
    text = (
        "```tool_code\n"
        "from weather_tool import get_weather\n\n"
        "weather = get_weather(city='San Francisco')\n"
        "print(weather)\n"
        "```"
    )
    clean, calls = extract_tool_calls_from_text(text, tool_names=["get_weather"])
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "get_weather"
    parsed = parse_tool_calls(calls)
    assert parsed[0].arguments == {"city": "San Francisco"}
    assert "tool_code" not in clean


def test_extract_gemma_tool_code_ignores_non_tool_calls() -> None:
    # Without an allowlist, builtins like print() must not become tool calls.
    text = "```tool_code\nx = get_weather(city='NYC')\nprint(x)\n```"
    _, calls = extract_tool_calls_from_text(text)
    names = {c["function"]["name"] for c in calls}
    assert "print" not in names
    assert "get_weather" in names


def test_extract_gemma_tool_code_allowlist_scopes_calls() -> None:
    # With an allowlist, only declared tools are extracted.
    text = "```tool_code\nget_weather(city='NYC')\nsend_email(to='a@b.c')\n```"
    _, calls = extract_tool_calls_from_text(text, tool_names=["get_weather"])
    names = {c["function"]["name"] for c in calls}
    assert names == {"get_weather"}


def test_extract_gemma4_control_token_tool_call() -> None:
    # Gemma 4 documents function calls with tool-call control tokens.
    text = '<|tool_call>call:get_weather{city:<|"|>NYC<|"|>}<tool_call|>'
    clean, calls = extract_tool_calls_from_text(text, tool_names=["get_weather"])
    assert clean == ""
    parsed = parse_tool_calls(calls)
    assert parsed[0].tool_name == "get_weather"
    assert parsed[0].arguments == {"city": "NYC"}


def test_extract_gemma4_control_token_tool_call_with_response_marker() -> None:
    text = (
        '<|tool_call>call:get_weather{city:<|"|>London<|"|>,unit:<|"|>celsius<|"|>}'
        "<tool_call|><|tool_response>"
    )
    clean, calls = extract_tool_calls_from_text(text, tool_names=["get_weather"])
    assert clean == ""
    parsed = parse_tool_calls(calls)
    assert parsed[0].arguments == {"city": "London", "unit": "celsius"}


def test_extract_gemma4_control_token_unknown_tool_left_untouched() -> None:
    text = '<|tool_call>call:send_email{to:<|"|>a@example.com<|"|>}<tool_call|>'
    clean, calls = extract_tool_calls_from_text(text, tool_names=["get_weather"])
    assert calls == []
    assert clean == text


def test_extract_phi_bare_json_array() -> None:
    # phi-4 (Path A) emits a bare top-level JSON array of tool-call objects.
    text = '[{"name": "get_weather", "arguments": {"city": "San Francisco"}}]'
    clean, calls = extract_tool_calls_from_text(text)
    assert clean == ""
    assert len(calls) == 1
    parsed = parse_tool_calls(calls)
    assert parsed[0].tool_name == "get_weather"
    assert parsed[0].arguments == {"city": "San Francisco"}


def test_extract_phi_trailing_json_array_after_prose() -> None:
    # phi-4 sometimes explains what it will do before emitting the tool-call array.
    text = (
        'To take a screenshot, I will use the "mcp__playwright__browser_take_screenshot" tool.\n\n'
        '[{"name": "mcp__playwright__browser_take_screenshot", '
        '"arguments": {"name": "cantina_co_screenshot", "url": "https://cantina.co"}}]'
    )
    clean, calls = extract_tool_calls_from_text(
        text,
        tool_names=[
            "mcp__playwright__browser_take_screenshot",
            "workflow_navigate_screenshot",
        ],
    )
    assert clean == (
        'To take a screenshot, I will use the "mcp__playwright__browser_take_screenshot" tool.'
    )
    parsed = parse_tool_calls(calls)
    assert parsed[0].tool_name == "mcp__playwright__browser_take_screenshot"
    assert parsed[0].arguments == {
        "name": "cantina_co_screenshot",
        "url": "https://cantina.co",
    }


def test_extract_phi_uppercase_python_call() -> None:
    # phi-4 can emit an uppercase function-call expression instead of JSON.
    text = 'GET_WEATHER(city="San Francisco")'
    clean, calls = extract_tool_calls_from_text(text, tool_names=["get_weather"])
    assert clean == ""
    parsed = parse_tool_calls(calls)
    assert parsed[0].tool_name == "get_weather"
    assert parsed[0].arguments == {"city": "San Francisco"}


def test_extract_bare_json_array_multiple_calls() -> None:
    text = (
        '[{"name": "a", "arguments": {"x": 1}}, '
        '{"name": "b", "parameters": {"y": 2}}]'
    )
    _, calls = extract_tool_calls_from_text(text)
    assert [c["function"]["name"] for c in calls] == ["a", "b"]
    assert calls[1]["id"] == "call_1"  # index advances across the array


def test_extract_plain_json_array_not_tool_calls() -> None:
    # A bare array that is not tool calls must be left as text, not mis-parsed.
    text = '[{"foo": 1}, {"bar": 2}]'
    clean, calls = extract_tool_calls_from_text(text)
    assert calls == []
    assert clean == text


def test_extract_phi_fenced_json_with_unprefixed_name() -> None:
    # Observed phi-4 failure: narrates, then wraps the call in a ```json fence,
    # using the unprefixed tool name. The trailing fence breaks the bare-JSON
    # parser, and the name lacks the ``core.`` prefix. Both must be tolerated.
    text = (
        "To browse CNN.com I will use the http_get_browser tool.\n\n"
        "```json\n"
        '{\n  "name": "http_get_browser",\n  "parameters": {"url": "https://www.cnn.com", '
        '"screenshot": true}\n}\n'
        "```"
    )
    clean, calls = extract_tool_calls_from_text(text, tool_names=["core.http_get_browser"])
    assert clean == "To browse CNN.com I will use the http_get_browser tool."
    parsed = parse_tool_calls(calls)
    assert parsed[0].tool_name == "core.http_get_browser"
    assert parsed[0].arguments == {"url": "https://www.cnn.com", "screenshot": True}


def test_extract_fenced_json_nested_objects_preserved() -> None:
    # Nested braces inside the fenced payload must round-trip (non-greedy fence body).
    text = '```json\n{"name": "t", "parameters": {"a": {"b": 1}}}\n```'
    _, calls = extract_tool_calls_from_text(text, tool_names=["t"])
    parsed = parse_tool_calls(calls)
    assert parsed[0].arguments == {"a": {"b": 1}}


def test_extract_fenced_non_tool_json_left_untouched() -> None:
    # A ```json fence that is not a tool call must not be consumed.
    text = 'Config:\n\n```json\n{"theme": "dark", "size": 12}\n```'
    clean, calls = extract_tool_calls_from_text(text)
    assert calls == []
    assert clean == text


def test_extract_fenced_json_undeclared_tool_dropped() -> None:
    # Fenced JSON naming an undeclared tool must be rejected (no match).
    text = '```json\n{"name": "delete_everything", "parameters": {}}\n```'
    _, calls = extract_tool_calls_from_text(text, tool_names=["core.http_get_browser"])
    assert calls == []


def test_canonical_name_suffix_match_unambiguous() -> None:
    # Unprefixed name resolves to the single declared tool sharing its suffix.
    text = '[{"name": "read_document_comments", "arguments": {}}]'
    _, calls = extract_tool_calls_from_text(
        text, tool_names=["mcp.google_workspace.read_document_comments"]
    )
    assert calls and calls[0]["function"]["name"] == "mcp.google_workspace.read_document_comments"


def test_canonical_name_suffix_match_ambiguous_rejected() -> None:
    # Ambiguous suffix (two declared tools share it) must NOT be guessed.
    text = '[{"name": "read", "arguments": {}}]'
    _, calls = extract_tool_calls_from_text(text, tool_names=["a.read", "b.read"])
    assert calls == []


def test_extract_phi_python_fence_wire_format_name() -> None:
    # Observed phi-4 failure: a ```python block calling the tool by its provider
    # wire-format name (dots -> __), which the model was shown in its schema.
    text = (
        "I will use the core__http_get_browser tool.\n\n"
        "```python\n"
        'core__http_get_browser(url="https://www.cnn.com", wait_for="body")\n'
        "```"
    )
    _, calls = extract_tool_calls_from_text(text, tool_names=["core.http_get_browser"])
    parsed = parse_tool_calls(calls)
    assert parsed[0].tool_name == "core.http_get_browser"
    assert parsed[0].arguments == {"url": "https://www.cnn.com", "wait_for": "body"}


def test_canonical_name_wire_format_multi_segment_mcp() -> None:
    # Multi-segment MCP wire name maps back to its canonical dotted form.
    text = '[{"name": "mcp__google_workspace__read_document_comments", "arguments": {"document_id": "d"}}]'
    _, calls = extract_tool_calls_from_text(
        text, tool_names=["mcp.google_workspace.read_document_comments"]
    )
    assert calls and calls[0]["function"]["name"] == "mcp.google_workspace.read_document_comments"


def test_canonical_name_undeclared_wire_name_dropped() -> None:
    # A wire-looking name that matches no declared tool must be rejected.
    text = '[{"name": "evil__delete", "arguments": {}}]'
    _, calls = extract_tool_calls_from_text(text, tool_names=["core.http_get_browser"])
    assert calls == []


@pytest.mark.parametrize(
    "text",
    [
        '<tool_call>{"name": "core.agent_turn", "arguments": {}}</tool_call>',
        '[TOOL_CALLS][{"name": "core.agent_turn", "arguments": {}}]',
    ],
)
def test_extract_self_delimited_undeclared_tool_rejected(text: str) -> None:
    # Self-delimited formats must still honor the declared tool list; otherwise
    # small models can execute copied example tools that were never exposed.
    clean, calls = extract_tool_calls_from_text(text, tool_names=["core.http_get_browser"])
    assert calls == []
    assert clean == text
    assert looks_like_unmatched_tool_call_markup(clean)


# --------------------------------------------------------------------------- #
# System-message tool injection (manager-side, model-free)
# --------------------------------------------------------------------------- #

_PATH_A_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


def test_tool_injection_attaches_to_system_message_for_phi() -> None:
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "weather in SF?"},
    ]
    out = _apply_tool_injection(messages, "phi-4-mini", {"tools": _PATH_A_TOOLS})
    sys_msg = out[0]
    assert sys_msg["role"] == "system"
    # The template reads message['tools'] as a string of function definitions.
    assert isinstance(sys_msg["tools"], str)
    assert "get_weather" in sys_msg["tools"]
    assert '"type": "function"' not in sys_msg["tools"]  # envelope unwrapped


def test_tool_injection_prepends_system_when_absent() -> None:
    messages = [{"role": "user", "content": "weather in SF?"}]
    out = _apply_tool_injection(messages, "phi-4-mini", {"tools": _PATH_A_TOOLS})
    assert out[0]["role"] == "system"
    assert "get_weather" in out[0]["tools"]


def test_tool_injection_noop_for_native_family() -> None:
    messages = [{"role": "user", "content": "hi"}]
    out = _apply_tool_injection(messages, "qwen3-8b-instruct", {"tools": _PATH_A_TOOLS})
    assert out == messages
    assert "tools" not in out[0]


def test_tool_injection_noop_for_gemma4_native_template() -> None:
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "weather in London?"},
    ]
    out = _apply_tool_injection(messages, "gemma-4-e4b", {"tools": _PATH_A_TOOLS})
    assert out == messages
    assert "tools" not in out[0]


def test_tool_injection_noop_without_tools() -> None:
    messages = [{"role": "system", "content": "x"}]
    out = _apply_tool_injection(messages, "phi-4-mini", {})
    assert out == messages


def test_thinking_control_appends_no_think_for_qwen() -> None:
    messages = [{"role": "user", "content": "what is the population of Paris?"}]
    out = _apply_thinking_control(messages, "qwen3-8b-instruct", enable_thinking=False)
    assert out[0]["content"].endswith("/no_think")


def test_thinking_control_noop_when_enabled() -> None:
    messages = [{"role": "user", "content": "think carefully"}]
    out = _apply_thinking_control(messages, "qwen3-8b-instruct", enable_thinking=True)
    assert out == messages


def test_thinking_control_adds_gemma4_think_token() -> None:
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "think carefully"},
    ]
    out = _apply_thinking_control(messages, "gemma-4-e4b", enable_thinking=True)
    assert out[0]["content"] == "<|think|>\nYou are helpful."


def test_thinking_control_removes_gemma4_think_token_when_disabled() -> None:
    messages = [
        {"role": "system", "content": "<|think|>\nYou are helpful."},
        {"role": "user", "content": "answer quickly"},
    ]
    out = _apply_thinking_control(messages, "gemma-4-e4b", enable_thinking=False)
    assert out[0]["content"] == "You are helpful."


def test_qwen_raw_fallback_uses_chatml_not_transcript_prompt() -> None:
    from motet.core.models.local.inference_manager import LocalInferenceManager

    mgr = LocalInferenceManager.__new__(LocalInferenceManager)
    prompt = mgr._messages_to_prompt(
        [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "what is the capital of France?"},
        ],
        model_id="qwen3-8b-instruct",
    )

    assert "<|im_start|>system\nYou are helpful.<|im_end|>" in prompt
    assert "<|im_start|>user\nwhat is the capital of France?<|im_end|>" in prompt
    assert prompt.endswith("<|im_start|>assistant\n")
    assert "User:" not in prompt
    assert "Assistant:" not in prompt


def test_profile_registry_resolves_longest_family_match() -> None:
    from motet.core.models.local.profiles import profile_for_model, resolve_local_model_family

    assert resolve_local_model_family("hermes-4-14b") == "hermes"
    assert profile_for_model("hermes-4-14b").family == "hermes"
    assert resolve_local_model_family("gemma-4-26b-a4b") == "gemma-4"
    assert profile_for_model("gemma-4-26b-a4b").family == "gemma-4"
    assert profile_for_model("llama-3.1-8b-instruct").family == "llama-3"
    assert profile_for_model("ministral-3-8b-instruct").family == "ministral"


def test_qwen_profile_applies_thinking_control_and_chatml_fallback() -> None:
    from motet.core.models.local.profiles import profile_for_model

    profile = profile_for_model("qwen3-8b-instruct")
    messages = [{"role": "user", "content": "what is the population of Paris?"}]
    out = profile.apply_thinking_control(messages, enabled=False)
    assert out[0]["content"].endswith("/no_think")

    prompt = profile.fallback_prompt(out)
    assert prompt.startswith("<|im_start|>user\n")
    assert prompt.endswith("<|im_start|>assistant\n")


def test_llama3_profile_uses_native_stops_and_fallback_prompt() -> None:
    from motet.core.models.local.inference_manager import LocalInferenceManager
    from motet.core.models.local.profiles import profile_for_model

    profile = profile_for_model("llama-3.1-8b-instruct")
    assert profile.stop_sequences() == ["<|eot_id|>", "<|end_of_text|>"]

    mgr = LocalInferenceManager.__new__(LocalInferenceManager)
    prompt = mgr._messages_to_prompt(
        [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
        ],
        model_id="llama-3.1-8b-instruct",
    )

    assert prompt.startswith("<|begin_of_text|>")
    assert "<|start_header_id|>system<|end_header_id|>\n\nYou are helpful.<|eot_id|>" in prompt
    assert "<|start_header_id|>user<|end_header_id|>\n\nhi<|eot_id|>" in prompt
    assert prompt.endswith("<|start_header_id|>assistant<|end_header_id|>\n\n")


def test_phi_profile_injects_tools_and_suppresses_native_tool_kwargs() -> None:
    from motet.core.models.local.profiles import profile_for_model

    profile = profile_for_model("phi-4-mini")
    messages = [{"role": "user", "content": "weather in SF?"}]
    out = profile.apply_tool_schemas(messages, {"tools": _PATH_A_TOOLS})
    assert out[0]["role"] == "system"
    assert "get_weather" in out[0]["tools"]

    kwargs: Dict[str, Any] = {}
    profile.apply_tool_kwargs({"tools": _PATH_A_TOOLS, "tool_choice": "auto"}, kwargs)
    assert kwargs == {}


def test_phi_profile_adds_anti_narration_instruction() -> None:
    """Regression (ADR-0115): phi-4-mini announced tool calls in prose
    ("I will use the tool core__web_search... Please wait a moment.") instead of
    emitting the JSON call, leaving nothing for the recovery parsers. The system
    message must instruct the model to emit the call itself."""
    from motet.core.models.local.profiles import profile_for_model
    from motet.core.models.local.profiles.phi import _TOOL_EMISSION_INSTRUCTION

    profile = profile_for_model("phi-4-mini")

    # Inserted system message (no system turn in payload).
    out = profile.apply_tool_schemas(
        [{"role": "user", "content": "weather in SF?"}], {"tools": _PATH_A_TOOLS}
    )
    assert _TOOL_EMISSION_INSTRUCTION in out[0]["content"]

    # Existing system message: instruction appended, original content kept.
    out = profile.apply_tool_schemas(
        [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "weather in SF?"},
        ],
        {"tools": _PATH_A_TOOLS},
    )
    assert out[0]["content"].startswith("You are helpful.")
    assert _TOOL_EMISSION_INSTRUCTION in out[0]["content"]

    # Idempotent: reapplying does not duplicate the instruction.
    out = profile.apply_tool_schemas(out, {"tools": _PATH_A_TOOLS})
    assert out[0]["content"].count(_TOOL_EMISSION_INSTRUCTION) == 1

    # No tools: message untouched.
    out = profile.apply_tool_schemas(
        [{"role": "user", "content": "hi"}], {"tools": []}
    )
    assert out == [{"role": "user", "content": "hi"}]


def test_gemma4_preserves_system_role_for_tool_template() -> None:
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "weather in London?"},
    ]
    assert _normalize_chat_messages(messages, "gemma-4-e4b") == messages


def test_gemma4_profile_exposes_native_stops_and_tool_kwargs() -> None:
    from motet.core.models.local.profiles import profile_for_model

    profile = profile_for_model("gemma-4-e4b")
    assert profile.stop_sequences() == ["<|turn|>", "<|tool_response|>"]

    kwargs: Dict[str, Any] = {}
    profile.apply_tool_kwargs({"tools": _PATH_A_TOOLS, "tool_choice": "auto"}, kwargs)
    assert kwargs["tools"] == _PATH_A_TOOLS
    assert kwargs["tool_choice"] == "auto"


def test_gemma4_profile_inserts_system_turn_for_thinking() -> None:
    from motet.core.models.local.profiles import profile_for_model

    profile = profile_for_model("gemma-4-26b-a4b")
    out = profile.apply_thinking_control(
        [{"role": "user", "content": "Solve this."}],
        enabled=True,
    )

    assert out[0] == {"role": "system", "content": "<|think|>"}
    assert out[1]["role"] == "user"


def test_hermes_profile_injects_thinking_and_tools() -> None:
    from motet.core.models.local.profiles import profile_for_model

    profile = profile_for_model("hermes-4-14b")
    messages = [{"role": "user", "content": "weather in SF?"}]

    with_thinking = profile.apply_thinking_control(messages, enabled=True)
    assert with_thinking[0]["role"] == "system"
    assert "<think> </think>" in with_thinking[0]["content"]

    with_tools = profile.apply_tool_schemas(with_thinking, {"tools": _PATH_A_TOOLS})
    assert "<tools>" in with_tools[0]["content"]
    assert "<tool_call>" in with_tools[0]["content"]
    assert "get_weather" in with_tools[0]["content"]

    kwargs: Dict[str, Any] = {}
    profile.apply_tool_kwargs({"tools": _PATH_A_TOOLS, "tool_choice": "auto"}, kwargs)
    assert kwargs == {}


def test_hermes_profile_removes_thinking_prompt_when_disabled() -> None:
    from motet.core.models.local.profiles import profile_for_model

    profile = profile_for_model("hermes-4-14b")
    enabled = profile.apply_thinking_control(
        [{"role": "system", "content": "You are helpful."}],
        enabled=True,
    )
    disabled = profile.apply_thinking_control(enabled, enabled=False)

    assert disabled[0]["content"] == "You are helpful."


def test_gemma4_converts_tool_result_to_user_turn_for_second_inference() -> None:
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "can you take a screenshot of cantina.co"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "call_id": "call_0",
                    "tool_name": "mcp.playwright.browser_take_screenshot",
                    "arguments_json": '{"name": "cantina_co_screenshot"}',
                    "arguments": {"name": "cantina_co_screenshot"},
                }
            ],
        },
        {
            "role": "tool",
            "content": (
                "Screenshot saved to: root/Downloads/cantina_co_screenshot.png\n"
                "Screenshot also stored in memory with name: 'cantina_co_screenshot'"
            ),
            "name": "mcp.playwright.browser_take_screenshot",
            "tool_call_id": "call_0",
        },
    ]

    out = _normalize_chat_messages(messages, "gemma-4-26b-a4b")

    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]
    assistant = out[-2]
    assert assistant["tool_calls"] == [
        {
            "id": "call_0",
            "type": "function",
            "function": {
                "name": "mcp.playwright.browser_take_screenshot",
                "arguments": {"name": "cantina_co_screenshot"},
            },
        }
    ]
    assert out[-1]["role"] == "user"
    assert "Tool mcp.playwright.browser_take_screenshot returned:" in out[-1]["content"]
    assert "Do not call the same tool again" in out[-1]["content"]


def test_gemma4_tool_response_uses_call_id_name_when_tool_name_missing() -> None:
    out = _normalize_chat_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "abc",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "London"}',
                        },
                    }
                ],
            },
            {"role": "tool", "content": '{"temperature": 15}', "tool_call_id": "abc"},
        ],
        "gemma-4-e4b",
    )

    assert out[0]["tool_calls"] == [
        {
            "id": "abc",
            "type": "function",
            "function": {
                "name": "get_weather",
                "arguments": {"city": "London"},
            },
        }
    ]
    assert out[1]["role"] == "user"
    assert "Tool get_weather returned:" in out[1]["content"]
    assert '"temperature": 15' in out[1]["content"]


def test_apply_tool_kwargs_skips_native_forward_for_phi() -> None:
    kwargs: Dict[str, Any] = {}
    profile_for_model("phi-4-mini").apply_tool_kwargs(
        {"model_id": "phi-4-mini", "tools": _PATH_A_TOOLS, "tool_choice": "auto"}, kwargs
    )
    assert "tools" not in kwargs  # injection family: not forwarded natively


def test_apply_tool_kwargs_forwards_native_for_qwen() -> None:
    kwargs: Dict[str, Any] = {}
    profile_for_model("qwen3-8b-instruct").apply_tool_kwargs(
        {"model_id": "qwen3-8b-instruct", "tools": _PATH_A_TOOLS}, kwargs
    )
    assert kwargs["tools"] == _PATH_A_TOOLS


def test_apply_tool_kwargs_forwards_native_for_gemma4() -> None:
    kwargs: Dict[str, Any] = {}
    profile_for_model("gemma-4-e4b").apply_tool_kwargs(
        {"model_id": "gemma-4-e4b", "tools": _PATH_A_TOOLS, "tool_choice": "auto"},
        kwargs,
    )
    assert kwargs["tools"] == _PATH_A_TOOLS
    assert kwargs["tool_choice"] == "auto"


def test_extract_tool_calls_then_parse_canonical() -> None:
    # End-to-end: text -> raw dicts -> canonical ToolCallRequest.
    _, raw = extract_tool_calls_from_text(
        '<tool_call>{"name": "g", "arguments": {"k": "v"}}</tool_call>'
    )
    canonical = parse_tool_calls(raw)
    assert canonical[0].tool_name == "g"
    assert canonical[0].arguments == {"k": "v"}


# --------------------------------------------------------------------------- #
# Adapter: non-stream canonical mapping
# --------------------------------------------------------------------------- #


class _StubClient:
    """Stub LocalInferenceClient capturing kwargs and returning canned output."""

    def __init__(self, result: Dict[str, Any] | None = None, events: List[Any] | None = None) -> None:
        self._result = result or {"success": True, "text": "ok", "elapsed_seconds": 0.0}
        self._events = events or []
        self.last_kwargs: Dict[str, Any] = {}
        self.last_max_tokens: int | None = None

    def infer_sync(self, *, model_id: str, messages: List[Dict[str, Any]], temperature: float, max_tokens: int, **kwargs: Any) -> Dict[str, Any]:
        self.last_kwargs = kwargs
        self.last_max_tokens = max_tokens
        return self._result

    def infer_stream(self, *, model: str, messages: List[Dict[str, Any]], temperature: float, max_tokens: int, **kwargs: Any) -> Iterator[Any]:
        self.last_kwargs = kwargs
        self.last_max_tokens = max_tokens
        yield from self._events


def _patch_client(monkeypatch: pytest.MonkeyPatch, stub: _StubClient) -> None:
    monkeypatch.setattr(
        "motet.core.models.adapters.providers.local._get_client",
        lambda: stub,
    )


def test_complete_maps_reasoning_usage_and_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient(
        result={
            "success": True,
            "text": "the answer",
            "reasoning": "chain of thought",
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            "finish_reason": "length",
            "elapsed_seconds": 0.0,
        }
    )
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "qwen3-8b-instruct"},
    )
    resp = adapter.complete(req)

    assert resp.output_text == "the answer"
    assert resp.reasoning_content == "chain of thought"
    assert resp.usage is not None and resp.usage.total_tokens == 14
    assert resp.stop_reason == StopReason.LENGTH_LIMIT


def test_complete_maps_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient(
        result={
            "success": True,
            "text": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "lookup", "arguments": '{"q": "x"}'}}
            ],
            "finish_reason": "tool_calls",
            "elapsed_seconds": 0.0,
        }
    )
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="use a tool")],
        model_settings={"model_name": "qwen3-8b-instruct"},
    )
    resp = adapter.complete(req)

    tool_items = [i for i in resp.output_items if isinstance(i, ToolCallRequest)]
    assert len(tool_items) == 1
    assert tool_items[0].tool_name == "lookup"
    assert resp.stop_reason == StopReason.TOOL_CALLS


# --------------------------------------------------------------------------- #
# Adapter: sampling + tool gating (request -> client kwargs)
# --------------------------------------------------------------------------- #


def test_sampling_params_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient()
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={
            "model_name": "qwen3-8b-instruct",
            "top_p": 0.5,
            "top_k": 40,
            "repeat_penalty": 1.1,
            "seed": 7,
            "stop": ["DONE"],
            "enable_thinking": False,
        },
    )
    adapter.complete(req)

    for key, value in {
        "top_p": 0.5,
        "top_k": 40,
        "repeat_penalty": 1.1,
        "seed": 7,
        "stop": ["DONE"],
        "enable_thinking": False,
    }.items():
        assert stub.last_kwargs.get(key) == value


def test_local_adapter_uses_bounded_default_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chat-demo callers omit max_tokens; local defaults must not run to 8000 tokens."""
    stub = _StubClient()
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "llama-3.1-8b-instruct"},
    )
    adapter.complete(req)

    assert stub.last_max_tokens == 1024


def test_local_adapter_preserves_explicit_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient(events=[{"type": "final", "finish_reason": "stop"}])
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "llama-3.1-8b-instruct", "max_tokens": 64},
    )
    list(adapter.stream(req))

    assert stub.last_max_tokens == 64


def test_tools_forwarded_when_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient()
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "qwen3-8b-instruct"},
        tools=[
            CanonicalToolSchema(
                name="get_weather",
                description="Get weather",
                json_schema={"type": "object", "properties": {"city": {"type": "string"}}},
            )
        ],
    )
    adapter.complete(req)

    forwarded = stub.last_kwargs.get("tools")
    assert forwarded and forwarded[0]["function"]["name"] == "get_weather"


def test_tools_dropped_when_not_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    # gemma-3-4b does not advertise CAP_TOOL_USE -> tools must be dropped (degrade).
    stub = _StubClient()
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "gemma-3-4b"},
        tools=[CanonicalToolSchema(name="t", description="", json_schema={"type": "object"})],
    )
    adapter.complete(req)

    assert "tools" not in stub.last_kwargs


def test_capability_reports_tool_and_reasoning_support() -> None:
    adapter = LocalAdapter(provider="local", adapter_name="local")
    qwen = adapter.capabilities(model="qwen3-8b-instruct")
    assert qwen.supports_tools is True
    assert qwen.supports_reasoning is True

    gemma4_e4b = adapter.capabilities(model="gemma-4-e4b")
    assert gemma4_e4b.supports_tools is True
    assert gemma4_e4b.supports_reasoning is True

    # Gemma 4 26B-A4B finalizes tool results, but the local GGUF path does not
    # expose the template's enable_thinking variable through llama-cpp-python.
    gemma4_26b = adapter.capabilities(model="gemma-4-26b-a4b")
    assert gemma4_26b.supports_tools is True
    assert gemma4_26b.supports_reasoning is False

    hermes = adapter.capabilities(model="hermes-4-14b")
    assert hermes.supports_tools is True
    assert hermes.supports_reasoning is True

    # phi-4-mini is tool-capable via system-message injection (ADR-0115).
    phi = adapter.capabilities(model="phi-4-mini")
    assert phi.supports_tools is True

    # gemma-3-4b remains non-tool-capable (format too inconsistent to enable).
    gemma = adapter.capabilities(model="gemma-3-4b")
    assert gemma.supports_tools is False


# --------------------------------------------------------------------------- #
# Adapter: streaming canonical events
# --------------------------------------------------------------------------- #


def test_stream_routes_thinking_and_text_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"type": "text", "text": "<think>reason"},
        {"type": "text", "text": "ing</think>visible answer"},
        {"type": "final", "finish_reason": "stop", "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
    ]
    stub = _StubClient(events=events)
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "qwen3-8b-instruct"},
    )
    out = list(adapter.stream(req))

    thinking = "".join(e.text for e in out if isinstance(e, ThinkingEvent))
    text = "".join(e.text for e in out if isinstance(e, TextDeltaEvent))
    assert thinking == "reasoning"
    assert text == "visible answer"
    assert any(isinstance(e, UsageEvent) for e in out)
    stop = [e for e in out if isinstance(e, StopEvent)]
    assert stop and stop[-1].reason == StopReason.NATURAL_STOP


def test_stream_tool_calls_emit_events_and_tool_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        {"type": "tool_call_delta", "index": 0, "call_id": "c1", "tool_name": "f", "arguments_delta": '{"a":'},
        {"type": "tool_call_delta", "index": 0, "call_id": "c1", "tool_name": "f", "arguments_delta": "1}"},
        {"type": "tool_call_complete", "index": 0, "call_id": "c1", "tool_name": "f", "arguments_json": '{"a":1}'},
        {"type": "final", "finish_reason": "tool_calls", "usage": None},
    ]
    stub = _StubClient(events=events)
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "qwen3-8b-instruct"},
    )
    out = list(adapter.stream(req))

    assert any(isinstance(e, ToolCallDeltaEvent) for e in out)
    complete = [e for e in out if isinstance(e, ToolCallCompleteEvent)]
    assert complete and complete[0].arguments_json == '{"a":1}'
    stop = [e for e in out if isinstance(e, StopEvent)]
    assert stop and stop[-1].reason == StopReason.TOOL_CALLS


def test_stream_legacy_string_tokens_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient(events=["hello ", "world"])
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "phi-4-mini"},
    )
    out = list(adapter.stream(req))
    text = "".join(e.text for e in out if isinstance(e, TextDeltaEvent))
    assert text == "hello world"
    assert any(isinstance(e, StopEvent) for e in out)


def test_stream_emits_thinking_complete_on_transition_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # UIs key "done thinking" off is_complete=True (spinner stays on otherwise).
    stub = _StubClient(events=[
        {"type": "text", "text": "<think>plan"},
        {"type": "text", "text": "ning</think>answer"},
        {"type": "final", "finish_reason": "stop", "usage": None},
    ])
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "qwen3-8b-instruct"},
    )
    out = list(adapter.stream(req))

    thinking = [e for e in out if isinstance(e, ThinkingEvent)]
    completes = [e for e in thinking if e.is_complete]
    assert len(completes) == 1
    # Completion arrives before the first text delta.
    first_text_idx = next(i for i, e in enumerate(out) if isinstance(e, TextDeltaEvent))
    complete_idx = next(i for i, e in enumerate(out) if isinstance(e, ThinkingEvent) and e.is_complete)
    assert complete_idx < first_text_idx


def test_stream_emits_thinking_complete_before_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Manager-classified thinking (tool turns) followed by a recovered tool call.
    stub = _StubClient(events=[
        {"type": "thinking", "text": "let me check the weather"},
        {"type": "tool_call_complete", "index": 0, "call_id": "c1", "tool_name": "get_weather", "arguments_json": '{"city": "SF"}'},
        {"type": "final", "finish_reason": "tool_calls", "usage": None},
    ])
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="weather?")],
        model_settings={"model_name": "qwen3-8b-instruct"},
    )
    out = list(adapter.stream(req))

    complete_idx = next(i for i, e in enumerate(out) if isinstance(e, ThinkingEvent) and e.is_complete)
    tool_idx = next(i for i, e in enumerate(out) if isinstance(e, ToolCallCompleteEvent))
    assert complete_idx < tool_idx


def test_stream_emits_thinking_complete_for_thinking_only_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # Thinking-only (or truncated-in-think) stream still closes before stop.
    stub = _StubClient(events=[
        {"type": "thinking", "text": "pondering forever"},
        {"type": "final", "finish_reason": "length", "usage": None},
    ])
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "qwen3-8b-instruct"},
    )
    out = list(adapter.stream(req))

    complete_idx = next(i for i, e in enumerate(out) if isinstance(e, ThinkingEvent) and e.is_complete)
    stop_idx = next(i for i, e in enumerate(out) if isinstance(e, StopEvent))
    assert complete_idx < stop_idx


def test_stream_no_thinking_emits_no_completion_event(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient(events=[
        {"type": "text", "text": "plain answer"},
        {"type": "final", "finish_reason": "stop", "usage": None},
    ])
    _patch_client(monkeypatch, stub)

    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "qwen3-8b-instruct"},
    )
    out = list(adapter.stream(req))
    assert not any(isinstance(e, ThinkingEvent) for e in out)


# --------------------------------------------------------------------------- #
# ToolCallStreamGate: incremental streaming with tool-markup withholding
# --------------------------------------------------------------------------- #


def test_gate_plain_text_streams_through() -> None:
    gate = ToolCallStreamGate()
    emitted = [gate.feed(c) for c in ["Hello", " there", ", how are you?"]]
    tail, held = gate.flush()
    assert "".join(emitted) + tail == "Hello there, how are you?"
    assert held == ""


def test_gate_holds_from_qwen_sentinel_onward() -> None:
    gate = ToolCallStreamGate()
    emitted = []
    emitted.append(gate.feed("Let me check. "))
    emitted.append(gate.feed('<tool_call>{"name": "get_weather", "arguments": {"city": "SF"}}'))
    emitted.append(gate.feed("</tool_call>"))
    tail, held = gate.flush()
    assert "".join(emitted) == "Let me check. "
    assert tail == ""
    assert held.startswith("<tool_call>")
    # Held markup parses into a tool call.
    clean, calls = extract_tool_calls_from_text(held, tool_names=["get_weather"])
    assert len(calls) == 1 and calls[0]["function"]["name"] == "get_weather"
    assert clean == ""


def test_gate_partial_sentinel_across_chunks_never_leaks() -> None:
    gate = ToolCallStreamGate()
    emitted = []
    emitted.append(gate.feed("Sure. "))
    emitted.append(gate.feed("<tool_"))  # could be <tool_call> — must be withheld
    emitted.append(gate.feed('call>{"name": "f", "arguments": {}}</tool_call>'))
    tail, held = gate.flush()
    assert "".join(emitted) == "Sure. "
    assert "<tool_" not in "".join(emitted)
    assert held.startswith("<tool_call>")


def test_gate_partial_sentinel_resolving_as_text_is_emitted() -> None:
    gate = ToolCallStreamGate()
    emitted = []
    emitted.append(gate.feed("the <tool"))   # holds "<tool" as potential sentinel
    emitted.append(gate.feed(" was used"))   # resolves: plain text
    tail, held = gate.flush()
    assert "".join(emitted) + tail == "the <tool was used"
    assert held == ""


def test_gate_pending_partial_sentinel_returned_at_flush() -> None:
    gate = ToolCallStreamGate()
    emitted = gate.feed("ends with <tool_ca")
    tail, held = gate.flush()
    assert emitted + tail == "ends with <tool_ca"
    assert held == ""


def test_gate_holds_bare_json_object_from_start() -> None:
    # Llama 3.1 emits the tool call as the entire content: a bare JSON object.
    gate = ToolCallStreamGate()
    emitted = []
    emitted.append(gate.feed('  {"name": "lookup",'))
    emitted.append(gate.feed(' "parameters": {"id": 5}}'))
    tail, held = gate.flush()
    assert "".join(emitted) == "" and tail == ""
    _, calls = extract_tool_calls_from_text(held)
    assert len(calls) == 1 and calls[0]["function"]["name"] == "lookup"


def test_gate_holds_bare_json_array_from_start() -> None:
    # phi-4 emits a bare JSON array of tool calls.
    gate = ToolCallStreamGate()
    gate.feed('[{"name": "get_weather", "arguments": {"city": "SF"}}]')
    tail, held = gate.flush()
    assert tail == ""
    _, calls = extract_tool_calls_from_text(held)
    assert len(calls) == 1


def test_gate_mistral_sentinel() -> None:
    gate = ToolCallStreamGate()
    emitted = gate.feed('On it. [TOOL_CALLS][{"name": "f", "arguments": {}}]')
    tail, held = gate.flush()
    assert emitted == "On it. "
    assert held.startswith("[TOOL_CALLS]")


def test_gate_whitespace_then_text_streams() -> None:
    gate = ToolCallStreamGate()
    first = gate.feed("  \n")
    second = gate.feed("Hello!")
    tail, held = gate.flush()
    assert first == "" and (second + tail) == "  \nHello!"
    assert held == ""


# --------------------------------------------------------------------------- #
# Manager stream path: incremental streaming with tools requested (model-free)
# --------------------------------------------------------------------------- #


class _FakeLlama:
    """Stub of llama-cpp's model exposing create_chat_completion(stream=True)."""

    def __init__(self, tokens: List[str], finish_reason: str = "stop") -> None:
        self._tokens = tokens
        self._finish = finish_reason

    def create_chat_completion(self, messages: Any, stream: bool = False, **kwargs: Any):
        assert stream is True
        for tok in self._tokens:
            yield {"choices": [{"delta": {"content": tok}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": self._finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def _manager() -> Any:
    from motet.core.models.local.inference_manager import LocalInferenceManager

    return LocalInferenceManager.__new__(LocalInferenceManager)


_STREAM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


def test_manager_stream_with_tools_streams_text_incrementally() -> None:
    # Regression: with tools requested, plain text must stream as it is
    # generated (multiple text events), not buffer into one end-of-stream blob.
    tokens = ["<think>plan", "ning</think>", "Hello", " there", "!"]
    mgr = _manager()
    req = {
        "model_id": "qwen3-8b-instruct",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": _STREAM_TOOLS,
        "max_tokens": 64,
    }
    events = list(mgr._run_llama_cpp_inference_stream(_FakeLlama(tokens), req))

    text_events = [e for e in events if e.get("type") == "text"]
    thinking = "".join(e["text"] for e in events if e.get("type") == "thinking")
    assert len(text_events) >= 2, f"text did not stream incrementally: {events}"
    assert "".join(e["text"] for e in text_events) == "Hello there!"
    assert "planning" in thinking
    final = [e for e in events if e.get("type") == "final"]
    assert final and final[0]["finish_reason"] == "stop"


def test_manager_stream_with_tools_recovers_text_tool_call() -> None:
    tokens = ["Checking. ", "<tool_call>", '{"name": "get_weather", "arguments": {"city": "SF"}}', "</tool_call>"]
    mgr = _manager()
    req = {
        "model_id": "qwen3-8b-instruct",
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": _STREAM_TOOLS,
        "max_tokens": 64,
    }
    events = list(mgr._run_llama_cpp_inference_stream(_FakeLlama(tokens), req))

    text = "".join(e["text"] for e in events if e.get("type") == "text")
    assert text == "Checking. "          # markup never leaked as text
    completes = [e for e in events if e.get("type") == "tool_call_complete"]
    assert len(completes) == 1 and completes[0]["tool_name"] == "get_weather"
    final = [e for e in events if e.get("type") == "final"]
    assert final and final[0]["finish_reason"] == "tool_calls"


def test_manager_stream_recovers_prose_first_fenced_tool_call() -> None:
    # Regression (phi-4): the model narrates first and emits the tool call as a
    # ```json block mid-response. The stream gate only withholds markup that
    # *leads* the turn, so this call is never gated -- it must be recovered from
    # the full streamed text at end of stream. The model echoes the provider
    # wire name (``core__http_get_browser``) which maps back to the canonical
    # declared ``core.http_get_browser``.
    browser_tools = [
        {
            "type": "function",
            "function": {
                "name": "core.http_get_browser",
                "description": "Fetch a URL with a browser.",
                "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
            },
        }
    ]
    tokens = [
        'I will use the "core__http_get_browser" tool to read the page.\n\n',
        "```json\n",
        '{\n  "name": "core__http_get_browser",\n',
        '  "parameters": {"url": "https://www.cnn.com"}\n}\n',
        "```\n\n",
        "It runs headless and extracts the HTML.",
    ]
    mgr = _manager()
    req = {
        "model_id": "phi-4-mini",
        "messages": [{"role": "user", "content": "browse cnn.com and read it to me"}],
        "tools": browser_tools,
        "max_tokens": 256,
    }
    events = list(mgr._run_llama_cpp_inference_stream(_FakeLlama(tokens), req))

    completes = [e for e in events if e.get("type") == "tool_call_complete"]
    assert len(completes) == 1, f"tool call not recovered: {events}"
    assert completes[0]["tool_name"] == "core.http_get_browser"
    assert "cnn.com" in completes[0]["arguments_json"]
    text = "".join(e["text"] for e in events if e.get("type") == "text")
    assert "```json" not in text
    assert '"parameters"' not in text
    assert "It runs headless" not in text
    final = [e for e in events if e.get("type") == "final"]
    assert final and final[0]["finish_reason"] == "tool_calls"


def test_manager_stream_prose_answer_not_misrecovered_as_tool_call() -> None:
    # Guard: a plain prose answer (no declared tool name) must NOT be turned into
    # a tool call by the full-text recovery fallback.
    tokens = ["The capital ", "of France ", "is Paris."]
    mgr = _manager()
    req = {
        "model_id": "phi-4-mini",
        "messages": [{"role": "user", "content": "capital of france?"}],
        "tools": _STREAM_TOOLS,
        "max_tokens": 64,
    }
    events = list(mgr._run_llama_cpp_inference_stream(_FakeLlama(tokens), req))
    assert not [e for e in events if e.get("type") == "tool_call_complete"]
    final = [e for e in events if e.get("type") == "final"]
    assert final and final[0]["finish_reason"] == "stop"
    text = "".join(e["text"] for e in events if e.get("type") == "text")
    assert text == "The capital of France is Paris."


def test_manager_stream_without_tools_unchanged() -> None:
    tokens = ["Hello", " world"]
    mgr = _manager()
    req = {
        "model_id": "qwen3-8b-instruct",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 64,
    }
    events = list(mgr._run_llama_cpp_inference_stream(_FakeLlama(tokens), req))
    text_events = [e for e in events if e.get("type") == "text"]
    # No-tools path passes raw tokens through (think tags routed adapter-side).
    assert [e["text"] for e in text_events] == ["Hello", " world"]


# --------------------------------------------------------------------------- #
# Assistant tool-call turn fidelity (ADR-0115): local GGUF templates render only
# role/content and ignore structured tool_calls, so an assistant turn that only
# carried tool calls must echo them as text or the following tool result orphans.
# --------------------------------------------------------------------------- #


def test_message_to_text_synthesizes_assistant_tool_call_when_content_empty() -> None:
    from motet.core.models.adapters.providers.local import _message_to_text

    msg = Message(
        role="assistant",
        content="",
        tool_calls_canonical=[{
            "call_id": "c1",
            "tool_name": "core.http_get_browser",
            "arguments": {"url": "cnn.com"},
            "arguments_json": '{"url": "cnn.com"}',
        }],
    )
    rendered = _message_to_text(msg)
    assert rendered == '[{"name": "core.http_get_browser", "arguments": {"url": "cnn.com"}}]'


def test_message_to_text_preserves_nonempty_assistant_content() -> None:
    from motet.core.models.adapters.providers.local import _message_to_text

    msg = Message(
        role="assistant",
        content="Let me check that.",
        tool_calls_canonical=[{"call_id": "c1", "tool_name": "core.http_get_browser", "arguments_json": '{"url": "cnn.com"}', "arguments": {"url": "cnn.com"}}],
    )
    # Existing content wins; we only synthesize for an otherwise-empty assistant turn.
    assert _message_to_text(msg) == "Let me check that."


def test_message_to_text_empty_assistant_without_tool_calls_stays_empty() -> None:
    from motet.core.models.adapters.providers.local import _message_to_text

    assert _message_to_text(Message(role="assistant", content="")) == ""


def test_message_to_text_tool_role_content_unchanged() -> None:
    from motet.core.models.adapters.providers.local import _message_to_text

    # A tool-result turn must pass through verbatim (no synthesis on non-assistant roles).
    msg = Message(role="tool", content="Top story: ...", tool_call_id="c1", name="core.http_get_browser")
    assert _message_to_text(msg) == "Top story: ..."


@pytest.mark.parametrize(
    "tool_call,expected",
    [
        ({"tool_name": "t", "arguments": {"a": 1}}, {"name": "t", "arguments": {"a": 1}}),
        ({"name": "t", "arguments": {"a": 1}}, {"name": "t", "arguments": {"a": 1}}),
        ({"function": {"name": "t", "arguments": '{"a": 1}'}}, {"name": "t", "arguments": {"a": 1}}),
        ({"tool_name": "t", "arguments_json": '{"a": 1}'}, {"name": "t", "arguments": {"a": 1}}),
        ({"tool_name": "t"}, {"name": "t", "arguments": {}}),
        ({"arguments": {"a": 1}}, None),  # no name -> dropped
    ],
)
def test_tool_call_name_and_args_shapes(tool_call: Dict[str, Any], expected: Any) -> None:
    from motet.core.models.adapters.providers.local import _tool_call_name_and_args

    assert _tool_call_name_and_args(tool_call) == expected


# --------------------------------------------------------------------------- #
# Tool-result ownership instruction (ADR-0115): when a turn ends on a tool
# result, the adapter appends a user turn attributing the result to the model's
# own tool call so small local models don't disown it ("I can't browse").
# --------------------------------------------------------------------------- #


def _format_for(messages: List[Message]) -> List[Dict[str, Any]]:
    adapter = LocalAdapter(provider="local", adapter_name="local")
    req = LLMRequest(messages=messages, model_settings={"model_name": "phi-4-mini"})
    return adapter._format_messages(req, "phi-4-mini")


def test_trailing_tool_result_gets_ownership_instruction() -> None:
    formatted = _format_for([
        Message(role="user", content="browse cnn.com and read it to me"),
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[{"call_id": "c1", "tool_name": "core.http_get_browser", "arguments_json": '{"url": "cnn.com"}', "arguments": {"url": "cnn.com"}}],
        ),
        Message(role="tool", content="Top story: ...", tool_call_id="c1", name="core.http_get_browser"),
    ])

    assert formatted[-1]["role"] == "user"
    assert "core.http_get_browser" in formatted[-1]["content"]
    assert "never claim you cannot browse" in formatted[-1]["content"]
    # The tool turn itself passes through verbatim, right before the instruction.
    assert formatted[-2]["role"] == "tool"
    assert formatted[-2]["content"] == "Top story: ..."


def test_trailing_tool_result_without_name_uses_generic_label() -> None:
    formatted = _format_for([
        Message(role="user", content="fetch it"),
        Message(role="tool", content="result", tool_call_id="c1"),
    ])

    assert formatted[-1]["role"] == "user"
    assert "your own tool call" in formatted[-1]["content"]


def test_multiple_trailing_tool_results_get_single_instruction() -> None:
    formatted = _format_for([
        Message(role="user", content="check both"),
        Message(role="tool", content="a", tool_call_id="c1", name="core.http_get"),
        Message(role="tool", content="b", tool_call_id="c2", name="core.web_search"),
    ])

    assert [m["role"] for m in formatted] == ["user", "tool", "tool", "user"]
    assert "core.http_get" in formatted[-1]["content"]
    assert "core.web_search" in formatted[-1]["content"]


def test_intermediate_tool_result_gets_no_instruction() -> None:
    # A tool result already answered by a later assistant turn must not be reframed.
    formatted = _format_for([
        Message(role="user", content="browse cnn.com"),
        Message(role="tool", content="Top story: ...", tool_call_id="c1", name="core.http_get_browser"),
        Message(role="assistant", content="The top story is ..."),
        Message(role="user", content="thanks, anything else?"),
    ])

    assert [m["role"] for m in formatted] == ["user", "tool", "assistant", "user"]
    assert formatted[-1]["content"] == "thanks, anything else?"


def test_no_tool_messages_leaves_conversation_unchanged() -> None:
    formatted = _format_for([
        Message(role="system", content="be helpful"),
        Message(role="user", content="hi"),
    ])

    assert formatted == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
    ]
