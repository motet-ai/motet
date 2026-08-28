"""
Unit tests for canonical transcript Message.agent_id (ADR-0083).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-22

Description:
    Verifies build_transcript_items_for_turn attaches agent_id to assistant messages.

Dependencies:
    - pytest
    - motet.core.conversations.transcript_codec
    - motet.core.types

Usage:
    pytest tests/unit/core/test_transcript_agent_id.py -q
"""

from motet.core.conversations.transcript_codec import build_transcript_items_for_turn
from motet.core.types import Message


def test_build_transcript_items_sets_agent_id() -> None:
    items = build_transcript_items_for_turn(
        [Message(role="user", content="hi")],
        [],
        "hello back",
        agent_id="core.default",
    )
    assert items
    last = items[-1]
    assert isinstance(last, Message)
    assert last.role == "assistant"
    assert last.content == "hello back"
    assert last.agent_id == "core.default"


def test_build_transcript_items_omits_agent_when_none() -> None:
    items = build_transcript_items_for_turn(
        [Message(role="user", content="hi")],
        [],
        "hello back",
        agent_id=None,
    )
    last = items[-1]
    assert getattr(last, "agent_id", None) in (None, "")
