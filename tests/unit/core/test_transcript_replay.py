"""
Motet - Transcript Replay Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for transcript replay ordering behavior.
    Verifies single-pass backend ordering (user -> sub-agents -> root assistant),
    legacy rows without sequence (sort to position 0), multi-turn replay, and
    tool-result adjacency when sub-agent tool transcripts reorder past a root
    synthesis assistant.

Dependencies:
    - pytest: test framework
    - motet.core.conversations.transcript_replay: replay under test
    - motet.core.types.Message: canonical message model

Usage:
    pytest tests/unit/core/test_transcript_replay.py -q

Notes:
    - Uses monkeypatched transcript codec/render helpers to keep these tests
      deterministic and focused on replay ordering logic only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import pytest

from motet.core.conversations import transcript_replay
from motet.core.conversations import transcript_codec, transcript_rendering
from motet.core.types import Message


def _fake_motet(memories: List[Any]) -> Any:
    memory = SimpleNamespace(
        recall_conversation=lambda conversation_id, types, limit: memories,
    )
    stack = SimpleNamespace(config=SimpleNamespace(model_provider="openai"))
    return SimpleNamespace(memory=memory, stack=stack)


def _patch_passthrough_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript_codec, "deserialize_transcript_items", lambda raw: raw)
    monkeypatch.setattr(
        transcript_rendering,
        "render_transcript_items_to_messages",
        lambda items, provider_name=None, turn_agent_id=None: [Message.model_validate(i) for i in items],
    )


def test_replay_orders_root_after_subagents_within_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_passthrough_render(monkeypatch)
    memories = [
        # Root transcript appears first by sequence (deterministic reservation path).
        SimpleNamespace(
            created_at=1.0,
            metadata={
                "sequence": 1,
                "agent_id": "core.default",
                "root_agent_id": "core.default",
                "root_turn": True,
                "items": [
                    {"role": "user", "content": "u1"},
                    {"role": "assistant", "content": "final"},
                ],
            },
        ),
        # Sub-agents finalize later with higher sequence values.
        SimpleNamespace(
            created_at=2.0,
            metadata={
                "sequence": 2,
                "agent_id": "expert-panel.a",
                "root_agent_id": "core.default",
                "root_turn": False,
                "items": [{"role": "assistant", "content": "sub-a"}],
            },
        ),
        SimpleNamespace(
            created_at=3.0,
            metadata={
                "sequence": 3,
                "agent_id": "expert-panel.b",
                "root_agent_id": "core.default",
                "root_turn": False,
                "items": [{"role": "assistant", "content": "sub-b"}],
            },
        ),
    ]
    motet = _fake_motet(memories)

    out = transcript_replay.get_conversation_history_from_transcripts(motet, "conv-1")
    msgs = [m for _, m in out]

    assert [m.role for m in msgs] == ["user", "assistant", "assistant", "assistant"]
    assert [m.content for m in msgs] == ["u1", "sub-a", "sub-b", "final"]


def test_replay_attaches_pre_user_assistants_to_first_user_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_passthrough_render(monkeypatch)
    memories = [
        SimpleNamespace(
            created_at=1.0,
            metadata={"sequence": 1, "items": [{"role": "assistant", "content": "sub-a"}]},
        ),
        SimpleNamespace(
            created_at=2.0,
            metadata={"sequence": 2, "items": [{"role": "assistant", "content": "sub-b"}]},
        ),
        SimpleNamespace(
            created_at=3.0,
            metadata={
                "sequence": 3,
                "agent_id": "core.default",
                "root_agent_id": "core.default",
                "root_turn": True,
                "items": [
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "final"},
                ],
            },
        ),
    ]
    motet = _fake_motet(memories)

    out = transcript_replay.get_conversation_history_from_transcripts(motet, "conv-2")
    msgs = [m for _, m in out]

    assert [m.role for m in msgs] == ["user", "assistant", "assistant", "assistant"]
    assert [m.content for m in msgs] == ["question", "sub-a", "sub-b", "final"]


def test_replay_legacy_row_without_sequence_sorts_to_position_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy rows (no sequence) get sort key 0 and appear before sequenced rows."""
    _patch_passthrough_render(monkeypatch)
    memories = [
        SimpleNamespace(
            created_at=5.0,
            metadata={"sequence": 10, "items": [{"role": "assistant", "content": "seq-row"}]},
        ),
        SimpleNamespace(
            created_at=1.0,
            metadata={"items": [{"role": "assistant", "content": "legacy-row"}]},
        ),
    ]
    motet = _fake_motet(memories)

    out = transcript_replay.get_conversation_history_from_transcripts(motet, "conv-3")
    msgs = [m for _, m in out]

    assert [m.content for m in msgs] == ["legacy-row", "seq-row"]


