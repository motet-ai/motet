"""
Motet - DeepSeek Chat Completions Adapter Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for DeepSeek V4 ModelSpec registration, adapter registration,
    credential normalization, thinking/reasoning_effort params, and
    reasoning_content replay on assistant tool-call messages.

Dependencies:
    - pytest: Test framework
    - motet.core.models.adapters: adapter_registry
    - motet.core.models.adapters.providers.deepseek_chat_completions: helpers
    - motet.core.models.registry: get_model_spec

Usage:
    pytest tests/unit/core/providers/test_deepseek_adapter.py
"""

from __future__ import annotations

from decimal import Decimal

from motet.core.models.adapters import adapter_registry
from motet.core.models.adapters.providers.deepseek_responses import DeepSeekResponsesAdapter
from motet.core.models.adapters.providers.deepseek_chat_completions import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DeepSeekChatCompletionsAdapter,
    _apply_deepseek_generation_params,
    _format_messages_for_deepseek,
    _normalize_deepseek_credentials,
    _resolve_reasoning_effort,
)
from motet.core.models.registry import get_model_spec
from motet.core.models.specs import CAP_REASONING, CAP_TOOL_USE, CAP_VISION
from motet.core.commands.builtin.model import _normalize_adapter_credentials
from motet.core.types import Message


def test_deepseek_responses_adapter_registered() -> None:
    adapter = adapter_registry.build(
        "deepseek",
        "responses",
        credentials={"deepseek_api_key": "test-key"},
    )
    assert isinstance(adapter, DeepSeekResponsesAdapter)
    assert adapter.provider == "deepseek"
    assert adapter.credentials is not None
    assert adapter.credentials["api_key"] == "test-key"
    assert adapter.credentials["base_url"] == DEFAULT_DEEPSEEK_BASE_URL
    caps = adapter.capabilities(model="deepseek-v4-pro")
    assert caps.supports_builtin_tools is True
    assert caps.supports_stateful_sessions is False
    assert caps.provider_metadata.get("adapter") == "deepseek_responses"


def test_deepseek_adapter_registered() -> None:
    adapter = adapter_registry.build(
        "deepseek",
        "chat_completions",
        credentials={"deepseek_api_key": "test-key"},
    )
    assert isinstance(adapter, DeepSeekChatCompletionsAdapter)
    assert adapter.provider == "deepseek"
    assert adapter.credentials is not None
    assert adapter.credentials["api_key"] == "test-key"
    assert adapter.credentials["base_url"] == DEFAULT_DEEPSEEK_BASE_URL


def test_normalize_deepseek_credentials_defaults_base_url() -> None:
    creds = _normalize_deepseek_credentials({"deepseek_api_key": "k"})
    assert creds["api_key"] == "k"
    assert creds["base_url"] == DEFAULT_DEEPSEEK_BASE_URL


