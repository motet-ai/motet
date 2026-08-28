"""
Motet - Prepare Context Tool-Call Sanitizer Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-29

Description:
    Unit tests for orphan tool-call sanitization in `core.prepare_context`.
    Verifies malformed assistant tool-call spans are pruned while valid spans
    are preserved, preventing provider-side 400 errors for missing tool results.
    Uses the shared sanitizer delegated from orchestration.context.tool_calls.

Dependencies:
    - pytest for assertions and test execution
    - unittest.mock.patch for command context isolation
    - motet.core.orchestration.context.tool_calls sanitizer (ADR-0109 canonical home)
    - motet.core.orchestration.turn.phases prepare_context
    - motet.core.types.Message canonical message model

Usage:
    pytest tests/unit/core/orchestration/test_prepare_context_tool_call_sanitizer.py

Notes:
    - These tests exercise both the pure sanitizer helper and the integrated
      prepare_context flow after conversation history merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict
from unittest.mock import patch

from motet.core.commands.command_data_classes import PrepareContextData
from motet.core.orchestration.turn.phases import prepare_context
from motet.core.orchestration.context.tool_calls import (
    sanitize_orphan_tool_call_messages as _sanitize_orphan_tool_call_messages,
)
from motet.core.types import Message


class _MemoryStub:
    def recall_conversation(self, *args: Any, **kwargs: Any) -> list:
        return []


@dataclass
class _MotetStub:
    conversation_id: str = "conv-1"
    memory: Any = _MemoryStub()
    stack: Any = None
    artifact_store: Any = None
    task_id: str = "task-1"
    command_id: str = "cmd-1"
    tenant_id: str = "tenant-1"

    def log_fields(self, **extra: Any) -> Dict[str, Any]:
        return {"task_id": self.task_id, **extra}


def test_sanitize_orphan_tool_calls_removes_invalid_block() -> None:
    messages = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[{"call_id": "call-1", "tool_name": "tool.x", "arguments_json": "{}"}],
        ),
        Message(role="assistant", content="next reply"),
    ]

    sanitized, stats = _sanitize_orphan_tool_call_messages(messages)

    assert len(sanitized) == 2
    assert all(not getattr(msg, "tool_calls_canonical", None) for msg in sanitized)
    assert stats["removed_assistant_calls"] == 1
    assert stats["removed_tool_messages"] == 0


def test_sanitize_orphan_tool_calls_keeps_valid_block() -> None:
    messages = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[{"call_id": "call-1", "tool_name": "tool.x", "arguments_json": "{}"}],
        ),
        Message(role="tool", content='{"ok": true}', tool_call_id="call-1", name="tool.x"),
        Message(role="assistant", content="done"),
    ]

    sanitized, stats = _sanitize_orphan_tool_call_messages(messages)

    assert len(sanitized) == 4
    assert stats["removed_assistant_calls"] == 0
    assert stats["removed_tool_messages"] == 0


def test_prepare_context_prunes_orphaned_history_tool_calls() -> None:
    orphan_history = [
        (
            "2026-04-01T00:00:00Z",
            Message(
                role="assistant",
                content="",
                tool_calls_canonical=[{"call_id": "call-1", "tool_name": "tool.x", "arguments_json": "{}"}],
            ),
        )
    ]

    data = PrepareContextData(
        messages=[Message(role="user", content="copy the synthesis")],
        include_memory_recall=False,
    )

    with patch(
        "motet.core.orchestration.turn.phases.get_motet_context",
        return_value=_MotetStub(),
    ), patch(
        "motet.core.conversations.load_history",
        return_value=orphan_history,
    ):
        out = prepare_context.__wrapped__(data)

    prepared = out["prepared_messages"]
    assert prepared
    assert all(not msg.get("tool_calls") for msg in prepared if isinstance(msg, dict))
