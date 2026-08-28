"""
Motet - Kimi K3 Moonshot Adapter Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-17

Description:
    Unit tests for Kimi K3 ModelSpec registration and Moonshot Chat Completions
    generation params (always-on reasoning_effort, max_completion_tokens, no
    K2.x thinking{} block).

Dependencies:
    - pytest: Test framework
    - motet.core.models.specs: MODEL_REGISTRY / get_model_spec
    - motet.core.models.adapters.providers.moonshot_chat_completions: param helpers

Usage:
    pytest tests/unit/core/providers/test_moonshot_k3.py
"""

from __future__ import annotations

from decimal import Decimal

from motet.core.models.registry import get_model_spec
from motet.core.models.specs import (
    CAP_REASONING,
    CAP_TOOL_USE,
    CAP_VISION,
)
from motet.core.models.adapters.providers import moonshot_chat_completions as moon


def test_kimi_k3_model_spec() -> None:
    spec = get_model_spec("moonshot", "kimi-k3")
    assert spec is not None
    assert spec.display_name == "Kimi K3"
    assert CAP_TOOL_USE in spec.capabilities
    assert CAP_VISION in spec.capabilities
    assert CAP_REASONING in spec.capabilities
    assert spec.default_adapter == "chat_completions"
    assert spec.max_output_tokens == 131072
    assert spec.supported_builtin_tools == []
    assert spec.base_url == "https://api.moonshot.ai/v1"
    assert spec.pricing is not None
    assert spec.pricing.input_per_1k == Decimal("0.003")
    assert spec.pricing.output_per_1k == Decimal("0.015")
    assert spec.pricing.cache_read_discount_pct == Decimal("90.0")


def test_kimi_k3_generation_params_always_on_reasoning() -> None:
    params: dict = {}
    emit = moon._apply_moonshot_generation_params(
        params,
        model_name="kimi-k3",
        settings={"enable_thinking": False, "reasoning_effort": "high", "max_tokens": 4096},
        moonshot_tools=None,
    )
    assert emit is True
    assert params["reasoning_effort"] == "max"
    assert params["max_completion_tokens"] == 4096
    assert "max_tokens" not in params
    assert "temperature" not in params
    assert "extra_body" not in params


def test_kimi_k3_generation_params_default_max_completion_tokens() -> None:
    params: dict = {}
    moon._apply_moonshot_generation_params(
        params,
        model_name="kimi-k3",
        settings={},
        moonshot_tools=None,
    )
    assert params["max_completion_tokens"] == 131072
    assert params["reasoning_effort"] == "max"


def test_kimi_k3_prefers_max_completion_tokens_setting() -> None:
    params: dict = {}
    moon._apply_moonshot_generation_params(
        params,
        model_name="kimi-k3",
        settings={"max_completion_tokens": 65536, "max_tokens": 1000},
        moonshot_tools=None,
    )
    assert params["max_completion_tokens"] == 65536


def test_kimi_k3_attaches_tools_without_thinking_block() -> None:
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
    params: dict = {}
    emit = moon._apply_moonshot_generation_params(
        params,
        model_name="kimi-k3",
        settings={"enable_thinking": True},
        moonshot_tools=tools,
    )
    assert emit is True
    assert params["tools"] == tools
    assert params.get("extra_body") is None
    assert params["reasoning_effort"] == "max"


def test_kimi_k25_still_uses_thinking_block() -> None:
    params: dict = {}
    emit = moon._apply_moonshot_generation_params(
        params,
        model_name="kimi-k2.5",
        settings={"enable_thinking": True},
        moonshot_tools=None,
    )
    assert emit is True
    assert params["max_tokens"] == 65536  # ModelSpec.max_output_tokens for kimi-k2.5
    assert params["temperature"] == 1.0
    assert params["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "reasoning_effort" not in params


def test_ensure_reasoning_content_on_assistant_tool_calls() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}],
        }
    ]
    moon._ensure_reasoning_content(messages)
    assert messages[0]["reasoning_content"] == ""
