"""
Motet - Anthropic Messages Adapter Formatting Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Unit tests for rendering canonical `Message` history into Anthropic Messages API inputs
    and for Anthropic thinking param shapes (adaptive, fixed-budget, Opus 5+ explicit disable).

    Validates:
    - system/developer extraction into the `system` string
    - standardized tool_calls on assistant messages -> `tool_use` content blocks
    - canonical role="tool" messages -> `tool_result` blocks in role="user"
    - Opus/Sonnet 5+ send thinking.type=disabled when enable_thinking is false

Dependencies:
    - pytest
    - motet.core.models.adapters.providers.anthropic_messages._format_messages_for_anthropic
    - motet.core.types.Message

Usage:
    pytest tests/unit/core/providers/test_anthropic_messages_format_messages.py
"""

from __future__ import annotations

import pytest


def test_anthropic_format_messages_renders_tool_use_and_tool_result_blocks() -> None:
    from motet.core.models.adapters.providers.anthropic_messages import _format_messages_for_anthropic
    from motet.core.types import Message, RequestContext

    system_msg = Message(role="system", content="You are helpful.")
    assistant_tool_call = Message(
        role="assistant",
        content="",
        tool_calls_canonical=[
            {
                "call_id": "call_1",
                "tool_name": "mcp.google_workspace.list_docs_in_folder",
                "arguments_json": '{"folder_id": "root", "page_size": 10}',
                "arguments": {"folder_id": "root", "page_size": 10},
            }
        ],
    )
    tool_result = Message(role="tool", content="OK", tool_call_id="call_1")

    system, msgs = _format_messages_for_anthropic(
        messages=[system_msg, assistant_tool_call, tool_result],
        request_context=RequestContext(enable_multimodal=False),
    )

    assert system == "You are helpful."
    assert len(msgs) == 2

    # assistant -> tool_use
    assert msgs[0]["role"] == "assistant"
    blocks0 = msgs[0]["content"]
    assert isinstance(blocks0, list)
    assert blocks0[0]["type"] == "tool_use"
    assert blocks0[0]["id"] == "call_1"
    assert blocks0[0]["name"] == "mcp.google_workspace.list_docs_in_folder"
    assert blocks0[0]["input"]["folder_id"] == "root"

    # tool -> user tool_result
    assert msgs[1]["role"] == "user"
    blocks1 = msgs[1]["content"]
    assert blocks1[0]["type"] == "tool_result"
    assert blocks1[0]["tool_use_id"] == "call_1"
    assert blocks1[0]["content"] == "OK"


def test_anthropic_format_messages_ignores_leftover_openai_tool_calls() -> None:
    """Issue #225: leftover Message.tool_calls is discarded, not lifted."""
    from motet.core.models.adapters.providers.anthropic_messages import _format_messages_for_anthropic
    from motet.core.types import Message, RequestContext

    assistant_tool_call = Message(
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "web_search", "arguments": "{\"query\":\"cats\"}"},
            }
        ],
    )

    assert assistant_tool_call.tool_calls_canonical is None
    system, msgs = _format_messages_for_anthropic(
        messages=[assistant_tool_call],
        request_context=RequestContext(enable_multimodal=False),
    )
    assert system is None
    assert msgs[0]["role"] == "assistant"
    content = msgs[0]["content"]
    if isinstance(content, list):
        assert all(block.get("type") != "tool_use" for block in content if isinstance(block, dict))


def test_anthropic_thinking_params_use_fixed_budget_for_legacy_models() -> None:
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params = {"temperature": 0.2}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={
            "enable_thinking": True,
            "thinking_budget_tokens": 4096,
            "reasoning_effort": "high",
        },
        model_name="claude-3-5-sonnet-latest",
    )

    assert enabled is True
    assert params["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert "output_config" not in params
    assert "temperature" not in params


def test_anthropic_thinking_params_use_adaptive_for_new_models() -> None:
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params = {"temperature": 0.2}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={"enable_thinking": True, "reasoning_effort": "low"},
        model_name="claude-sonnet-4-6-20260501",
    )

    assert enabled is True
    assert params["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert params["output_config"] == {"effort": "low"}
    assert "temperature" not in params


def test_anthropic_thinking_params_allows_explicit_adaptive_override() -> None:
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params = {"temperature": 0.2}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={
            "enable_thinking": True,
            "anthropic_thinking_type": "adaptive",
            "reasoning_effort": "max",
        },
        model_name="claude-3-5-sonnet-latest",
    )

    assert enabled is True
    assert params["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert params["output_config"] == {"effort": "max"}
    assert "temperature" not in params


def test_anthropic_thinking_params_allows_omitted_display_override() -> None:
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params = {"temperature": 0.2}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={
            "enable_thinking": True,
            "reasoning_effort": "high",
            "anthropic_thinking_display": "omitted",
        },
        model_name="claude-opus-4-7",
    )

    assert enabled is True
    assert params["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert params["output_config"] == {"effort": "high"}
    assert "temperature" not in params


def test_anthropic_thinking_params_disables_explicitly_for_opus_5() -> None:
    """Opus 5 defaults thinking on; Motet enable_thinking=False must send disabled."""
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params: dict = {"temperature": 0.2}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={"enable_thinking": False, "reasoning_effort": "medium"},
        model_name="claude-opus-5",
    )

    assert enabled is False
    assert params["thinking"] == {"type": "disabled"}
    assert params["output_config"] == {"effort": "medium"}
    assert "temperature" not in params


