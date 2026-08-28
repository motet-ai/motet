"""
Motet - Chat Inbound Message agent_id Mapping Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Unit tests for optional inbound per-message agent_id on the native chat API
    and mapping into canonical core Message (issue #138 / ADR-0083).

Dependencies:
    - pytest: test framework
    - motet.interfaces.api.v1.chat: API Message and _to_core_messages
    - motet.core.types.Message: canonical message model

Usage:
    pytest tests/unit/interfaces/test_chat_message_agent_id.py -q
"""

from __future__ import annotations

from motet.core.types import Message as CoreMessage
from motet.interfaces.api.v1.chat import Message as ApiMessage
from motet.interfaces.api.v1.chat import _to_core_messages


def test_api_message_accepts_agent_id() -> None:
    msg = ApiMessage(role="assistant", content="hello", agent_id="core.default")
    assert msg.agent_id == "core.default"


def test_to_core_messages_preserves_agent_id() -> None:
    api_msgs = [
        ApiMessage(role="user", content="hi"),
        ApiMessage(role="assistant", content="hello", agent_id="core.default"),
        ApiMessage(role="user", content="next", attachments=[{"artifact_id": "a1"}]),
    ]
    core = _to_core_messages(api_msgs)
    assert all(isinstance(m, CoreMessage) for m in core)
    assert core[0].agent_id is None
    assert core[1].agent_id == "core.default"
    assert core[2].attachments == [{"artifact_id": "a1"}]


def test_to_core_messages_strips_blank_agent_id() -> None:
    core = _to_core_messages([ApiMessage(role="assistant", content="x", agent_id="  ")])
    assert core[0].agent_id is None
