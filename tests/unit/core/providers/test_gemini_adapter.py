"""Unit tests for Gemini native adapter (no network)."""

from __future__ import annotations

import pytest

from motet.core.models.adapters.providers.gemini_generate_content import (
    GeminiGenerateContentAdapter,
    _finish_to_stop,
    _format_gemini_contents,
    _parse_response,
)
from motet.core.types import Message, StopReason, ToolCallRequest


def test_select_adapter_registered() -> None:
    from motet.core.models.adapters import adapter_registry

    assert adapter_registry.supports("gemini", "generate_content")


def test_parse_response_text_and_tool_calls() -> None:
    raw = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "Hello"},
                        {"function_call": {"name": "fn", "args": {"x": 1}}},
                    ],
                },
                "finish_reason": "STOP",
            }
        ],
        "usage_metadata": {
            "prompt_token_count": 10,
            "candidates_token_count": 5,
            "total_token_count": 15,
        },
    }
    text, tools, stop, usage = _parse_response(raw)
    assert text == "Hello"
    assert stop == StopReason.TOOL_CALLS
    assert len(tools) == 1
    assert isinstance(tools[0], ToolCallRequest)
    assert tools[0].tool_name == "fn"
    assert tools[0].call_id == "gemini_fc_1"
    assert usage is not None
    assert usage.prompt_tokens == 10
    assert usage.output_tokens == 5


def test_parse_response_converts_mcp_wire_name() -> None:
    raw = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"function_call": {"name": "mcp__test__add_two_numbers", "args": {"a": 7}}},
                    ],
                },
                "finish_reason": "STOP",
            }
        ],
    }
    _text, tools, _stop, _usage = _parse_response(raw)
    assert len(tools) == 1
    assert tools[0].tool_name == "mcp.test.add_two_numbers"


def test_format_contents_coalesces_tool_messages() -> None:
    pytest.importorskip("google.genai", reason="google-genai optional in lightweight test environments")
    sys_text, contents = _format_gemini_contents(
        messages=[
            Message(role="system", content="sys"),
            Message(role="user", content="hi"),
            Message(role="assistant", content="", tool_calls_canonical=[{"tool_name": "t", "arguments_json": '{"a": 1}', "call_id": "gemini_fc_0"}]),
            Message(role="tool", tool_call_id="gemini_fc_0", name="t", content='{"ok": true}'),
        ],
        request_context=None,
    )
    assert sys_text == "sys"
    assert len(contents) == 3


def test_capabilities_from_registry() -> None:
    ad = GeminiGenerateContentAdapter(provider="gemini", adapter_name="generate_content", credentials={"gemini_api_key": "x"})
    caps = ad.capabilities(model="gemini-2.5-pro")
    assert caps.supports_streaming is True
    assert caps.supports_tool_call_id is False
    assert caps.supports_reasoning is True


def test_registry_includes_gemini_3_preview_models() -> None:
    from motet.core.models.registry import get_model_spec

    assert get_model_spec("gemini", "gemini-3-flash-preview") is not None
    assert get_model_spec("gemini", "gemini-3.1-pro-preview") is not None
    assert get_model_spec("gemini", "gemini-3.1-flash-lite-preview") is not None
    assert get_model_spec("gemini", "gemini-2.5-flash-lite") is not None


def test_format_contents_translates_openai_shaped_tool_calls_to_function_call_parts() -> None:
    """Canonical history stores tool_calls_canonical; adapters render function_call parts."""
    pytest.importorskip("google.genai", reason="google-genai optional in lightweight test environments")
    sys_text, contents = _format_gemini_contents(
        messages=[
            Message(
                role="assistant",
                content="",
                tool_calls_canonical=[
                    {
                        "call_id": "call_1",
                        "tool_name": "mcp__acme__do_thing",
                        "arguments_json": '{"x": 1}',
                    }
                ],
            ),
            Message(role="tool", tool_call_id="call_1", name="mcp__acme__do_thing", content='{"ok": true}'),
        ],
        request_context=None,
    )
    assert sys_text is None
    assert len(contents) == 2
    model_dump = contents[0].model_dump(mode="json", by_alias=False)
    assert model_dump.get("role") == "model"
    parts = model_dump.get("parts") or []
    assert len(parts) == 1
    fc = parts[0].get("function_call") or {}
    assert fc.get("name") == "mcp__acme__do_thing"
    assert fc.get("args") == {"x": 1}


