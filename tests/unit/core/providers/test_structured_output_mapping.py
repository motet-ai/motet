"""
Motet - Structured Output Mapping Tests (ADR-0114)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-10

Description:
Unit tests for mapping the canonical LLMRequest.output_contract onto each
provider adapter's structured-output mechanism (ADR-0114):

- Moonshot (OpenAI-compatible): response_format (json_schema / json_object).
- OpenAI Chat Completions: response_format (json_schema / json_object).
- OpenAI Responses: text.format — flattened {type, name, strict, schema}
  (name is required by the API; this differs from response_format's nesting).
- Gemini: GenerateContentConfig response_mime_type + response_json_schema
  (requires the google-genai SDK; skipped when not installed).
- Anthropic: forced tool-use (a single tool whose input_schema is the contract
  schema), unwrapped back into JSON text.

These tests cover the pure mapping helpers + capability flags and (except the
Gemini config test) do not require the provider SDKs or network access.

Dependencies:
- pytest: Test framework
- motet.core.types: LLMRequest, OutputContract, ToolCallRequest
- motet.core.models.adapters.providers: anthropic_messages,
  moonshot_chat_completions, openai_chat_completions, openai_responses,
  gemini_generate_content

Usage:
pytest tests/unit/core/providers/test_structured_output_mapping.py
"""

from __future__ import annotations

import pytest

from motet.core.types import LLMRequest, Message, OutputContract, ToolCallRequest
from motet.core.models.adapters.providers import anthropic_messages as anth
from motet.core.models.adapters.providers import moonshot_chat_completions as moon
from motet.core.models.adapters.providers import openai_chat_completions as oai_cc
from motet.core.models.adapters.providers import openai_responses as oai_resp


_SCHEMA = {
    "type": "object",
    "properties": {"component": {"type": "string"}, "title": {"type": "string"}},
    "required": ["component", "title"],
}


def _req(contract: OutputContract | None, *, tools=None) -> LLMRequest:
    return LLMRequest(
        messages=[Message(role="user", content="render a card")],
        model_settings={"model_name": "x"},
        output_contract=contract,
        tools=tools,
    )


# --------------------------------------------------------------------------- #
# Moonshot: response_format mapping
# --------------------------------------------------------------------------- #


def test_moonshot_response_format_json_schema_when_schema_present() -> None:
    rf = moon._build_response_format(_req(OutputContract(format="json", json_schema=_SCHEMA, strict=True)))
    assert rf == {
        "type": "json_schema",
        "json_schema": {"name": "structured_output", "schema": _SCHEMA, "strict": True},
    }


def test_moonshot_response_format_json_object_without_schema() -> None:
    rf = moon._build_response_format(_req(OutputContract(format="json")))
    assert rf == {"type": "json_object"}


def test_moonshot_response_format_none_for_text_or_missing() -> None:
    assert moon._build_response_format(_req(None)) is None
    assert moon._build_response_format(_req(OutputContract(format="text"))) is None


def test_moonshot_advertises_structured_output() -> None:
    adapter = moon.MoonshotChatCompletionsAdapter(provider="moonshot", adapter_name="chat_completions")
    assert adapter.capabilities(model="kimi-k2.5").supports_json_schema_strict is True


# --------------------------------------------------------------------------- #
# OpenAI Chat Completions: response_format mapping
# --------------------------------------------------------------------------- #


def test_openai_cc_response_format_json_schema_when_schema_present() -> None:
    rf = oai_cc._build_response_format(_req(OutputContract(format="json", json_schema=_SCHEMA, strict=True)))
    assert rf == {
        "type": "json_schema",
        "json_schema": {"name": "structured_output", "schema": _SCHEMA, "strict": True},
    }


def test_openai_cc_response_format_json_object_without_schema() -> None:
    rf = oai_cc._build_response_format(_req(OutputContract(format="json")))
    assert rf == {"type": "json_object"}


def test_openai_cc_response_format_none_for_text_or_missing() -> None:
    assert oai_cc._build_response_format(_req(None)) is None
    assert oai_cc._build_response_format(_req(OutputContract(format="text"))) is None


def test_openai_cc_advertises_structured_output() -> None:
    adapter = oai_cc.OpenAIChatCompletionsAdapter(provider="openai", adapter_name="chat_completions")
    assert adapter.capabilities(model="gpt-4o").supports_json_schema_strict is True


