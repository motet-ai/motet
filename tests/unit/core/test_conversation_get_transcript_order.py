"""
Motet - conversation_get Transcript Order Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Integration-style unit test for conversation_get history replay ordering.
    Simulates panel-agent transcript completion before the outer turn transcript
    and verifies API history is reconstructed as:
    user -> panel agents -> default agent assistant.

Dependencies:
    - pytest
    - motet.core.commands.builtin.conversation
    - motet.core.conversations.transcript_codec
    - motet.core.types.Message

Usage:
    pytest tests/unit/core/test_conversation_get_transcript_order.py -q
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch

import pytest

from motet.core.conversations.transcript_codec import build_transcript_items_for_turn, serialize_transcript_items
from motet.core.commands.builtin import conversation as conversation_module
from motet.core.commands.command_data_classes import GetConversationData
from motet.core.commands.builtin.conversation import conversation_get
from motet.core.types import Message


@pytest.fixture(autouse=True)
def _caller_owns_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    conversation_get authorizes conversation ownership (issue #139).

    These cases build transcripts directly in a fake memory rather than going
    through agent_turn, so no ownership record exists and the command would
    return empty history. Treat the caller as the owner so the assertions stay
    focused on transcript ordering.
    """
    monkeypatch.setattr(
        conversation_module,
        "authorize_conversation_access_sync",
        lambda **kwargs: kwargs.get("principal_id"),
    )


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@dataclass
class FakeMemoryItem:
    created_at: datetime
    metadata: dict


class FakeConversationMemory:
    def __init__(self, items: List[FakeMemoryItem]) -> None:
        self._items = items

    def recall_conversation(self, *, conversation_id: str, types: List[str], limit: int) -> List[FakeMemoryItem]:
        if "conversation_transcript" not in types:
            return []
        filtered = [it for it in self._items if (it.metadata or {}).get("conversation_id") == conversation_id]
        return filtered[:limit]


class FakeStackMemory:
    def recent(self, limit: int = 100) -> List[Any]:
        return []


def test_conversation_get_reconstructs_panel_turn_order() -> None:
    conversation_id = "conv-panel"

    panel_optimist = serialize_transcript_items(
        build_transcript_items_for_turn(
            [],
            [],
            assistant_response="Optimist analysis",
            agent_id="expert-panel.optimist",
        )
    )
    panel_skeptic = serialize_transcript_items(
        build_transcript_items_for_turn(
            [],
            [],
            assistant_response="Skeptic analysis",
            agent_id="expert-panel.skeptic",
        )
    )
    outer_turn = serialize_transcript_items(
        build_transcript_items_for_turn(
            [Message(role="user", content="Should we launch this feature?")],
            [],
            assistant_response="Synthesis recommendation",
            agent_id="core.default",
        )
    )

    # Intentionally unsorted by created_at; replay should sort by metadata.sequence.
    fake_items = [
        FakeMemoryItem(
            created_at=_dt(30),
            metadata={
                "conversation_id": conversation_id,
                "sequence": 12,
                "agent_id": "core.default",
                "items": outer_turn,
            },
        ),
        FakeMemoryItem(
            created_at=_dt(10),
            metadata={
                "conversation_id": conversation_id,
                "sequence": 10,
                "agent_id": "expert-panel.optimist",
                "items": panel_optimist,
            },
        ),
        FakeMemoryItem(
            created_at=_dt(20),
            metadata={
                "conversation_id": conversation_id,
                "sequence": 11,
                "agent_id": "expert-panel.skeptic",
                "items": panel_skeptic,
            },
        ),
    ]

    fake_motet = SimpleNamespace(
        motet_id="default",
        tenant_id="tenant-1",
        principal_id="principal-1",
        conversation_id=conversation_id,
        memory=FakeConversationMemory(fake_items),
        stack=SimpleNamespace(memory=FakeStackMemory(), vector=None),
    )

    with patch("motet.core.commands.builtin.conversation.get_motet_context", return_value=fake_motet):
        out = conversation_get.__wrapped__(GetConversationData(conversation_id=conversation_id))

    history = out["history"]
    assert [h["role"] for h in history] == ["user", "assistant", "assistant", "assistant"]
    assert [h.get("agent_id") for h in history] == [None, "expert-panel.optimist", "expert-panel.skeptic", "core.default"]
    assert [h["content"] for h in history] == [
        "Should we launch this feature?",
        "Optimist analysis",
        "Skeptic analysis",
        "Synthesis recommendation",
    ]


