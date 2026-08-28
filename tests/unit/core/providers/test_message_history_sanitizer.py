"""
Motet - Provider Message History Sanitizer Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-29

Description:
    Unit tests for provider-boundary orphan tool-call sanitization.
    Ensures malformed assistant tool-call spans are removed, valid
    tool-call + tool-result spans are preserved, transparent system noise
    between pairs is repaired into provider-valid adjacency, and stranded
    role=tool messages are dropped.

Dependencies:
    - pytest
    - motet.core.models.adapters.providers.message_history_sanitizer
    - motet.core.types.Message

Usage:
    pytest tests/unit/core/providers/test_message_history_sanitizer.py
"""

from __future__ import annotations

from motet.core.models.adapters.providers.message_history_sanitizer import (
    sanitize_orphan_tool_call_messages,
)
from motet.core.types import Message


def test_sanitizer_removes_orphan_assistant_tool_call_block() -> None:
    messages = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[{"call_id": "call_1", "tool_name": "tool.a", "arguments_json": "{}"}],
        ),
        Message(role="assistant", content="final"),
    ]

    sanitized, stats = sanitize_orphan_tool_call_messages(messages)

    assert [m.role for m in sanitized] == ["user", "assistant"]
    assert stats["removed_assistant_calls"] == 1
    assert stats["removed_tool_messages"] == 0


def test_sanitizer_keeps_valid_assistant_tool_call_block() -> None:
    messages = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[{"call_id": "call_1", "tool_name": "tool.a", "arguments_json": "{}"}],
        ),
        Message(role="tool", content="ok", tool_call_id="call_1"),
        Message(role="assistant", content="final"),
    ]

    sanitized, stats = sanitize_orphan_tool_call_messages(messages)

    assert [m.role for m in sanitized] == ["user", "assistant", "tool", "assistant"]
    assert stats["removed_assistant_calls"] == 0
    assert stats["removed_tool_messages"] == 0


def test_sanitizer_repairs_system_between_assistant_and_tools() -> None:
    """System noise between tool_calls and results must not orphan the tools."""
    messages = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[{"call_id": "call_1", "tool_name": "tool.a", "arguments_json": "{}"}],
        ),
        Message(role="system", content="Relevant context from memory"),
        Message(role="tool", content="ok", tool_call_id="call_1"),
        Message(role="assistant", content="final"),
    ]

    sanitized, stats = sanitize_orphan_tool_call_messages(messages)

    assert [m.role for m in sanitized] == ["user", "assistant", "tool", "system", "assistant"]
    assert sanitized[1].tool_calls_canonical
    assert sanitized[2].tool_call_id == "call_1"
    assert stats["removed_assistant_calls"] == 0
    assert stats["removed_tool_messages"] == 0


def test_sanitizer_drops_orphan_tool_messages() -> None:
    """Stranded role=tool after a non-tool_calls assistant must be removed."""
    messages = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[{"call_id": "call_1", "tool_name": "tool.a", "arguments_json": "{}"}],
        ),
        Message(role="assistant", content="interrupted"),
        Message(role="tool", content="ok", tool_call_id="call_1"),
    ]

    sanitized, stats = sanitize_orphan_tool_call_messages(messages)

    assert [m.role for m in sanitized] == ["user", "assistant"]
    assert sanitized[1].content == "interrupted"
    assert stats["removed_assistant_calls"] == 1
    assert stats["removed_tool_messages"] == 1
