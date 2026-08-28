"""
Motet - OpenAI Compatible Translation Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-29

Description:
    Unit tests for OpenAI wire <-> canonical translation in the compatibility
    facade (ADR-0125 §5a, §5b, §5f). Covers both request shapes (Chat Completions
    and the Responses shape Cursor posts), multimodal parts, tool name wire
    mapping, structured output contracts, finish_reason mapping, and the explicit
    rejection of parameters Motet cannot honor.

Dependencies:
    - pytest: test runner
    - motet.interfaces.api.openai_compat.translation: system under test

Usage:
    pytest tests/unit/interfaces/api/test_openai_compat_translation.py

Notes:
    - Model resolution tests rely on the built-in registry entry openai/gpt-4o-mini
    - Denied models must be indistinguishable from unknown models to the client
"""

import pytest

from motet.core.security.facade_policy import FacadePolicy
from motet.core.types import MediaPart, TextPart
from motet.interfaces.api.openai_compat import translation
from motet.interfaces.api.openai_compat.errors import FacadeError
from motet.interfaces.api.openai_compat.wire import ChatCompletionRequest

ALLOW_ALL = FacadePolicy(allowed_models=["*"])
ALLOW_NONE = FacadePolicy(allowed_models=[])


def make_request(**kwargs) -> ChatCompletionRequest:
    payload = {"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
    payload.update(kwargs)
    return ChatCompletionRequest.model_validate(payload)


class TestModelResolution:
    """Model ids resolve through the registry under the credential allowlist."""

    def test_qualified_id_resolves(self):
        provider, key, spec = translation.resolve_model("openai/gpt-4o-mini", ALLOW_ALL)
        assert (provider, key) == ("openai", "gpt-4o-mini")
        assert spec is not None

    def test_bare_key_resolves_when_unambiguous(self):
        provider, key, _ = translation.resolve_model("gpt-4o-mini", ALLOW_ALL)
        assert (provider, key) == ("openai", "gpt-4o-mini")

    def test_unknown_model_is_404(self):
        with pytest.raises(FacadeError) as exc:
            translation.resolve_model("openai/not-a-model", ALLOW_ALL)
        assert exc.value.status_code == 404

    def test_denied_model_is_indistinguishable_from_unknown(self):
        with pytest.raises(FacadeError) as exc:
            translation.resolve_model("openai/gpt-4o-mini", ALLOW_NONE)
        assert exc.value.status_code == 404
        assert exc.value.code == "model_not_found"

    def test_missing_model_is_400(self):
        with pytest.raises(FacadeError) as exc:
            translation.resolve_model("", ALLOW_ALL)
        assert exc.value.status_code == 400

    def test_allowed_models_reflects_policy(self):
        entries = translation.allowed_models(FacadePolicy(allowed_models=["openai/gpt-4o-mini"]))
        assert [(p, k) for p, k, _ in entries] == [("openai", "gpt-4o-mini")]


class TestUnsupportedParameters:
    """Parameters the client would not notice being dropped are rejected."""

    def test_multiple_choices_rejected(self):
        with pytest.raises(FacadeError) as exc:
            translation.validate_supported(make_request(n=2))
        assert exc.value.param == "n"

    def test_single_choice_allowed(self):
        translation.validate_supported(make_request(n=1))

    def test_logprobs_rejected(self):
        with pytest.raises(FacadeError) as exc:
            translation.validate_supported(make_request(logprobs=True))
        assert exc.value.param == "logprobs"


class TestMessageTranslation:
    """OpenAI messages become canonical messages without wire leakage."""

    def test_simple_text_message(self):
        messages = translation.messages_to_canonical(make_request())
        assert len(messages) == 1
        assert messages[0].role == "user"
        assert messages[0].content == "hi"
        assert messages[0].content_parts is None

    def test_developer_role_maps_to_system(self):
        req = make_request(messages=[{"role": "developer", "content": "be terse"}])
        assert translation.messages_to_canonical(req)[0].role == "system"

    def test_instructions_become_leading_system_message(self):
        req = make_request(instructions="be terse")
        messages = translation.messages_to_canonical(req)
        assert messages[0].role == "system"
        assert messages[0].content == "be terse"

    def test_multimodal_content_becomes_media_parts(self):
        req = make_request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/a.png", "detail": "high"},
                        },
                    ],
                }
            ]
        )
        message = translation.messages_to_canonical(req)[0]
        assert message.content == "what is this"
        assert message.content_parts is not None
        assert isinstance(message.content_parts[0], TextPart)
        media = message.content_parts[1]
        assert isinstance(media, MediaPart)
        assert media.url == "https://example.com/a.png"
        assert media.mime_type == "image/png"
        assert media.detail == "high"

    def test_data_url_becomes_base64_media(self):
        req = make_request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}
                    ],
                }
            ]
        )
        media = translation.messages_to_canonical(req)[0].content_parts[0]
        assert media.base64_data == "QUJD"
        assert media.mime_type == "image/jpeg"

    def test_tool_names_arrive_in_wire_form_and_become_canonical(self):
        req = make_request(
            messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "mcp__github__list_repos", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "[]"},
            ]
        )
        messages = translation.messages_to_canonical(req)
        assert messages[0].tool_calls_canonical[0].tool_name == "mcp.github.list_repos"
        assert messages[1].role == "tool"
        assert messages[1].tool_call_id == "call_1"

    def test_empty_message_list_is_rejected(self):
        req = ChatCompletionRequest.model_validate({"model": "openai/gpt-4o-mini", "messages": []})
        with pytest.raises(FacadeError) as exc:
            translation.messages_to_canonical(req)
        assert exc.value.status_code == 400