def test_conversation_get_places_root_assistant_after_subagents_when_root_sequence_is_first() -> None:
    conversation_id = "conv-panel-root-first"

    # Root turn reserved first; user+root assistant share this transcript timestamp.
    root_turn = serialize_transcript_items(
        build_transcript_items_for_turn(
            [Message(role="user", content="What is the recommendation?")],
            [],
            assistant_response="Final synthesis from default",
            agent_id="core.default",
        )
    )
    panel_a = serialize_transcript_items(
        build_transcript_items_for_turn(
            [],
            [],
            assistant_response="Panel agent A output",
            agent_id="expert-panel.optimist",
        )
    )
    panel_b = serialize_transcript_items(
        build_transcript_items_for_turn(
            [],
            [],
            assistant_response="Panel agent B output",
            agent_id="expert-panel.skeptic",
        )
    )

    # Root sequence is first; sub-agents complete later with higher sequences.
    fake_items = [
        FakeMemoryItem(
            created_at=_dt(10),
            metadata={
                "conversation_id": conversation_id,
                "sequence": 100,
                "agent_id": "core.default",
                "root_turn": True,
                "items": root_turn,
            },
        ),
        FakeMemoryItem(
            created_at=_dt(20),
            metadata={
                "conversation_id": conversation_id,
                "sequence": 101,
                "agent_id": "expert-panel.optimist",
                "root_turn": False,
                "items": panel_a,
            },
        ),
        FakeMemoryItem(
            created_at=_dt(30),
            metadata={
                "conversation_id": conversation_id,
                "sequence": 102,
                "agent_id": "expert-panel.skeptic",
                "root_turn": False,
                "items": panel_b,
            },
        ),
    ]

    fake_motet = SimpleNamespace(
        motet_id="default",
        tenant_id="tenant-1",
        principal_id="principal-1",
        conversation_id=conversation_id,
        memory=FakeConversationMemory(fake_items),
        stack=SimpleNamespace(memory=FakeStackMemory(), vector=None),
    )

    with patch("motet.core.commands.builtin.conversation.get_motet_context", return_value=fake_motet):
        out = conversation_get.__wrapped__(GetConversationData(conversation_id=conversation_id))

    history = out["history"]
    assert [h["role"] for h in history] == ["user", "assistant", "assistant", "assistant"]
    assert [h.get("agent_id") for h in history] == [None, "expert-panel.optimist", "expert-panel.skeptic", "core.default"]
    assert [h["content"] for h in history] == [
        "What is the recommendation?",
        "Panel agent A output",
        "Panel agent B output",
        "Final synthesis from default",
    ]


def test_conversation_get_uses_root_agent_id_when_root_turn_flag_missing() -> None:
    conversation_id = "conv-panel-root-agent-id-only"

    root_turn = serialize_transcript_items(
        build_transcript_items_for_turn(
            [Message(role="user", content="Give me the final answer.")],
            [],
            assistant_response="Root final answer",
            agent_id="core.default",
        )
    )
    panel_turn = serialize_transcript_items(
        build_transcript_items_for_turn(
            [],
            [],
            assistant_response="Panel evidence",
            agent_id="expert-panel.optimist",
        )
    )

    # root_turn intentionally omitted to verify root_agent_id fallback.
    fake_items = [
        FakeMemoryItem(
            created_at=_dt(10),
            metadata={
                "conversation_id": conversation_id,
                "sequence": 200,
                "agent_id": "core.default",
                "root_agent_id": "core.default",
                "items": root_turn,
            },
        ),
        FakeMemoryItem(
            created_at=_dt(20),
            metadata={
                "conversation_id": conversation_id,
                "sequence": 201,
                "agent_id": "expert-panel.optimist",
                "root_agent_id": "core.default",
                "items": panel_turn,
            },
        ),
    ]

    fake_motet = SimpleNamespace(
        motet_id="default",
        tenant_id="tenant-1",
        principal_id="principal-1",
        conversation_id=conversation_id,
        memory=FakeConversationMemory(fake_items),
        stack=SimpleNamespace(memory=FakeStackMemory(), vector=None),
    )

    with patch("motet.core.commands.builtin.conversation.get_motet_context", return_value=fake_motet):
        out = conversation_get.__wrapped__(GetConversationData(conversation_id=conversation_id))

    history = out["history"]
    assert [h["role"] for h in history] == ["user", "assistant", "assistant"]
    assert [h.get("agent_id") for h in history] == [None, "expert-panel.optimist", "core.default"]
    assert [h["content"] for h in history] == [
        "Give me the final answer.",
        "Panel evidence",
        "Root final answer",
    ]