def test_normalize_credentials_deepseek_env_base_url(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://example.deepseek.test")
    out = _normalize_adapter_credentials(
        provider="deepseek",
        credentials={"deepseek_api_key": "k"},
        spec=None,
    )
    assert out["base_url"] == "https://example.deepseek.test"
    assert out["api_key"] == "k"


def test_deepseek_v4_flash_model_spec() -> None:
    spec = get_model_spec("deepseek", "deepseek-v4-flash")
    assert spec is not None
    assert spec.provider == "deepseek"
    assert CAP_TOOL_USE in spec.capabilities
    assert CAP_REASONING in spec.capabilities
    assert CAP_VISION not in spec.capabilities
    assert spec.default_adapter == "responses"
    assert spec.supported_adapters == ["responses", "chat_completions"]
    assert spec.fallback_adapters == ["chat_completions"]
    assert spec.supported_builtin_tools == ["deepseek.web_search"]
    assert spec.base_url == "https://api.deepseek.com"
    assert spec.max_output_tokens == 384000
    assert spec.pricing is not None
    assert spec.pricing.input_per_1k == Decimal("0.00014")
    assert spec.pricing.output_per_1k == Decimal("0.00028")
    assert spec.provenance is not None
    assert spec.provenance.origin == "cn"


def test_deepseek_v4_pro_model_spec() -> None:
    spec = get_model_spec("deepseek", "deepseek-v4-pro")
    assert spec is not None
    assert CAP_TOOL_USE in spec.capabilities
    assert CAP_REASONING in spec.capabilities
    assert spec.default_adapter == "responses"
    assert spec.supported_adapters == ["responses", "chat_completions"]
    assert spec.supported_builtin_tools == ["deepseek.web_search"]
    assert spec.pricing is not None
    assert spec.pricing.input_per_1k == Decimal("0.000435")
    assert spec.pricing.output_per_1k == Decimal("0.00087")


def test_deepseek_adapter_capabilities() -> None:
    adapter = DeepSeekChatCompletionsAdapter(
        provider="deepseek",
        adapter_name="chat_completions",
        credentials={"api_key": "k"},
    )
    caps = adapter.capabilities(model="deepseek-v4-pro")
    assert caps.supports_tools is True
    assert caps.supports_parallel_tool_calls is True
    assert caps.supports_reasoning is True
    assert caps.supports_vision is False
    assert caps.provider_metadata.get("adapter") == "deepseek_chat_completions"


def test_resolve_reasoning_effort_mapping() -> None:
    assert _resolve_reasoning_effort({}) == "high"
    assert _resolve_reasoning_effort({"reasoning_effort": "low"}) == "high"
    assert _resolve_reasoning_effort({"reasoning_effort": "medium"}) == "high"
    assert _resolve_reasoning_effort({"reasoning_effort": "high"}) == "high"
    assert _resolve_reasoning_effort({"reasoning_effort": "max"}) == "max"
    assert _resolve_reasoning_effort({"reasoning_effort": "xhigh"}) == "max"


def test_generation_params_thinking_enabled() -> None:
    params: dict = {}
    emit = _apply_deepseek_generation_params(
        params,
        settings={
            "enable_thinking": True,
            "reasoning_effort": "max",
            "max_tokens": 4096,
            "temperature": 0.2,
        },
        openai_tools=[{"type": "function", "function": {"name": "get_date"}}],
    )
    assert emit is True
    assert params["max_tokens"] == 4096
    assert params["reasoning_effort"] == "max"
    assert params["extra_body"] == {"thinking": {"type": "enabled"}}
    assert params["tools"][0]["function"]["name"] == "get_date"
    assert "temperature" not in params


def test_generation_params_thinking_disabled() -> None:
    params: dict = {"model": "deepseek-v4-pro"}
    emit = _apply_deepseek_generation_params(
        params,
        settings={"enable_thinking": False, "temperature": 0.4},
        openai_tools=None,
    )
    assert emit is False
    assert params["extra_body"] == {"thinking": {"type": "disabled"}}
    assert params["temperature"] == 0.4
    assert "reasoning_effort" not in params
    # Unset max_tokens uses ModelSpec.max_output_tokens (not adapter magic 8000).
    assert params["max_tokens"] == 384000


def test_format_messages_replays_reasoning_content() -> None:
    messages = [
        Message(role="user", content="weather?"),
        Message(
            role="assistant",
            content="",
            reasoning_content="Need to call get_weather",
            tool_calls_canonical=[
                {
                    "call_id": "call_1",
                    "tool_name": "get_weather",
                    "arguments_json": "{}",
                }
            ],
        ),
        Message(role="tool", content="sunny", tool_call_id="call_1", name="get_weather"),
    ]
    formatted = _format_messages_for_deepseek(
        messages,
        model_name="deepseek-v4-pro",
        request_context=None,
    )
    assistant = next(m for m in formatted if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "Need to call get_weather"
    assert assistant["content"] == ""
    assert assistant["tool_calls"][0]["function"]["name"] == "get_weather"


def test_format_messages_maps_developer_to_system() -> None:
    messages = [Message(role="developer", content="Be concise.")]
    formatted = _format_messages_for_deepseek(
        messages,
        model_name="deepseek-v4-flash",
        request_context=None,
    )
    assert formatted[0]["role"] == "system"
    assert formatted[0]["content"] == "Be concise."


def test_format_messages_empty_reasoning_on_tool_calls() -> None:
    # Dict path (worker deserialization): content may be null with tool_calls_canonical.
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls_canonical": [
                {
                    "call_id": "call_1",
                    "tool_name": "noop",
                    "arguments_json": "{}",
                }
            ],
        }
    ]
    formatted = _format_messages_for_deepseek(
        messages,
        model_name="deepseek-v4-flash",
        request_context=None,
    )
    assert formatted[0]["reasoning_content"] == ""
    assert formatted[0]["content"] == ""
