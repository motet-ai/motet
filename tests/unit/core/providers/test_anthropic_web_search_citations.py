"""
Motet - Anthropic Native Web Search Citation Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for Anthropic Messages web_search citation parse. Text-block
    ``web_search_result_location`` rows and ``web_search_tool_result`` items
    become canonical Citation URLs for core.web_search.

Usage:
    pytest tests/unit/core/providers/test_anthropic_web_search_citations.py
"""

from __future__ import annotations

from motet.core.models.adapters.providers.anthropic_messages import (
    _canonical_tools_to_anthropic,
    _extract_web_search_citations,
)
from motet.core.types import CanonicalToolSchema


def test_anthropic_emits_web_search_server_tool() -> None:
    tools = _canonical_tools_to_anthropic(
        [
            CanonicalToolSchema(
                name="web_search",
                description="Search the web",
                json_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
            CanonicalToolSchema(
                name="core.spawn_agents",
                description="Spawn child agents",
                json_schema={"type": "object", "properties": {}},
            ),
        ]
    )
    assert tools is not None
    assert {"type": "web_search_20250305", "name": "web_search"} in tools
    assert any(t.get("name") == "core.spawn_agents" for t in tools)


def test_extract_prefers_location_citations_then_result_urls() -> None:
    raw = {
        "content": [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "web_search",
                "input": {"query": "things to do in Boston"},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": [
                    {
                        "type": "web_search_result",
                        "url": "https://www.boston.gov",
                        "title": "City of Boston",
                    },
                    {
                        "type": "web_search_result",
                        "url": "https://www.meetboston.com/things-to-do/",
                        "title": "Meet Boston",
                    },
                ],
            },
            {
                "type": "text",
                "text": "The Freedom Trail is a 2.5-mile walk.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": "https://www.boston.gov",
                        "title": "City of Boston",
                        "cited_text": "Freedom Trail links 16 historic sites.",
                    }
                ],
            },
        ]
    }
    citations = _extract_web_search_citations(raw)
    by_url = {c.url: c for c in citations}
    assert set(by_url) == {
        "https://www.boston.gov",
        "https://www.meetboston.com/things-to-do/",
    }
    assert by_url["https://www.boston.gov"].snippets == ["Freedom Trail links 16 historic sites."]
    assert by_url["https://www.meetboston.com/things-to-do/"].title == "Meet Boston"


def test_extract_skips_web_search_tool_result_errors() -> None:
    raw = {
        "content": [
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_err",
                "content": {
                    "type": "web_search_tool_result_error",
                    "error_code": "max_uses_exceeded",
                },
            },
            {
                "type": "text",
                "text": "I could not search.",
                "citations": [],
            },
        ]
    }
    assert _extract_web_search_citations(raw) == []
