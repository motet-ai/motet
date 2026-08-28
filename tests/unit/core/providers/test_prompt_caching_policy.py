"""
Motet - Provider Prompt Caching Policy Tests (ADR-0124)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-18

Description:
    Unit tests for ADR-0124 provider prompt caching policy:
    CAP_PROMPT_CACHING tagging, capability-gated helpers, Anthropic cache_control
    breakpoints, OpenAI/Moonshot prompt_cache_key injection, and agentic-loop defaults.

Dependencies:
    - pytest
    - motet.core.models.specs / adapters.prompt_caching
    - Anthropic / OpenAI / Moonshot / xAI adapters
    - agentic_loop._resolve_enable_prompt_caching

Usage:
    pytest tests/unit/core/providers/test_prompt_caching_policy.py
"""

from __future__ import annotations

from typing import Any, Dict

from motet.core.models.adapters.prompt_caching import (
    apply_prompt_cache_key,
    conversation_prompt_cache_key,
    prompt_caching_enabled,
)
from motet.core.models.adapters.providers.anthropic_messages import (
    _canonical_tools_to_anthropic,
    _format_messages_for_anthropic,
)
from motet.core.models.adapters.providers.openai_responses import OpenAIResponsesAdapter
from motet.core.models.registry import get_model_spec
from motet.core.models.specs import (
    CAP_IMAGE_GENERATION,
    CAP_PROMPT_CACHING,
    CAP_STREAM,
    MODEL_REGISTRY,
)
from motet.core.reasoning.react.agentic_loop import (
    _resolve_enable_prompt_caching,
)
from motet.core.types import CanonicalToolSchema, LLMRequest, Message, RequestContext


def test_cap_prompt_caching_tagged_on_hosted_chat_models() -> None:
    assert CAP_PROMPT_CACHING in MODEL_REGISTRY["openai"]["gpt-4o"].capabilities
    assert CAP_PROMPT_CACHING in MODEL_REGISTRY["anthropic"]["claude-sonnet-5"].capabilities
    assert CAP_PROMPT_CACHING in MODEL_REGISTRY["xai"]["grok-4.5"].capabilities
    assert CAP_PROMPT_CACHING in MODEL_REGISTRY["xai"]["grok-4.6"].capabilities
    assert CAP_PROMPT_CACHING in MODEL_REGISTRY["moonshot"]["kimi-k2.5"].capabilities
    assert CAP_PROMPT_CACHING in MODEL_REGISTRY["deepseek"]["deepseek-v4-flash"].capabilities


def test_cap_prompt_caching_absent_on_image_only_and_local() -> None:
    image = MODEL_REGISTRY["openai"]["gpt-image-1"]
    assert CAP_IMAGE_GENERATION in image.capabilities
    assert CAP_STREAM not in image.capabilities
    assert CAP_PROMPT_CACHING not in image.capabilities

    local = MODEL_REGISTRY["local"]["llama-3.1-8b-instruct"]
    assert CAP_PROMPT_CACHING not in local.capabilities


def test_prompt_caching_enabled_requires_flag_and_capability() -> None:
    request_on = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={
            "model_name": "gpt-4o",
            "enable_prompt_caching": True,
        },
    )
    assert prompt_caching_enabled(request_on, provider="openai") is True

    request_off = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={
            "model_name": "gpt-4o",
            "enable_prompt_caching": False,
        },
    )
    assert prompt_caching_enabled(request_off, provider="openai") is False

    # Image-only model: flag True is still a no-op (no CAP).
    request_image = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={
            "model_name": "gpt-image-1",
            "enable_prompt_caching": True,
        },
    )
    assert prompt_caching_enabled(request_image, provider="openai") is False


def test_conversation_prompt_cache_key_from_request_context() -> None:
    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "gpt-4o"},
        request_context=RequestContext(conversation_id="  conv-abc  "),
    )
    assert conversation_prompt_cache_key(request) == "conv-abc"
    assert conversation_prompt_cache_key(
        LLMRequest(messages=[Message(role="user", content="hi")])
    ) is None


def test_apply_prompt_cache_key_gated_for_openai() -> None:
    params: Dict[str, Any] = {"model": "gpt-4o"}
    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={
            "model_name": "gpt-4o",
            "enable_prompt_caching": True,
        },
        request_context=RequestContext(conversation_id="conv-99"),
    )
    apply_prompt_cache_key(params, request, provider="openai")
    assert params["prompt_cache_key"] == "conv-99"

    params_off: Dict[str, Any] = {"model": "gpt-4o"}
    request_off = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "gpt-4o"},
        request_context=RequestContext(conversation_id="conv-99"),
    )
    apply_prompt_cache_key(params_off, request_off, provider="openai")
    assert "prompt_cache_key" not in params_off


def test_openai_responses_finalize_sets_prompt_cache_key_when_enabled() -> None:
    adapter = OpenAIResponsesAdapter(
        provider="openai",
        adapter_name="responses",
        credentials={"api_key": "k"},
    )
    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={
            "model_name": "gpt-4o",
            "enable_prompt_caching": True,
        },
        request_context=RequestContext(conversation_id="conv-openai"),
    )
    params = adapter._finalize_responses_params(
        {"model": "gpt-4o", "input": []},
        request,
    )
    assert params["store"] is False
    assert params["prompt_cache_key"] == "conv-openai"