class TestResponsesShape:
    """Cursor posts Responses-shaped bodies to /chat/completions (ADR-0125 §9)."""

    def test_string_input_becomes_user_message(self):
        req = ChatCompletionRequest.model_validate(
            {"model": "openai/gpt-4o-mini", "input": "hello"}
        )
        assert req.is_responses_shaped() is True
        messages = translation.messages_to_canonical(req)
        assert messages[0].role == "user"
        assert messages[0].content == "hello"

    def test_input_item_list_with_function_call_round_trip(self):
        req = ChatCompletionRequest.model_validate(
            {
                "model": "openai/gpt-4o-mini",
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": "run it"}]},
                    {
                        "type": "function_call",
                        "call_id": "call_9",
                        "name": "core__web_search",
                        "arguments": '{"q":"x"}',
                    },
                    {"type": "function_call_output", "call_id": "call_9", "output": "done"},
                ],
            }
        )
        messages = translation.messages_to_canonical(req)
        assert [m.role for m in messages] == ["user", "assistant", "tool"]
        assert messages[1].tool_calls_canonical[0].tool_name == "core.web_search"
        assert messages[2].content == "done"


class TestToolsAndContracts:
    """Tool schemas and structured output map onto canonical types."""

    def test_tools_become_canonical_schemas(self):
        req = make_request(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "mcp__github__create_issue",
                        "description": "create",
                        "parameters": {"type": "object", "properties": {"title": {"type": "string"}}},
                    },
                }
            ]
        )
        schemas = translation.tools_to_canonical(req)
        assert schemas[0].name == "mcp.github.create_issue"
        assert schemas[0].json_schema["properties"]["title"]["type"] == "string"

    def test_tool_choice_none_suppresses_tools(self):
        req = make_request(
            tool_choice="none",
            tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
        )
        assert translation.tools_to_canonical(req) is None

    def test_json_object_response_format(self):
        contract = translation.output_contract_from_request(
            make_request(response_format={"type": "json_object"})
        )
        assert contract.format == "json"
        assert contract.json_schema is None

    def test_json_schema_response_format(self):
        contract = translation.output_contract_from_request(
            make_request(
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "answer",
                        "strict": True,
                        "schema": {"type": "object", "properties": {}},
                    },
                }
            )
        )
        assert contract.format == "json"
        assert contract.strict is True
        assert contract.json_schema == {"type": "object", "properties": {}}

    def test_responses_text_format_is_honored(self):
        req = ChatCompletionRequest.model_validate(
            {
                "model": "openai/gpt-4o-mini",
                "input": "hi",
                "text": {"format": {"type": "json_object"}},
            }
        )
        assert translation.output_contract_from_request(req).format == "json"

    def test_capability_check_rejects_unsupported_tools(self):
        _, _, spec = translation.resolve_model("openai/gpt-image-1", ALLOW_ALL)
        with pytest.raises(FacadeError) as exc:
            translation.capability_check(spec, needs_tools=True, needs_structured=False)
        assert exc.value.code == "unsupported_capability"

    def test_capability_check_passes_for_capable_model(self):
        _, _, spec = translation.resolve_model("openai/gpt-4o-mini", ALLOW_ALL)
        translation.capability_check(spec, needs_tools=True, needs_structured=True)