def test_conversation_get_handles_subagent_rows_that_also_contain_user_messages() -> None:
    conversation_id = "conv-panel-subrows-with-user"

    root_row = serialize_transcript_items(
        build_transcript_items_for_turn(
            [Message(role="user", content="Evaluate this launch")],
            [],
            assistant_response="Root synthesis",
            agent_id="core.default",
        )
    )
    # Realistic edge case: sub-agent rows can include current user in their turn delta.
    panel_row_a = serialize_transcript_items(
        build_transcript_items_for_turn(
            [Message(role="user", content="Evaluate this launch")],
            [],
            assistant_response="Panel A view",
            agent_id="expert-panel.optimist",
        )
    )
    panel_row_b = serialize_transcript_items(
        build_transcript_items_for_turn(
            [Message(role="user", content="Evaluate this launch")],
            [],
            assistant_response="Panel B view",
            agent_id="expert-panel.skeptic",
        )
    )

    fake_items = [
        FakeMemoryItem(
            created_at=_dt(10),
            metadata={
                "conversation_id": conversation_id,
                "sequence": 300,
                "agent_id": "core.default",
                "root_turn": True,
                "root_agent_id": "core.default",
                "items": root_row,
            },
        ),
        FakeMemoryItem(
            created_at=_dt(20),
            metadata={
                "conversation_id": conversation_id,
                "sequence": 301,
                "agent_id": "expert-panel.optimist",
                "root_turn": False,
                "root_agent_id": "core.default",
                "items": panel_row_a,
            },
        ),
        FakeMemoryItem(
            created_at=_dt(30),
            metadata={
                "conversation_id": conversation_id,
                "sequence": 302,
                "agent_id": "expert-panel.skeptic",
                "root_turn": False,
                "root_agent_id": "core.default",
                "items": panel_row_b,
            },
        ),
    ]

    fake_motet = SimpleNamespace(
        motet_id="default",
        tenant_id="tenant-1",
        principal_id="principal-1",
        conversation_id=conversation_id,
        memory=FakeConversationMemory(fake_items),
        stack=SimpleNamespace(memory=FakeStackMemory(), vector=None),
    )

    with patch("motet.core.commands.builtin.conversation.get_motet_context", return_value=fake_motet):
        out = conversation_get.__wrapped__(GetConversationData(conversation_id=conversation_id))

    history = out["history"]
    assert [h["role"] for h in history] == ["user", "assistant", "assistant", "assistant"]
    assert [h.get("agent_id") for h in history] == [None, "expert-panel.optimist", "expert-panel.skeptic", "core.default"]
    assert [h["content"] for h in history] == [
        "Evaluate this launch",
        "Panel A view",
        "Panel B view",
        "Root synthesis",
    ]


def test_conversation_get_warns_when_index_has_sealed_rows() -> None:
    conversation_id = "conv-sealed"

    class SealedIndexMemory:
        def conversation_index_count(self, cid: str) -> int:
            assert cid == conversation_id
            return 6

    fake_motet = SimpleNamespace(
        motet_id="default",
        tenant_id="tenant-1",
        principal_id="principal-1",
        conversation_id=conversation_id,
        memory=FakeConversationMemory([]),
        stack=SimpleNamespace(memory=SealedIndexMemory(), vector=None),
    )

    with patch("motet.core.commands.builtin.conversation.get_motet_context", return_value=fake_motet):
        out = conversation_get.__wrapped__(GetConversationData(conversation_id=conversation_id))

    assert out["history"] == []
    assert out["counts"]["memory"] == 6
    assert out["warning"]
    assert "cannot be decrypted" in out["warning"]


def test_conversation_get_includes_parent_conversation_id() -> None:
    conversation_id = "iso-child"
    fake_motet = SimpleNamespace(
        motet_id="default",
        tenant_id="tenant-1",
        principal_id="principal-1",
        conversation_id=conversation_id,
        memory=FakeConversationMemory([]),
        stack=SimpleNamespace(memory=FakeStackMemory(), vector=None),
    )

    with (
        patch("motet.core.commands.builtin.conversation.get_motet_context", return_value=fake_motet),
        patch(
            "motet.core.conversations.registry.get_conversation_sync",
            return_value={
                "id": conversation_id,
                "turn_agent_id": "core.subagent",
                "parent_conversation_id": "conv-parent",
            },
        ),
    ):
        out = conversation_get.__wrapped__(GetConversationData(conversation_id=conversation_id))

    assert out["turn_agent_id"] == "core.subagent"
    assert out["parent_conversation_id"] == "conv-parent"
