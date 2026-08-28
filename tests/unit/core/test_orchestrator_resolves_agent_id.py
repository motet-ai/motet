"""
Motet - Orchestrator Agent ID Resolution Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Verifies DistributedOrchestrator.stream_events resolves empty/null agent_id to
    core.default (and aliases to qualified ids) before dispatching core.agent_turn,
    so command Inputs never show agent_id=null.

Dependencies:
    - pytest
    - pytest-asyncio
    - motet.core.orchestration.orchestrator

Usage:
    pytest tests/unit/core/test_orchestrator_resolves_agent_id.py -q
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from motet.core.orchestration.orchestrator import DistributedOrchestrator
from motet.core.types import Message


async def _empty_stream(**_kwargs: Any) -> AsyncIterator[dict]:
    if False:  # pragma: no cover
        yield {}


@pytest.mark.asyncio
async def test_stream_events_resolves_null_agent_id_to_core_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = DistributedOrchestrator()
    captured: Dict[str, Any] = {}

    async def _fake_stream_agent_command(
        *,
        task_id: str,
        conversation_id: str,
        agent_id: Optional[str],
        messages: List[Message],
        context: dict,
    ) -> AsyncIterator[dict]:
        captured["agent_id"] = agent_id
        captured["context_agent_id"] = context.get("agent_id")
        async for item in _empty_stream():
            yield item

    monkeypatch.setattr(orch, "_stream_agent_command", _fake_stream_agent_command)

    stack = MagicMock()
    stack._current_trace_id = "t1"
    stack._current_conversation_id = "c1"

    events = [
        ev
        async for ev in orch.stream_events(
            stack,
            [Message(role="user", content="hi")],
            context={"conversation_id": "c1", "agent_id": None},
        )
    ]
    assert events == []
    assert captured["agent_id"] == "core.default"
    assert captured["context_agent_id"] == "core.default"


@pytest.mark.asyncio
async def test_stream_events_resolves_alias_agent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = DistributedOrchestrator()
    captured: Dict[str, Any] = {}

    async def _fake_stream_agent_command(
        *,
        task_id: str,
        conversation_id: str,
        agent_id: Optional[str],
        messages: List[Message],
        context: dict,
    ) -> AsyncIterator[dict]:
        captured["agent_id"] = agent_id
        captured["context_agent_id"] = context.get("agent_id")
        async for item in _empty_stream():
            yield item

    monkeypatch.setattr(orch, "_stream_agent_command", _fake_stream_agent_command)

    stack = MagicMock()
    stack._current_trace_id = "t1"

    _ = [
        ev
        async for ev in orch.stream_events(
            stack,
            [Message(role="user", content="hi")],
            context={"conversation_id": "c1", "agent_id": "default"},
        )
    ]
    assert captured["agent_id"] == "core.default"
    assert captured["context_agent_id"] == "core.default"