class TestModelSettings:
    """Sampling parameters reach the inference command."""

    def test_provider_and_model_are_set(self):
        settings = translation.model_settings_from_request(
            make_request(), provider="openai", registry_key="gpt-4o-mini"
        )
        assert settings["provider"] == "openai"
        assert settings["model_name"] == "gpt-4o-mini"

    def test_max_completion_tokens_wins_over_max_tokens(self):
        settings = translation.model_settings_from_request(
            make_request(max_tokens=100, max_completion_tokens=250),
            provider="openai",
            registry_key="gpt-4o-mini",
        )
        assert settings["max_tokens"] == 250

    def test_temperature_forwarded(self):
        settings = translation.model_settings_from_request(
            make_request(temperature=0.2), provider="openai", registry_key="gpt-4o-mini"
        )
        assert settings["temperature"] == 0.2


class TestThinkingOptIn:
    """Client-opted thinking maps to enable_thinking when CAP_REASONING allows it."""

    def test_no_opt_in_returns_none(self):
        assert translation.parse_thinking_opt_in(make_request()) is None

    def test_reasoning_effort_opts_in(self):
        assert translation.parse_thinking_opt_in(make_request(reasoning_effort="high")) == "high"

    def test_reasoning_object_opts_in_with_effort(self):
        assert (
            translation.parse_thinking_opt_in(make_request(reasoning={"effort": "low"})) == "low"
        )

    def test_reasoning_empty_object_defaults_medium(self):
        assert translation.parse_thinking_opt_in(make_request(reasoning={})) == "medium"

    def test_motet_enable_thinking_opts_in(self):
        assert (
            translation.parse_thinking_opt_in(make_request(motet_enable_thinking=True)) == "medium"
        )

    def test_applied_when_model_has_cap_reasoning(self):
        _, _, spec = translation.resolve_model("mock/mock-small", ALLOW_ALL)
        settings = translation.model_settings_from_request(
            make_request(reasoning_effort="high"),
            provider="mock",
            registry_key="mock-small",
            spec=spec,
        )
        assert settings["enable_thinking"] is True
        assert settings["reasoning_effort"] == "high"

    def test_stripped_when_model_lacks_cap_reasoning(self):
        _, _, spec = translation.resolve_model("openai/gpt-4o-mini", ALLOW_ALL)
        settings = translation.model_settings_from_request(
            make_request(reasoning_effort="high"),
            provider="openai",
            registry_key="gpt-4o-mini",
            spec=spec,
        )
        assert "enable_thinking" not in settings
        assert "reasoning_effort" not in settings

    def test_force_thinking_enables_without_client_opt_in(self):
        _, _, spec = translation.resolve_model("mock/mock-small", ALLOW_ALL)
        settings = translation.model_settings_from_request(
            make_request(),
            provider="mock",
            registry_key="mock-small",
            spec=spec,
            force_thinking=True,
            force_thinking_effort="low",
        )
        assert settings["enable_thinking"] is True
        assert settings["reasoning_effort"] == "low"

    def test_client_effort_wins_over_force_thinking_effort(self):
        _, _, spec = translation.resolve_model("mock/mock-small", ALLOW_ALL)
        settings = translation.model_settings_from_request(
            make_request(reasoning_effort="high"),
            provider="mock",
            registry_key="mock-small",
            spec=spec,
            force_thinking=True,
            force_thinking_effort="low",
        )
        assert settings["enable_thinking"] is True
        assert settings["reasoning_effort"] == "high"

    def test_force_thinking_stripped_without_cap_reasoning(self):
        _, _, spec = translation.resolve_model("openai/gpt-4o-mini", ALLOW_ALL)
        settings = translation.model_settings_from_request(
            make_request(),
            provider="openai",
            registry_key="gpt-4o-mini",
            spec=spec,
            force_thinking=True,
        )
        assert "enable_thinking" not in settings