# --------------------------------------------------------------------------- #
# OpenAI Responses: text.format mapping (flattened shape; name required)
# --------------------------------------------------------------------------- #


def test_openai_responses_text_format_flattened_json_schema() -> None:
    tf = oai_resp._build_text_format(_req(OutputContract(format="json", json_schema=_SCHEMA, strict=True)))
    # The Responses API requires the flattened shape with a mandatory `name`;
    # the Chat Completions nesting ({"json_schema": {...}}) is rejected.
    assert tf == {
        "format": {
            "type": "json_schema",
            "name": "structured_output",
            "strict": True,
            "schema": _SCHEMA,
        }
    }


def test_openai_responses_text_format_json_object_without_schema() -> None:
    tf = oai_resp._build_text_format(_req(OutputContract(format="json")))
    assert tf == {"format": {"type": "json_object"}}


def test_openai_responses_text_format_none_for_text_or_missing() -> None:
    assert oai_resp._build_text_format(_req(None)) is None
    assert oai_resp._build_text_format(_req(OutputContract(format="text"))) is None


def test_openai_responses_advertises_structured_output() -> None:
    adapter = oai_resp.OpenAIResponsesAdapter(provider="openai", adapter_name="responses")
    assert adapter.capabilities(model="gpt-4o").supports_json_schema_strict is True


# --------------------------------------------------------------------------- #
# Gemini: GenerateContentConfig mapping (requires google-genai SDK)
# --------------------------------------------------------------------------- #


def test_gemini_config_maps_json_schema() -> None:
    pytest.importorskip("google.genai")
    from motet.core.models.adapters.providers import gemini_generate_content as gem

    cfg = gem._build_config(
        settings={},
        sys_instruction=None,
        gemini_tools=None,
        output_contract=OutputContract(format="json", json_schema=_SCHEMA),
    )
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == _SCHEMA


def test_gemini_config_json_mode_without_schema() -> None:
    pytest.importorskip("google.genai")
    from motet.core.models.adapters.providers import gemini_generate_content as gem

    cfg = gem._build_config(
        settings={},
        sys_instruction=None,
        gemini_tools=None,
        output_contract=OutputContract(format="json"),
    )
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema is None


def test_gemini_config_unconstrained_for_text_or_missing() -> None:
    pytest.importorskip("google.genai")
    from motet.core.models.adapters.providers import gemini_generate_content as gem

    for contract in (None, OutputContract(format="text")):
        cfg = gem._build_config(
            settings={},
            sys_instruction=None,
            gemini_tools=None,
            output_contract=contract,
        )
        assert cfg.response_mime_type is None
        assert cfg.response_json_schema is None


# --------------------------------------------------------------------------- #
# Anthropic: forced-tool mapping + unwrap
# --------------------------------------------------------------------------- #


def test_anthropic_structured_tool_built_from_schema() -> None:
    tool = anth._structured_output_tool(_req(OutputContract(format="json", json_schema=_SCHEMA, strict=True)))
    assert tool is not None
    assert tool["name"] == anth._STRUCTURED_TOOL_NAME
    assert tool["input_schema"] == _SCHEMA


def test_anthropic_structured_tool_none_for_text_or_schemaless() -> None:
    assert anth._structured_output_tool(_req(None)) is None
    assert anth._structured_output_tool(_req(OutputContract(format="text"))) is None
    assert anth._structured_output_tool(_req(OutputContract(format="json"))) is None


def test_anthropic_extract_forced_tool_json() -> None:
    calls = [
        ToolCallRequest(call_id="1", tool_name="other", arguments_json='{"a":1}'),
        ToolCallRequest(call_id="2", tool_name=anth._STRUCTURED_TOOL_NAME, arguments_json='{"component":"list","title":"T"}'),
    ]
    assert anth._extract_forced_tool_json(calls) == '{"component":"list","title":"T"}'
    assert anth._extract_forced_tool_json([]) is None


def test_anthropic_advertises_structured_output() -> None:
    adapter = anth.AnthropicMessagesAdapter(provider="anthropic", adapter_name="messages")
    assert adapter.capabilities(model="claude-sonnet-4-6").supports_json_schema_strict is True