def test_replay_invalid_sequences_get_sort_key_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-integer sequences are treated as 0; stable sort preserves input order among ties."""
    _patch_passthrough_render(monkeypatch)
    memories = [
        SimpleNamespace(
            created_at=5.0,
            metadata={"sequence": "bad-seq", "items": [{"role": "assistant", "content": "first-in"}]},
        ),
        SimpleNamespace(
            created_at=1.0,
            metadata={"sequence": "also-bad", "items": [{"role": "assistant", "content": "second-in"}]},
        ),
    ]
    motet = _fake_motet(memories)
    out = transcript_replay.get_conversation_history_from_transcripts(motet, "conv-4")
    msgs = [m for _, m in out]
    # Both get sequence 0; Python stable sort preserves input order.
    assert [m.content for m in msgs] == ["first-in", "second-in"]


def test_replay_multi_turn_with_panel_agents_preserves_per_turn_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two user turns each with sub-agents; root assistant follows sub-agents in each turn."""
    _patch_passthrough_render(monkeypatch)
    memories = [
        # Turn 1: root reserves seq=1, sub-agents finish later with seq=2,3
        SimpleNamespace(
            created_at=1.0,
            metadata={
                "sequence": 1,
                "agent_id": "core.default",
                "root_agent_id": "core.default",
                "root_turn": True,
                "items": [
                    {"role": "user", "content": "turn-1-question"},
                    {"role": "assistant", "content": "turn-1-synthesis"},
                ],
            },
        ),
        SimpleNamespace(
            created_at=2.0,
            metadata={
                "sequence": 2,
                "agent_id": "expert-panel.a",
                "root_turn": False,
                "items": [{"role": "assistant", "content": "turn-1-sub-a"}],
            },
        ),
        SimpleNamespace(
            created_at=3.0,
            metadata={
                "sequence": 3,
                "agent_id": "expert-panel.b",
                "root_turn": False,
                "items": [{"role": "assistant", "content": "turn-1-sub-b"}],
            },
        ),
        # Turn 2: root reserves seq=4, sub-agents finish later with seq=5,6
        SimpleNamespace(
            created_at=4.0,
            metadata={
                "sequence": 4,
                "agent_id": "core.default",
                "root_agent_id": "core.default",
                "root_turn": True,
                "items": [
                    {"role": "user", "content": "turn-2-question"},
                    {"role": "assistant", "content": "turn-2-synthesis"},
                ],
            },
        ),
        SimpleNamespace(
            created_at=5.0,
            metadata={
                "sequence": 5,
                "agent_id": "expert-panel.a",
                "root_turn": False,
                "items": [{"role": "assistant", "content": "turn-2-sub-a"}],
            },
        ),
        SimpleNamespace(
            created_at=6.0,
            metadata={
                "sequence": 6,
                "agent_id": "expert-panel.b",
                "root_turn": False,
                "items": [{"role": "assistant", "content": "turn-2-sub-b"}],
            },
        ),
    ]
    motet = _fake_motet(memories)

    out = transcript_replay.get_conversation_history_from_transcripts(motet, "conv-5")
    msgs = [m for _, m in out]

    assert [m.role for m in msgs] == [
        "user", "assistant", "assistant", "assistant",
        "user", "assistant", "assistant", "assistant",
    ]
    assert [m.content for m in msgs] == [
        "turn-1-question", "turn-1-sub-a", "turn-1-sub-b", "turn-1-synthesis",
        "turn-2-question", "turn-2-sub-a", "turn-2-sub-b", "turn-2-synthesis",
    ]


def test_replay_assistant_only_conversation_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge case: no user messages at all — assistant rows should be preserved in sequence order."""
    _patch_passthrough_render(monkeypatch)
    memories = [
        SimpleNamespace(
            created_at=1.0,
            metadata={"sequence": 1, "items": [{"role": "assistant", "content": "first"}]},
        ),
        SimpleNamespace(
            created_at=2.0,
            metadata={"sequence": 2, "items": [{"role": "assistant", "content": "second"}]},
        ),
    ]
    motet = _fake_motet(memories)

    out = transcript_replay.get_conversation_history_from_transcripts(motet, "conv-6")
    msgs = [m for _, m in out]

    assert [m.content for m in msgs] == ["first", "second"]


def test_replay_keeps_tool_results_glued_when_reordering_past_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub-agent tool_calls + tools stay adjacent when moved before root synthesis.

    Root transcripts often reserve a lower sequence than later sub-agent
    finalizations. Reorder must not emit assistant(tool_calls), root text, then
    stranded tool results (DeepSeek/OpenAI 400).
    """
    _patch_passthrough_render(monkeypatch)
    tool_calls = [
        {
            "call_id": "call_1",
            "tool_name": "Read",
            "arguments_json": "{}",
        }
    ]
    memories = [
        SimpleNamespace(
            created_at=1.0,
            metadata={
                "sequence": 1,
                "agent_id": "core.default",
                "root_agent_id": "core.default",
                "root_turn": True,
                "items": [
                    {"role": "user", "content": "inspect"},
                    {"role": "assistant", "content": "synthesis"},
                ],
            },
        ),
        SimpleNamespace(
            created_at=2.0,
            metadata={
                "sequence": 2,
                "agent_id": "expert-panel.a",
                "root_agent_id": "core.default",
                "root_turn": False,
                "items": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls_canonical": tool_calls,
                    },
                    {
                        "role": "tool",
                        "content": "file body",
                        "tool_call_id": "call_1",
                        "name": "Read",
                    },
                ],
            },
        ),
    ]
    motet = _fake_motet(memories)

    out = transcript_replay.get_conversation_history_from_transcripts(motet, "conv-tools")
    msgs = [m for _, m in out]

    assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[1].tool_calls_canonical
    assert msgs[2].tool_call_id == "call_1"
    assert msgs[3].content == "synthesis"


def test_replay_stamps_parent_agent_id_on_nested_assistant(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_passthrough_render(monkeypatch)
    memories = [
        SimpleNamespace(
            created_at=1.0,
            metadata={
                "sequence": 1,
                "agent_id": "core.default.spawn-1",
                "root_agent_id": "core.default",
                "parent_agent_id": "core.default",
                "root_turn": False,
                "items": [{"role": "assistant", "content": "price is 12", "agent_id": "core.default.spawn-1"}],
            },
        ),
    ]
    motet = _fake_motet(memories)
    out = transcript_replay.get_conversation_history_from_transcripts(motet, "conv-parent")
    msg = out[0][1]
    assert msg.parent_agent_id == "core.default"
    item = transcript_replay.message_to_history_item(msg, "2026-08-25T00:00:00Z")
    assert item is not None
    assert item["agent_id"] == "core.default.spawn-1"
    assert item["parent_agent_id"] == "core.default"