class TestOutboundRendering:
    """Command results render as OpenAI response bodies."""

    def test_completion_payload_shape(self):
        payload = translation.completion_payload(
            {
                "content": "hello",
                "finish_reason": "stop",
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
            model_id="openai/gpt-4o-mini",
        )
        assert payload["object"] == "chat.completion"
        assert payload["model"] == "openai/gpt-4o-mini"
        assert payload["choices"][0]["message"]["content"] == "hello"
        assert payload["choices"][0]["finish_reason"] == "stop"
        assert payload["usage"]["total_tokens"] == 7

    def test_tool_calls_render_in_wire_form_and_force_finish_reason(self):
        result = {
            "content": "",
            "finish_reason": "stop",
            "tool_calls_canonical": [
                {
                    "call_id": "call_1",
                    "tool_name": "mcp.github.list_repos",
                    "arguments_json": '{"org":"x"}',
                }
            ],
        }
        payload = translation.completion_payload(result, model_id="openai/gpt-4o-mini")
        call = payload["choices"][0]["message"]["tool_calls"][0]
        assert call["function"]["name"] == "mcp__github__list_repos"
        assert payload["choices"][0]["finish_reason"] == "tool_calls"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("stop", "stop"),
            ("length", "length"),
            ("natural_stop", "stop"),
            ("length_limit", "length"),
            ("safety_filter", "content_filter"),
            ("", "stop"),
            ("something_odd", "stop"),
        ],
    )
    def test_finish_reason_mapping(self, raw, expected):
        assert (
            translation.finish_reason_from_result({"finish_reason": raw}, has_tool_calls=False)
            == expected
        )

    def test_usage_details_included_when_present(self):
        usage = translation.usage_payload(
            {"prompt_tokens": 10, "completion_tokens": 4, "reasoning_tokens": 3, "cache_read_tokens": 8}
        )
        assert usage["total_tokens"] == 14
        assert usage["completion_tokens_details"]["reasoning_tokens"] == 3
        assert usage["prompt_tokens_details"]["cached_tokens"] == 8

    def test_responses_payload_shape(self):
        payload = translation.responses_payload(
            {"content": "hi", "prompt_tokens": 1, "completion_tokens": 1},
            model_id="openai/gpt-4o-mini",
            conversation_id="conv-1",
        )
        assert payload["object"] == "response"
        assert payload["status"] == "completed"
        assert payload["output"][0]["content"][0]["text"] == "hi"
        assert payload["output_text"] == "hi"
        assert payload["conversation"]["id"] == "conv-1"
        assert payload["usage"]["input_tokens"] == 1

    def test_completion_payload_includes_reasoning_content(self):
        payload = translation.completion_payload(
            {"content": "answer", "reasoning_content": "step by step"},
            model_id="mock/mock-small",
        )
        assert payload["choices"][0]["message"]["reasoning_content"] == "step by step"
        assert payload["choices"][0]["message"]["content"] == "answer"

    def test_responses_payload_prepends_reasoning_item(self):
        payload = translation.responses_payload(
            {"content": "hi", "reasoning_content": "thinking aloud"},
            model_id="mock/mock-small",
        )
        assert payload["output"][0]["type"] == "reasoning"
        assert payload["output"][0]["summary"][0]["text"] == "thinking aloud"
        assert payload["output"][1]["type"] == "message"

    def test_responses_payload_includes_function_calls(self):
        payload = translation.responses_payload(
            {
                "content": "",
                "tool_calls_canonical": [
                    {"call_id": "c1", "tool_name": "core.web_search", "arguments_json": "{}"}
                ],
            },
            model_id="openai/gpt-4o-mini",
        )
        item = payload["output"][0]
        assert item["type"] == "function_call"
        assert item["name"] == "core__web_search"

    def test_model_card_shape(self):
        _, _, spec = translation.resolve_model("openai/gpt-4o-mini", ALLOW_ALL)
        card = translation.model_card("openai", "gpt-4o-mini", spec)
        assert card["id"] == "openai/gpt-4o-mini"
        assert card["object"] == "model"
        assert card["owned_by"] == "openai"
