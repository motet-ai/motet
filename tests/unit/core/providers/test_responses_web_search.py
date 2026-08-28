"""
Motet - Responses Native Web Search Mapping Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for Responses-host web_search wire mapping and citation parse.
    OpenAI, Grok, DeepSeek, and Meta inherit ``{"type": "web_search"}`` from the
    parent adapter. Mixing a function tool with native search is supported
    on the same tools list.

Usage:
    pytest tests/unit/core/providers/test_responses_web_search.py
"""

from __future__ import annotations

from typing import Type

import pytest

from motet.core.models.adapters.provider_builtin_tools import get_provider_builtin_tool_names
from motet.core.models.adapters.providers.deepseek_responses import DeepSeekResponsesAdapter
from motet.core.models.adapters.providers.openai_responses import (
    OpenAIResponsesAdapter,
    _extract_provider_tool_use_events,
    _parse_response_to_canonical,
)
from motet.core.models.adapters.providers.meta_responses import MetaResponsesAdapter
from motet.core.models.adapters.providers.xai_responses import XAIResponsesAdapter
from motet.core.types import CanonicalToolSchema, LLMRequest, Message


def _web_search_schema(name: str = "web_search") -> CanonicalToolSchema:
    return CanonicalToolSchema(
        name=name,
        description="Search the web",
        json_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )


def _function_schema() -> CanonicalToolSchema:
    return CanonicalToolSchema(
        name="core.spawn_agents",
        description="Spawn child agents",
        json_schema={"type": "object", "properties": {}},
    )


@pytest.mark.parametrize(
    ("adapter_cls", "provider", "namespaced_name"),
    [
        (OpenAIResponsesAdapter, "openai", "openai.web_search"),
        (XAIResponsesAdapter, "xai", "xai.web_search"),
        (DeepSeekResponsesAdapter, "deepseek", "deepseek.web_search"),
        (MetaResponsesAdapter, "meta", "meta.web_search"),
    ],
)
def test_responses_emits_web_search_and_mixes_function_tools(
    adapter_cls: Type[OpenAIResponsesAdapter],
    provider: str,
    namespaced_name: str,
) -> None:
    adapter = adapter_cls(
        provider=provider,
        adapter_name="responses",
        credentials={"api_key": "k"},
    )
    assert adapter._web_search_canonical_names() == {"web_search", namespaced_name}
    assert adapter._web_search_wire_type() == "web_search"
    assert adapter._web_search_tool_use_name() == namespaced_name

    tools = adapter._responses_tools(
        [_web_search_schema(), _web_search_schema(namespaced_name), _function_schema()]
    )
    assert tools is not None
    assert tools.count({"type": "web_search"}) == 2
    assert {"type": "web_search_preview"} not in tools
    assert any(t.get("type") == "function" and t.get("name") == "core.spawn_agents" for t in tools)


def test_provider_builtin_allowlist_includes_responses_hosts() -> None:
    assert get_provider_builtin_tool_names(
        provider="xai", allowlist_csv=None, denylist_csv=None
    ) == ["xai.web_search"]
    assert get_provider_builtin_tool_names(
        provider="deepseek", allowlist_csv=None, denylist_csv=None
    ) == ["deepseek.web_search"]
    assert get_provider_builtin_tool_names(
        provider="meta", allowlist_csv=None, denylist_csv=None
    ) == ["meta.web_search"]


def test_deepseek_finalize_drops_openai_only_fields() -> None:
    adapter = DeepSeekResponsesAdapter(
        provider="deepseek",
        adapter_name="responses",
        credentials={"api_key": "k"},
    )
    request = LLMRequest(
        messages=[Message(role="user", content="hi")],
        model_settings={"model_name": "deepseek-v4-pro", "enable_thinking": True, "reasoning_effort": "max"},
    )
    params = adapter._finalize_responses_params(
        {
            "model": "deepseek-v4-pro",
            "reasoning": {"effort": "high", "summary": "auto"},
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": "conv-1",
            "previous_response_id": "resp_1",
        },
        request,
    )
    assert params["reasoning"] == {"effort": "max"}
    assert "store" not in params
    assert "include" not in params
    assert "prompt_cache_key" not in params
    assert "previous_response_id" not in params


def test_parse_collects_deepseek_web_search_call_urls() -> None:
    raw = {
        "output": [
            {
                "type": "web_search_call",
                "id": "call_01",
                "status": "completed",
                "action": {
                    "type": "open_page",
                    "url": "https://www.boston.gov#ws_call_id=call_01",
                },
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "The official site is boston.gov.", "annotations": []}],
            },
        ]
    }
    _text, _calls, citations, _stop, _usage, _reason = _parse_response_to_canonical(raw)
    assert [c.url for c in citations] == ["https://www.boston.gov"]


def test_parse_merges_top_level_citation_urls() -> None:
    raw = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Boston has the Freedom Trail.",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.com/boston",
                                "title": "1",
                                "start_index": 0,
                                "end_index": 10,
                            }
                        ],
                    }
                ],
            },
            {"type": "web_search_call", "status": "completed", "id": "ws_1"},
        ],
        "citations": [
            "https://example.com/boston",
            "https://example.com/extra",
            {"url": "https://example.com/dict"},
        ],
    }
    text, _calls, citations, _stop, _usage, _reason = _parse_response_to_canonical(raw)
    assert "Freedom Trail" in text
    urls = {c.url for c in citations}
    assert "https://example.com/boston" in urls
    assert "https://example.com/extra" in urls
    assert "https://example.com/dict" in urls

    events = _extract_provider_tool_use_events(raw, tool_name="xai.web_search")
    assert len(events) == 1
    assert events[0].kind == "provider"
    assert events[0].tool_name == "xai.web_search"
