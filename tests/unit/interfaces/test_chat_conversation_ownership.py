"""
Motet - Chat API Conversation Ownership Tests (issue #139)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Verifies that POST /api/v1/chat rejects a conversation_id owned by a
    different principal with HTTP 403, on both the streaming and non-streaming
    request shapes. The check runs before stack construction and dispatch,
    because a StreamingResponse cannot change status once headers are sent.

Dependencies:
    - pytest: test framework
    - motet.interfaces.api.v1.chat: endpoint under test
    - motet.core.conversations.ownership: ConversationAccessDenied

Usage:
    pytest tests/unit/interfaces/test_chat_conversation_ownership.py -q

Notes:
    - The endpoint coroutine is called directly with a stub principal; the
      ownership guard is monkeypatched so no Redis or worker stack is needed.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from motet.core.conversations.ownership import (
    ACCESS_DENIED_MESSAGE,
    ConversationAccessDenied,
)
from motet.interfaces.api.v1 import chat as chat_api


class _StubPrincipal:
    id = "service-account:attacker"
    tenant_id = "acme"
    motet_id = "default"
    roles: list[str] = []


def _deny_guard(**kwargs: Any) -> None:
    raise ConversationAccessDenied(
        ACCESS_DENIED_MESSAGE,
        conversation_id=kwargs.get("conversation_id"),
        principal_id=kwargs.get("principal_id"),
        owner_principal_id="service-account:victim",
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_chat_rejects_foreign_conversation_id(
    monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    """Cross-principal conversation_id must fail with 403, not a 200 body."""
    monkeypatch.setattr(chat_api, "require_not_owned_by_other_sync", _deny_guard)

    req = chat_api.ChatRequest(
        messages=[chat_api.Message(role="user", content="summarize this chat")],
        conversation_id="native-conv-1785172357",
        stream=stream,
    )

    with pytest.raises(HTTPException) as exc_info:
        await chat_api.chat(
            request=None,  # type: ignore[arg-type]
            req=req,
            x_api_key=None,
            role=None,
            principal=_StubPrincipal(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == ACCESS_DENIED_MESSAGE


async def test_chat_guard_runs_before_stack_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The 403 must be raised without building a stack or dispatching a command.

    Guards the ordering: once the streaming branch returns a StreamingResponse,
    the status code is fixed and a denial can only be an in-band SSE event.
    """
    seen: list[str] = []

    def _recording_guard(**kwargs: Any) -> None:
        seen.append(str(kwargs.get("conversation_id")))
        _deny_guard(**kwargs)

    monkeypatch.setattr(chat_api, "require_not_owned_by_other_sync", _recording_guard)

    req = chat_api.ChatRequest(
        messages=[chat_api.Message(role="user", content="hi")],
        conversation_id="native-conv-1785172357",
        stream=True,
    )

    with pytest.raises(HTTPException):
        await chat_api.chat(
            request=None,  # type: ignore[arg-type]
            req=req,
            x_api_key=None,
            role=None,
            principal=_StubPrincipal(),  # type: ignore[arg-type]
        )

    assert seen == ["native-conv-1785172357"]