def test_format_contents_sorts_tool_results_by_synthetic_gemini_call_index() -> None:
    """ADR-0064 R4: functionResponse parts must follow gemini_fc_<n> order."""
    pytest.importorskip("google.genai", reason="google-genai optional in lightweight test environments")
    sys_text, contents = _format_gemini_contents(
        messages=[
            Message(role="user", content="hi"),
            Message(
                role="assistant",
                content="",
                tool_calls_canonical=[
                    {"tool_name": "t", "arguments_json": '{"i": 0}', "arguments": {"i": 0}, "call_id": "gemini_fc_0"},
                    {"tool_name": "t", "arguments_json": '{"i": 1}', "arguments": {"i": 1}, "call_id": "gemini_fc_1"},
                ],
            ),
            # Deliberately out-of-order tool messages
            Message(role="tool", tool_call_id="gemini_fc_1", name="t", content='{"order": 1}'),
            Message(role="tool", tool_call_id="gemini_fc_0", name="t", content='{"order": 0}'),
        ],
        request_context=None,
    )
    assert sys_text is None
    assert len(contents) == 3
    user_tool_turn = contents[-1].model_dump(mode="json", by_alias=False)
    parts = user_tool_turn.get("parts") or []
    assert len(parts) == 2
    r0 = (parts[0].get("function_response") or {}).get("response") or {}
    r1 = (parts[1].get("function_response") or {}).get("response") or {}
    assert r0.get("order") == 0
    assert r1.get("order") == 1


def test_parse_response_max_tokens_maps_to_length_limit() -> None:
    raw = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "done"}]},
                "finish_reason": "MAX_TOKENS",
            }
        ],
    }
    text, tools, stop, _usage = _parse_response(raw)
    assert text == "done"
    assert tools == []
    assert stop == StopReason.LENGTH_LIMIT


def test_finish_to_stop_safety_maps_to_safety_filter() -> None:
    assert _finish_to_stop("SAFETY", has_tool_calls=False) == StopReason.SAFETY_FILTER
    assert _finish_to_stop("MALFORMED_FUNCTION_CALL", has_tool_calls=False) == StopReason.ERROR


def test_finish_to_stop_prefers_tool_calls_when_present() -> None:
    assert _finish_to_stop("STOP", has_tool_calls=True) == StopReason.TOOL_CALLS


def test_parse_response_captures_thought_signature() -> None:
    """Gemini 3+ binds a thought_signature to functionCall parts; capture it verbatim."""
    raw = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "function_call": {"name": "fn", "args": {"x": 1}},
                            "thought_signature": "AQJzaWf_",
                        },
                        {"function_call": {"name": "fn2", "args": {}}},
                    ],
                },
                "finish_reason": "STOP",
            }
        ],
    }
    _text, tools, _stop, _usage = _parse_response(raw)
    assert len(tools) == 2
    assert tools[0].thought_signature == "AQJzaWf_"
    assert tools[1].thought_signature is None


def test_format_contents_replays_thought_signature_on_function_call_part() -> None:
    """Persisted signatures must be re-attached to the Part (Gemini 3+ rejects calls without them)."""
    pytest.importorskip("google.genai", reason="google-genai optional in lightweight test environments")
    import base64

    sig_bytes = b"\x01\x02signature\xff"
    sig_str = base64.urlsafe_b64encode(sig_bytes).decode().rstrip("=")

    _sys, contents = _format_gemini_contents(
        messages=[
            Message(role="user", content="hi"),
            Message(
                role="assistant",
                content="",
                tool_calls_canonical=[
                    {"tool_name": "t", "arguments_json": '{"a": 1}', "arguments": {"a": 1}, "call_id": "gemini_fc_0", "thought_signature": sig_str}
                ],
            ),
            Message(role="tool", tool_call_id="gemini_fc_0", name="t", content='{"ok": true}'),
        ],
        request_context=None,
    )
    assert len(contents) == 3
    part = contents[1].parts[0]
    assert part.function_call is not None
    assert part.thought_signature == sig_bytes


def test_format_contents_tolerates_invalid_thought_signature() -> None:
    """A corrupt signature must not break history rendering; the call is replayed unsigned."""
    pytest.importorskip("google.genai", reason="google-genai optional in lightweight test environments")
    _sys, contents = _format_gemini_contents(
        messages=[
            Message(
                role="assistant",
                content="",
                tool_calls_canonical=[
                    {"tool_name": "t", "arguments_json": "{}", "arguments": {}, "call_id": "gemini_fc_0", "thought_signature": "!!!not-base64!!!"}
                ],
            ),
            Message(role="tool", tool_call_id="gemini_fc_0", name="t", content='{"ok": true}'),
        ],
        request_context=None,
    )
    part = contents[0].parts[0]
    assert part.function_call is not None
    assert part.thought_signature is None
