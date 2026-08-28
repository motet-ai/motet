"""
Motet - MCP Keyword Inference Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-10

Description:
    Unit tests for MCP tool keyword inference in worker_mcp_startup.

    Every MCP tool must get a generic keyword baseline from name tokenization
    (_mcp_name_keywords: snake_case / kebab-case / dotted / camelCase splitting
    with stopword filtering), so tools from arbitrary servers are never
    keyword-less. Curated service-specific branches in
    _infer_mcp_tool_capabilities add synonym enrichment on top for known
    services (google_workspace, weather, slack, ...). Registry keywords feed
    the conversation-analysis tool-intent pattern and function-discovery
    keyword fusion, so an empty list degrades both.

Dependencies:
    - pytest
    - motet.core.distributed.worker_mcp_startup

Usage:
    pytest tests/unit/core/distributed/test_mcp_keyword_inference.py
"""

from __future__ import annotations

import pytest

from motet.core.distributed.worker_mcp_startup import (
    _infer_mcp_tool_capabilities,
    _mcp_name_keywords,
)


class TestMcpNameKeywords:
    def test_snake_case_tokenization(self) -> None:
        assert _mcp_name_keywords("google_workspace", "send_gmail_message") == [
            "google", "workspace", "send", "gmail", "message",
        ]

    def test_kebab_and_dotted_tokenization(self) -> None:
        assert _mcp_name_keywords("linear-server", "issues.create") == [
            "linear", "issues", "create",
        ]

    def test_camel_case_tokenization(self) -> None:
        assert _mcp_name_keywords("spotify", "playTrackFromPlaylist") == [
            "spotify", "play", "track", "playlist",
        ]

    def test_stopwords_and_short_tokens_filtered(self) -> None:
        # "in", "a" are stopwords; "v2" survives digit filtering only if not
        # numeric-only, but "2" alone is dropped.
        assert _mcp_name_keywords("mcp_server", "list_docs_in_a_folder_2") == [
            "list", "docs", "folder",
        ]

    def test_deduplicates_preserving_order(self) -> None:
        assert _mcp_name_keywords("search_server", "search_web") == [
            "search", "web",
        ]

    def test_empty_inputs(self) -> None:
        assert _mcp_name_keywords("", "") == []


class TestInferMcpToolCapabilities:
    def test_unknown_server_gets_name_baseline(self) -> None:
        """The original gap: arbitrary servers must not register keyword-less tools."""
        keywords, data_types = _infer_mcp_tool_capabilities(
            "spotify", "play_track", "Play a track on Spotify."
        )
        assert "spotify" in keywords
        assert "play" in keywords
        assert "track" in keywords
        assert data_types == []

    def test_known_service_gets_baseline_plus_curated_synonyms(self) -> None:
        keywords, data_types = _infer_mcp_tool_capabilities(
            "google_workspace", "send_gmail_message", "Send an email via Gmail."
        )
        # Baseline tokens from the names
        assert "gmail" in keywords
        assert "send" in keywords
        # Curated synonyms tokenization cannot infer
        assert "email" in keywords
        assert "inbox" in keywords
        assert "google_workspace" in data_types

    def test_weather_enrichment_still_applies(self) -> None:
        keywords, data_types = _infer_mcp_tool_capabilities(
            "weather_service", "current_conditions", "Get current weather."
        )
        assert "weather" in keywords
        assert "forecast" in keywords
        assert "weather" in data_types

    def test_no_duplicate_keywords(self) -> None:
        keywords, _ = _infer_mcp_tool_capabilities(
            "slack", "slack_send_message", "Send a Slack message."
        )
        assert len(keywords) == len(set(keywords))