def test_anthropic_system_cache_control_when_enabled() -> None:
    system, _msgs = _format_messages_for_anthropic(
        messages=[
            Message(role="system", content="You are helpful."),
            Message(role="user", content="hi"),
        ],
        request_context=RequestContext(enable_multimodal=False),
        enable_prompt_caching=True,
    )
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == "You are helpful."


def test_anthropic_volatile_system_kept_out_of_cached_block() -> None:
    """Per-turn injections must not be fused into the cached stable system block."""
    system, _msgs = _format_messages_for_anthropic(
        messages=[
            Message(role="system", content="You are helpful."),
            Message(
                role="system",
                content="A pending action awaits the user's decision.",
                metadata={"source": "pending_action", "cache_volatile": True},
            ),
            Message(
                role="system",
                content="Relevant context from memory:\n[Relevance: 0.57] foo",
                metadata={"source": "memory_recall", "cache_volatile": True},
            ),
            Message(role="user", content="hi"),
        ],
        request_context=RequestContext(enable_multimodal=False),
        enable_prompt_caching=True,
    )
    assert isinstance(system, list)
    # Stable block first, carrying the breakpoint.
    assert system[0]["text"] == "You are helpful."
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    # Volatile blocks follow, uncached.
    assert [b["text"] for b in system[1:]] == [
        "A pending action awaits the user's decision.",
        "Relevant context from memory:\n[Relevance: 0.57] foo",
    ]
    assert all("cache_control" not in b for b in system[1:])


def test_anthropic_volatile_source_marker_without_flag_still_split() -> None:
    """Stored transcripts predate the cache_volatile flag; the source marker suffices."""
    system, _msgs = _format_messages_for_anthropic(
        messages=[
            Message(role="system", content="Base prompt."),
            Message(
                role="system",
                content="Pending action text.",
                metadata={"source": "pending_action"},
            ),
            Message(role="user", content="hi"),
        ],
        request_context=RequestContext(enable_multimodal=False),
        enable_prompt_caching=True,
    )
    assert isinstance(system, list)
    assert system[0]["text"] == "Base prompt."
    assert "cache_control" in system[0]
    assert system[1]["text"] == "Pending action text."
    assert "cache_control" not in system[1]


def test_anthropic_volatile_only_system_has_no_breakpoint() -> None:
    system, _msgs = _format_messages_for_anthropic(
        messages=[
            Message(
                role="system",
                content="Pending action text.",
                metadata={"cache_volatile": True},
            ),
            Message(role="user", content="hi"),
        ],
        request_context=RequestContext(enable_multimodal=False),
        enable_prompt_caching=True,
    )
    assert isinstance(system, list)
    assert all("cache_control" not in b for b in system)


def test_anthropic_system_string_join_when_caching_disabled() -> None:
    system, _msgs = _format_messages_for_anthropic(
        messages=[
            Message(role="system", content="Base prompt."),
            Message(
                role="system",
                content="Pending action text.",
                metadata={"cache_volatile": True},
            ),
            Message(role="user", content="hi"),
        ],
        request_context=RequestContext(enable_multimodal=False),
        enable_prompt_caching=False,
    )
    assert system == "Base prompt.\n\nPending action text."


def test_anthropic_tools_cache_control_on_last_tool() -> None:
    tools = [
        CanonicalToolSchema(
            name="alpha",
            description="A",
            json_schema={"type": "object", "properties": {}},
        ),
        CanonicalToolSchema(
            name="beta",
            description="B",
            json_schema={"type": "object", "properties": {}},
        ),
    ]
    out = _canonical_tools_to_anthropic(tools, enable_prompt_caching=True)
    assert out is not None
    assert "cache_control" not in out[0]
    assert out[-1]["cache_control"] == {"type": "ephemeral"}
    assert out[-1]["name"] == "beta"

    out_off = _canonical_tools_to_anthropic(tools, enable_prompt_caching=False)
    assert out_off is not None
    assert all("cache_control" not in t for t in out_off)


def test_agentic_loop_defaults_prompt_caching_from_capability() -> None:
    assert (
        _resolve_enable_prompt_caching(
            enable_prompt_caching=None,
            model_provider="openai",
            model_name="gpt-4o",
        )
        is True
    )
    assert (
        _resolve_enable_prompt_caching(
            enable_prompt_caching=None,
            model_provider="local",
            model_name="llama-3.1-8b-instruct",
        )
        is False
    )
    assert (
        _resolve_enable_prompt_caching(
            enable_prompt_caching=False,
            model_provider="openai",
            model_name="gpt-4o",
        )
        is False
    )
    assert (
        _resolve_enable_prompt_caching(
            enable_prompt_caching=True,
            model_provider="local",
            model_name="llama-3.1-8b-instruct",
        )
        is True
    )


def test_get_model_spec_exposes_prompt_caching_cap() -> None:
    spec = get_model_spec("anthropic", "claude-sonnet-5")
    assert spec is not None
    assert CAP_PROMPT_CACHING in spec.capabilities
