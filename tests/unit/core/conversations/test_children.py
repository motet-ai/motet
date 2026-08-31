"""
Motet - Child Conversation Lifecycle Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Unit tests for motet.core.conversations.children — the reusable
    mint / claim+register / brief / reply / card-pointer lifecycle that
    fan-outs (core.spawn_agents today) bracket around a child agent_loop.

Dependencies:
    - unittest.mock: patch the transcript, registry, ownership, and lineage
      writers at their source modules (the helpers import at call time)

Usage:
    pytest tests/unit/core/conversations/test_children.py -q

Notes:
    - Registration and brief failures must be fail-soft: the returned
      ChildConversation still carries a usable isolated id.
    - complete_child_conversation returns None on a failed reply write so a
      fan-out can degrade to pointer-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

from motet.core.conversations.children import (
    ChildConversation,
    child_pointer,
    complete_child_conversation,
    create_child_conversation,
    hydrate_spawn_children,
    parent_registry_scope,
    spawn_contract_for_followup,
)


class _FakeMotet:
    def __init__(self, metadata: Optional[Dict[str, Any]] = None):
        self.metadata = metadata if metadata is not None else {}
        self.conversation_id = "conv-1"
        self.tenant_id = "t1"
        self.principal_id = "p1"
        self.motet_id = "default"
        self.memory = object()


def test_parent_registry_scope_prefers_configured_agent_and_surface():
    motet = _FakeMotet(
        metadata={
            "configured_agent_qualified_id": "celebs.elvis",
            "surface_id": "demo_chat",
        }
    )
    assert parent_registry_scope(motet, "core.default") == ("celebs.elvis", "demo_chat")

    bare = _FakeMotet(metadata={})
    assert parent_registry_scope(bare, "") == ("core.default", None)


def test_child_pointer_shape():
    pointer = child_pointer(
        child_cid="iso-abc",
        agent_id="core.default.spawn-1",
        title="research  pricing",
        preview="  price is   12 ",
        cost_usd=0.004,
    )
    assert pointer["child_conversation_id"] == "iso-abc"
    assert pointer["agent_id"] == "core.default.spawn-1"
    assert pointer["preview"] == "price is 12"
    assert pointer["cost_usd"] == 0.004
    assert "thinking_text" not in pointer
    assert "tool_summaries" not in pointer

    with_rail = child_pointer(
        child_cid="iso-abc",
        agent_id="core.default.spawn-1",
        title="research",
        preview="price is 12",
        cost_usd=None,
        thinking_text="look it up",
        tool_summaries=[{"tool_name": "core.web_search", "status": "success"}],
    )
    assert with_rail["thinking_text"] == "look it up"
    assert with_rail["tool_summaries"] == [{"tool_name": "core.web_search", "status": "success"}]

    bare = child_pointer(
        child_cid="iso-abc", agent_id="a", title="t", preview="", cost_usd=None
    )
    assert "preview" not in bare
    assert "cost_usd" not in bare


def test_create_child_conversation_mints_registers_and_briefs():
    motet = _FakeMotet(metadata={"root_conversation_id": "root-1"})
    stored: List[Dict[str, Any]] = []
    registered: List[Dict[str, Any]] = []
    claimed: List[str] = []

    with (
        patch(
            "motet.core.conversations.lineage.record_conversation_lineage_sync",
            return_value="root-1",
        ),
        patch(
            "motet.core.conversations.transcript_storage.store_turn_transcript",
            side_effect=lambda m, msgs, text, **kw: stored.append(
                {"messages": msgs, "text": text, **kw}
            )
            or {},
        ),
        patch(
            "motet.core.conversations.registry.register_or_touch_conversation_sync",
            side_effect=lambda *a, **kw: registered.append({"args": a, **kw}),
        ),
        patch(
            "motet.core.conversations.ownership.authorize_conversation_access_sync",
            side_effect=lambda **kw: claimed.append(kw["conversation_id"]) or True,
        ),
    ):
        child = create_child_conversation(
            motet,
            instruction="research pricing",
            registry_agent_id="core.default",
            pointer_agent_id="core.default.spawn-1",
            surface_id="demo_chat",
            kind="spawn",
        )

    assert child.conversation_id.startswith("iso-")
    assert child.parent_conversation_id == "conv-1"
    assert child.root_conversation_id == "root-1"
    assert child.brief_written is True
    assert claimed == [child.conversation_id]
    assert registered[0]["args"][3] == child.conversation_id
    assert registered[0]["parent_conversation_id"] == "conv-1"
    assert registered[0]["root_conversation_id"] == "root-1"
    # The brief is the child's first user message with an empty assistant.
    assert stored[0]["conversation_id"] == child.conversation_id
    assert stored[0]["messages"][0].content == "research pricing"
    assert stored[0]["text"] == ""
    assert stored[0]["root_turn"] is True
    assert stored[0]["include_tool_invocations"] is False
    # Early pointer has no preview or cost yet.
    assert child.turn_agent_id == "core.subagent"
    assert registered[0]["turn_agent_id"] == "core.subagent"
    assert child.pointer == {
        "child_conversation_id": child.conversation_id,
        "agent_id": "core.default.spawn-1",
        "title": "research pricing",
        "turn_agent_id": "core.subagent",
    }


def test_create_child_conversation_is_fail_soft_on_register_and_brief():
    motet = _FakeMotet()

    with (
        patch(
            "motet.core.conversations.lineage.record_conversation_lineage_sync",
            return_value="conv-1",
        ),
        patch(
            "motet.core.conversations.ownership.authorize_conversation_access_sync",
            side_effect=RuntimeError("redis down"),
        ),
        patch(
            "motet.core.conversations.transcript_storage.store_turn_transcript",
            side_effect=RuntimeError("store down"),
        ),
    ):
        child = create_child_conversation(
            motet,
            instruction="research pricing",
            registry_agent_id="core.default",
            pointer_agent_id="core.default.spawn-1",
        )

    assert child.conversation_id.startswith("iso-")
    assert child.brief_written is False


def test_complete_child_conversation_persists_reply_and_returns_pointer():
    motet = _FakeMotet()
    stored: List[Dict[str, Any]] = []

    with (
        patch(
            "motet.core.conversations.transcript_storage.store_turn_transcript",
            side_effect=lambda m, msgs, text, **kw: stored.append(
                {"messages": msgs, "text": text, **kw}
            )
            or {},
        ),
        patch(
            "motet.core.conversations.registry.register_or_touch_conversation_sync",
            return_value=None,
        ),
        patch(
            "motet.core.conversations.ownership.authorize_conversation_access_sync",
            return_value=True,
        ),
    ):
        pointer = complete_child_conversation(
            motet,
            child_cid="iso-abc",
            reply_text="price is 12",
            instruction="research pricing",
            registry_agent_id="core.default",
            pointer_agent_id="core.default.spawn-1",
            brief_written=True,
            thinking_text="look it up",
            tool_summaries=[{"tool_name": "core.web_search"}],
            cost_usd=0.004,
        )

    assert pointer is not None
    assert pointer["child_conversation_id"] == "iso-abc"
    assert pointer["preview"] == "price is 12"
    assert pointer["cost_usd"] == 0.004
    assert pointer["thinking_text"] == "look it up"
    assert pointer["tool_summaries"] == [{"tool_name": "core.web_search", "status": "success"}]
    assert pointer["agent_id"] == "core.default.spawn-1"
    assert pointer["turn_agent_id"] == "core.subagent"
    assert stored[0]["agent_id"] == "core.subagent"
    # Brief already written: reply row only, not a root turn, no inline user.
    assert stored[0]["messages"] == []
    assert stored[0]["root_turn"] is False
    assert stored[0]["thinking_text"] == "look it up"
    assert stored[0]["cost_usd"] == 0.004


def test_complete_child_conversation_inlines_brief_when_missing():
    motet = _FakeMotet()
    stored: List[Dict[str, Any]] = []

    with (
        patch(
            "motet.core.conversations.transcript_storage.store_turn_transcript",
            side_effect=lambda m, msgs, text, **kw: stored.append(
                {"messages": msgs, "text": text, **kw}
            )
            or {},
        ),
        patch(
            "motet.core.conversations.registry.register_or_touch_conversation_sync",
            return_value=None,
        ),
        patch(
            "motet.core.conversations.ownership.authorize_conversation_access_sync",
            return_value=True,
        ),
    ):
        pointer = complete_child_conversation(
            motet,
            child_cid="iso-abc",
            reply_text="price is 12",
            instruction="research pricing",
            registry_agent_id="core.default",
            pointer_agent_id="core.default.spawn-1",
            brief_written=False,
        )

    assert pointer is not None
    assert stored[0]["messages"][0].content == "research pricing"
    assert stored[0]["root_turn"] is True


def test_complete_child_conversation_returns_none_on_write_failure():
    motet = _FakeMotet()

    with patch(
        "motet.core.conversations.transcript_storage.store_turn_transcript",
        side_effect=RuntimeError("store down"),
    ):
        pointer = complete_child_conversation(
            motet,
            child_cid="iso-abc",
            reply_text="price is 12",
            registry_agent_id="core.default",
            pointer_agent_id="core.default.spawn-1",
        )

    assert pointer is None


def test_complete_child_conversation_skips_empty_reply_and_missing_memory():
    motet = _FakeMotet()
    assert (
        complete_child_conversation(
            motet,
            child_cid="iso-abc",
            reply_text="   ",
            registry_agent_id="core.default",
            pointer_agent_id="a",
        )
        is None
    )

    no_memory = _FakeMotet()
    no_memory.memory = None
    assert (
        complete_child_conversation(
            no_memory,
            child_cid="iso-abc",
            reply_text="answer",
            registry_agent_id="core.default",
            pointer_agent_id="a",
        )
        is None
    )


def test_child_conversation_dataclass_is_frozen():
    child = ChildConversation(
        conversation_id="iso-abc",
        parent_conversation_id="conv-1",
        root_conversation_id="conv-1",
        title="t",
        registry_agent_id="core.default",
        pointer_agent_id="core.default.spawn-1",
        turn_agent_id="core.subagent",
        surface_id=None,
        brief_written=False,
    )
    try:
        child.conversation_id = "other"  # type: ignore[misc]
        assert False, "expected FrozenInstanceError"
    except Exception:
        pass


class _RecallMemory:
    def __init__(self, rows: List[Any]):
        self.rows = rows
        self.called_with: List[Dict[str, Any]] = []

    def recall_conversation(self, **kwargs: Any) -> List[Any]:
        self.called_with.append(kwargs)
        return list(self.rows)


def test_hydrate_spawn_children_fills_from_child_transcript():
    from types import SimpleNamespace

    motet = _FakeMotet()
    motet.memory = _RecallMemory(
        [
            SimpleNamespace(
                metadata={
                    "thinking_text": "look it up",
                    "tool_summaries": [
                        {
                            "tool_name": "core.web_search",
                            "status": "success",
                            "preview": "list price",
                            "step": 1,
                        }
                    ],
                    "cost_usd": 0.004,
                }
            )
        ]
    )
    cards = [
        {
            "child_conversation_id": "iso-abc",
            "agent_id": "core.default.spawn-1",
            "title": "research pricing",
            "preview": "price is 12",
        }
    ]
    out = hydrate_spawn_children(motet, cards)
    assert motet.memory.called_with[0]["conversation_id"] == "iso-abc"
    assert out[0]["thinking_text"] == "look it up"
    assert out[0]["tool_summaries"] == [
        {"tool_name": "core.web_search", "status": "success", "preview": "list price", "step": 1}
    ]
    assert out[0]["cost_usd"] == 0.004


def test_hydrate_spawn_children_reads_serialized_memory_dicts():
    motet = _FakeMotet()
    motet.memory = _RecallMemory(
        [
            {
                "metadata": {
                    "thinking_text": "from a dict row",
                    "cost_usd": 0.003,
                }
            }
        ]
    )
    out = hydrate_spawn_children(
        motet,
        [
            {
                "child_conversation_id": "iso-abc",
                "agent_id": "core.default.spawn-1",
                "title": "research",
                "preview": "price is 12",
            }
        ],
    )
    assert out[0]["thinking_text"] == "from a dict row"
    assert out[0]["cost_usd"] == 0.003


def test_hydrate_spawn_children_keeps_filled_pointer():
    motet = _FakeMotet()
    motet.memory = _RecallMemory([])
    cards = [
        {
            "child_conversation_id": "iso-abc",
            "agent_id": "core.default.spawn-1",
            "title": "research pricing",
            "preview": "price is 12",
            "thinking_text": "already stored",
            "tool_summaries": [{"tool_name": "core.web_search", "status": "success"}],
            "cost_usd": 0.002,
        }
    ]
    out = hydrate_spawn_children(motet, cards)
    assert motet.memory.called_with == []
    assert out[0]["thinking_text"] == "already stored"


def test_spawn_contract_for_followup_only_when_turn_agent_matches():
    row = {
        "turn_agent_id": "core.subagent",
        "spawn_contract": {"discover": False, "tools": ["core.web_search"]},
    }
    assert spawn_contract_for_followup(row, "core.subagent") == row["spawn_contract"]
    assert spawn_contract_for_followup(row, "core.default") is None
    assert spawn_contract_for_followup({"spawn_contract": {}}, "core.subagent") == {}
    assert spawn_contract_for_followup(None, "core.subagent") is None
    assert spawn_contract_for_followup({"turn_agent_id": "core.subagent"}, "core.subagent") is None
