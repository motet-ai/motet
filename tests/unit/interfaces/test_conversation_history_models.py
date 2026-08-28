"""
Tests for typed conversation history OpenAPI models (ADR-0083).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-22

Description:
    Validates ConversationHistoryMessage / Attachment and _coerce_conversation_history.

Usage:
    pytest tests/unit/interfaces/test_conversation_history_models.py -q
"""

from motet.interfaces.api.v1.conversations import (
    ConversationHistoryAttachment,
    ConversationHistoryMessage,
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
