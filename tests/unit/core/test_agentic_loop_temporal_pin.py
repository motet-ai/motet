"""
Motet - Agentic Loop Temporal Keyword Pin Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-02

Description:
    Unit tests for keyword pins of core.current_time and schedule tools so
    temporal intents remain callable in the frozen meta bag (issue #131 /
    ADR-0128).

Usage:
    pytest tests/unit/core/test_agentic_loop_temporal_pin.py
"""

from __future__ import annotations

from motet.core.reasoning.react.loop_discovery import (
    _TEMPORAL_PIN_TOOLS,
    _keyword_pinned_tool_names,
)


def test_temporal_query_pins_current_time_and_schedule_tools() -> None:
    pinned = _keyword_pinned_tool_names("please schedule this in a minute")
    for name in _TEMPORAL_PIN_TOOLS:
        assert name in pinned


def test_current_time_query_pins_clock_tool() -> None:
    pinned = _keyword_pinned_tool_names("what time is it in utc?")
    assert "core.current_time" in pinned


def test_non_temporal_query_does_not_pin_time_tools() -> None:
    pinned = _keyword_pinned_tool_names("summarize this document for me")
    for name in _TEMPORAL_PIN_TOOLS:
        assert name not in pinned


def test_oauth_pin_uses_canonical_core_names() -> None:
    pinned = _keyword_pinned_tool_names("please login with oauth")
    assert "core.oauth_login" in pinned
    assert "oauth_login" not in pinned