def test_anthropic_thinking_params_clamps_effort_when_disabling_opus_5() -> None:
    """disabled + xhigh/max returns 400 on Opus 5; clamp effort to high."""
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params: dict = {}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={"enable_thinking": False, "reasoning_effort": "max"},
        model_name="claude-opus-5",
    )

    assert enabled is False
    assert params["thinking"] == {"type": "disabled"}
    assert params["output_config"] == {"effort": "high"}


def test_anthropic_thinking_params_preserves_xhigh_when_thinking_enabled() -> None:
    """xhigh is a real Anthropic rung; it must survive when thinking is on."""
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params: dict = {}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={"enable_thinking": True, "reasoning_effort": "xhigh"},
        model_name="claude-opus-5",
    )

    assert enabled is True
    assert params["thinking"]["type"] == "adaptive"
    assert params["output_config"]["effort"] == "xhigh"


def test_anthropic_thinking_params_clamps_xhigh_when_disabling_opus_5() -> None:
    """The Opus 5 disabled-thinking ceiling applies to xhigh as well as max."""
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params: dict = {}

    _apply_anthropic_thinking_params(
        params=params,
        settings={"enable_thinking": False, "reasoning_effort": "xhigh"},
        model_name="claude-opus-5",
    )

    assert params["output_config"] == {"effort": "high"}


def test_anthropic_thinking_params_keeps_max_effort_when_disabling_sonnet_5() -> None:
    """The disabled-thinking effort ceiling is Opus-specific; Sonnet 5 accepts max."""
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params: dict = {}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={"enable_thinking": False, "reasoning_effort": "max"},
        model_name="claude-sonnet-5",
    )

    assert enabled is False
    assert params["thinking"] == {"type": "disabled"}
    assert params["output_config"] == {"effort": "max"}


def test_anthropic_thinking_params_omits_thinking_when_off_for_opus_4_8() -> None:
    """Pre-Opus-5 models: omit thinking field when disabled (API default is off)."""
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params: dict = {"temperature": 0.2}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={"enable_thinking": False},
        model_name="claude-opus-4-8",
    )

    assert enabled is False
    assert "thinking" not in params
    assert "output_config" not in params
    assert params.get("temperature") == 0.2


def test_anthropic_thinking_params_enables_adaptive_for_opus_5() -> None:
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params: dict = {}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={"enable_thinking": True, "reasoning_effort": "high"},
        model_name="claude-opus-5",
    )

    assert enabled is True
    assert params["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert params["output_config"] == {"effort": "high"}


def test_anthropic_adaptive_models_default_to_high_effort() -> None:
    """Adaptive-thinking Claude families match Anthropic's own `high` default."""
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params: dict = {}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={"enable_thinking": True},
        model_name="claude-opus-5",
    )

    assert enabled is True
    assert params["output_config"] == {"effort": "high"}


def test_anthropic_legacy_models_default_to_medium_effort() -> None:
    """Fixed-budget-era models keep Motet's generic medium default."""
    from motet.core.models.adapters.providers.anthropic_messages import _apply_anthropic_thinking_params

    params: dict = {}

    enabled = _apply_anthropic_thinking_params(
        params=params,
        settings={"enable_thinking": True, "anthropic_thinking_type": "adaptive"},
        model_name="claude-3-5-sonnet-latest",
    )

    assert enabled is True
    assert params["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize(
    "model_name,family,major,minor",
    [
        ("claude-opus-5", "opus", 5, 0),
        ("claude-opus-4.8", "opus", 4, 8),
        ("claude-sonnet-4-5-20250929", "sonnet", 4, 5),
        ("claude-haiku-4-5-20251001", "haiku", 4, 5),
        ("claude-3-5-sonnet-latest", "", 0, 0),
        ("claude-fable-5", "fable", 5, 0),
        ("claude-mythos-5", "mythos", 5, 0),
        ("gpt-5.6-sol", "", 0, 0),
    ],
)
def test_parse_claude_model_handles_separators_and_date_snapshots(
    model_name: str, family: str, major: int, minor: int
) -> None:
    """Date suffixes must not be read as version components (…-4-5-20250929 is 4.5)."""
    from motet.core.models.adapters.providers.anthropic_messages import _parse_claude_model

    model = _parse_claude_model(model_name)
    assert (model.family, model.major, model.minor) == (family, major, minor)


@pytest.mark.parametrize(
    "model_name,adaptive,explicit_disable",
    [
        ("claude-opus-5", True, True),
        ("claude-sonnet-5", True, True),
        ("claude-opus-4.8", True, False),
        ("claude-haiku-4-5-20251001", False, False),
        ("claude-3-5-sonnet-latest", False, False),
        # Always-on families are adaptive but reject an explicit disable.
        ("claude-fable-5", True, False),
        ("claude-mythos-5", True, False),
    ],
)
def test_anthropic_model_policy_predicates(
    model_name: str, adaptive: bool, explicit_disable: bool
) -> None:
    """Policy predicates share one parser; pin the family/version distinctions they encode."""
    from motet.core.models.adapters.providers.anthropic_messages import (
        _anthropic_model_prefers_adaptive_thinking,
        _anthropic_model_requires_explicit_thinking_disable,
    )

    assert _anthropic_model_prefers_adaptive_thinking(model_name) is adaptive
    assert _anthropic_model_requires_explicit_thinking_disable(model_name) is explicit_disable


def test_parse_content_blocks_converts_mcp_wire_name() -> None:
    from motet.core.models.adapters.providers.anthropic_messages import _parse_content_blocks

    text, tool_calls, server_calls, _thinking = _parse_content_blocks(
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "mcp__test__add_two_numbers",
                    "input": {"a": 7, "b": 5},
                }
            ]
        }
    )
    assert text == ""
    assert server_calls == []
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "mcp.test.add_two_numbers"
    assert tool_calls[0].call_id == "toolu_1"

