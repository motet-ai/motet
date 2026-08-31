"""
Tests for typed conversation history OpenAPI models (ADR-0083).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Validates ConversationHistoryMessage / Attachment and _coerce_conversation_history.

Usage:
    pytest tests/unit/interfaces/test_conversation_history_models.py -q
"""

from motet.interfaces.api.v1.conversations import (
    ConversationHistoryAttachment,
    ConversationHistoryMessage,
    ConversationItem,
    _coerce_conversation_history,
)


def test_attachment_accepts_bytes_key() -> None:
    a = ConversationHistoryAttachment.model_validate(
        {
            "artifact_id": "art-1",
            "content_type": "image/png",
            "filename": "a.png",
            "bytes": 42,
        }
    )
    assert a.size_bytes == 42
    d = a.model_dump(by_alias=True)
    assert d["bytes"] == 42


def test_conversation_item_accepts_parent_conversation_id() -> None:
    item = ConversationItem.model_validate(
        {
            "id": "iso-1",
            "title": "research",
            "created_at": 1.0,
            "updated_at": 2.0,
            "agent_id": "core.default",
            "turn_agent_id": "core.subagent",
            "parent_conversation_id": "conv-1",
            "spawn_contract": {"discover": False},
            "root_conversation_id": "conv-1",
        }
    )
    dumped = item.model_dump()
    assert item.parent_conversation_id == "conv-1"
    assert item.turn_agent_id == "core.subagent"
    assert "parent_conversation_id" in dumped
    assert "spawn_contract" not in dumped
    assert "root_conversation_id" not in dumped


def test_conversation_item_root_parent_is_null() -> None:
    item = ConversationItem.model_validate(
        {
            "id": "conv-1",
            "title": "Chat",
            "created_at": 1.0,
            "updated_at": 2.0,
            "parent_conversation_id": "",
        }
    )
    assert item.parent_conversation_id is None
    assert item.model_dump()["parent_conversation_id"] is None


def test_history_message_round_trip_with_agent_id() -> None:
    raw = {
        "content": "hello",
        "role": "assistant",
        "created_at": "2026-03-22T00:00:00",
        "agent_id": "core.default",
    }
    m = ConversationHistoryMessage.model_validate(raw)
    assert m.agent_id == "core.default"


def test_coerce_filters_invalid_and_keeps_valid() -> None:
    items = _coerce_conversation_history(
        [
            {"content": "a", "role": "user", "created_at": "t1"},
            "not-a-dict",
            {"content": "b", "role": "assistant", "created_at": "t2", "agent_id": "core.x"},
        ]
    )
    assert len(items) == 2
    assert items[1].agent_id == "core.x"
