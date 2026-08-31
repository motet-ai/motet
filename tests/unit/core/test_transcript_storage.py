"""
Motet - Transcript Storage Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-30

Description:
    Verifies deterministic transcript sequence behavior and finalize storage path.
    Ensures pre-reserved transcript_sequence is honored, first-turn system-message
    inclusion remains correct, empty tool_summaries are stored, and in-thread
    rows do not ingest another agent's tool invocations.

Dependencies:
    - pytest: test framework
    - motet.core.conversations.transcript_storage: functions under test
    - motet.core.types.Message: canonical message model

Usage:
    pytest tests/unit/core/test_transcript_storage.py -q
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from motet.core.conversations import transcript_codec, transcript_storage
from motet.core.tools import transcript_service
from motet.core.types import Message


class FakeMemory:
    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}

    def store(self, **kwargs: Any) -> Dict[str, Any]:
        item_id = str(kwargs["item_id"])
        self._items[item_id] = {
            "id": item_id,
            "content": kwargs.get("content", ""),
            "type": kwargs.get("type"),
            "metadata": dict(kwargs.get("metadata") or {}),
            "conversation_id": (kwargs.get("metadata") or {}).get("conversation_id"),
        }
        return {"id": item_id}

    def store_memory(self, **kwargs: Any) -> Dict[str, Any]:
        return self.store(**kwargs)

    def recall_conversation(self, *, conversation_id: str, types: List[str], limit: int) -> List[Any]:
        rows = [
            SimpleNamespace(created_at=r["metadata"].get("timestamp", 0.0), metadata=r["metadata"])
            for r in self._items.values()
            if r.get("conversation_id") == conversation_id and r.get("type") in types
        ]
        rows.sort(key=lambda x: float(getattr(x, "created_at", 0.0)))
        return rows[-limit:]


class FakeRedis:
    def __init__(self) -> None:
        self._store: Dict[str, str] = {}

    def _norm(self, value: Any) -> str:
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def incr(self, key: str) -> int:
        current = int(self._store.get(key, "0"))
        current += 1
        self._store[key] = str(current)
        return current

    def expire(self, _key: str, _ttl: int) -> bool:
        return True

    def get(self, key: str) -> Any:
        value = self._store.get(key)
        if value is None:
            return None
        return value.encode("utf-8")

    def set(self, key: str, value: Any, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self._store:
            return False
        self._store[key] = self._norm(value)
        return True

    def delete(self, key: str) -> int:
        return 1 if self._store.pop(key, None) is not None else 0


def _fake_motet(memory: FakeMemory) -> Any:
    return SimpleNamespace(
        task_id="task-1",
        command_id="cmd-1",
        conversation_id="conv-1",
        distributed_context=SimpleNamespace(metadata={}),
        memory=memory,
        redis=None,
    )


def test_store_uses_explicit_transcript_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)

    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(transcript_codec, "serialize_transcript_items", lambda items: [{"role": i.role, "content": i.content} for i in items])
    sequence = 4242

    result = transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="hello")],
        assistant_response="world",
        agent_id="core.default",
        transcript_sequence=sequence,
    )

    assert result["canonical_transcript_stored"] is True
    assert result["sequence"] == sequence

    rows = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)
    assert len(rows) == 1
    md = rows[0].metadata
    assert md.get("status") == "completed"
    assert md.get("sequence") == sequence
    assert md.get("items")


def test_first_turn_with_explicit_sequence_keeps_system_message(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)

    captured_roles: List[str] = []

    def fake_build(
        turn_messages: List[Message],
        invs: List[Any],
        assistant_response: str,
        agent_id: str | None = None,
        pending_action: Dict[str, Any] | None = None,
    ) -> List[Message]:
        captured_roles[:] = [m.role for m in turn_messages]
        return [Message(role="assistant", content=assistant_response, agent_id=agent_id)]

    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(transcript_codec, "build_transcript_items_for_turn", fake_build)
    monkeypatch.setattr(transcript_codec, "serialize_transcript_items", lambda items: [{"role": i.role, "content": i.content} for i in items])

    transcript_storage.store_turn_transcript(
        motet,
        messages=[
            Message(role="system", content="sys"),
            Message(role="user", content="u1"),
        ],
        assistant_response="a1",
        agent_id="core.default",
        transcript_sequence=777,
    )

    # Sequence reservation metadata must not affect first-turn system inclusion.
    assert captured_roles == ["system", "user"]


def test_store_turn_transcript_duplicate_replay_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()

    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(
        transcript_codec,
        "serialize_transcript_items",
        lambda items: [{"role": i.role, "content": i.content} for i in items],
    )

    first = transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="hello")],
        assistant_response="world",
        transcript_sequence=33,
    )
    second = transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="hello")],
        assistant_response="world",
        transcript_sequence=33,
    )

    assert first["canonical_transcript_stored"] is True
    assert second["canonical_transcript_stored"] is True
    assert second.get("duplicate_replay") is True

    rows = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)
    assert len(rows) == 1


def test_store_turn_transcript_sequence_conflict_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()

    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(
        transcript_codec,
        "serialize_transcript_items",
        lambda items: [{"role": i.role, "content": i.content} for i in items],
    )

    transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="hello")],
        assistant_response="world",
        transcript_sequence=44,
    )
    conflict = transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="hello")],
        assistant_response="different",
        transcript_sequence=44,
    )

    assert conflict.get("sequence_conflict") is True
    assert "sequence conflict" in str(conflict.get("canonical_transcript_error", ""))


def test_store_turn_transcript_persists_thinking_text(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()

    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(
        transcript_codec,
        "serialize_transcript_items",
        lambda items: [{"role": i.role, "content": i.content} for i in items],
    )

    result = transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="hello")],
        assistant_response="world",
        agent_id="core.default",
        transcript_sequence=9,
        thinking_text="  I should greet the user.  ",
    )

    assert result["canonical_transcript_stored"] is True
    rows = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)
    md = rows[0].metadata
    assert md.get("thinking_text") == "I should greet the user."
    assert all(item.get("content") != "I should greet the user." for item in md.get("items") or [])


def test_store_turn_transcript_persists_tool_summaries(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()

    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(
        transcript_codec,
        "serialize_transcript_items",
        lambda items: [{"role": i.role, "content": i.content} for i in items],
    )

    result = transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="browse cnn.com")],
        assistant_response="Here are today's headlines.",
        agent_id="core.default",
        transcript_sequence=10,
        tool_summaries=[
            {
                "tool_name": "core.browse_page",
                "status": "success",
                "preview": "CNN homepage loaded.",
                "step": 1,
                "duration_ms": 2364,
                "arguments": {"url": "https://cnn.com"},
            }
        ],
    )

    assert result["canonical_transcript_stored"] is True
    rows = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)
    md = rows[0].metadata
    assert md.get("tool_summaries") == [
        {
            "tool_name": "core.browse_page",
            "status": "success",
            "preview": "CNN homepage loaded.",
            "step": 1,
            "duration_ms": 2364,
        },
    ]
    assert "https://cnn.com" not in str(md.get("items"))


def test_store_turn_transcript_persists_priced_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()
    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(
        transcript_codec,
        "serialize_transcript_items",
        lambda items: [{"role": i.role, "content": i.content} for i in items],
    )

    transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="hello")],
        assistant_response="world",
        agent_id="core.default",
        transcript_sequence=11,
        cost_usd=0.0124,
    )
    md = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)[0].metadata
    assert md.get("cost_usd") == pytest.approx(0.0124)


def test_store_turn_transcript_omits_unpriced_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()
    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(
        transcript_codec,
        "serialize_transcript_items",
        lambda items: [{"role": i.role, "content": i.content} for i in items],
    )

    transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="hello")],
        assistant_response="world",
        agent_id="core.default",
        transcript_sequence=12,
        cost_usd=0.0,
    )
    md = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)[0].metadata
    assert "cost_usd" not in md


def test_conversation_title_from_text_uses_first_line_snippet() -> None:
    assert transcript_storage.conversation_title_from_text("research pricing") == "research pricing"
    assert transcript_storage.conversation_title_from_text("  a\n\nb  ") == "a b"
    assert transcript_storage.conversation_title_from_text("") == "New Chat"
    long_title = "x" * 90
    titled = transcript_storage.conversation_title_from_text(long_title)
    assert titled.endswith("…")
    assert len(titled) <= 81


def test_store_turn_transcript_conversation_id_override(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()
    recalled: List[str] = []

    orig_recall = mem.recall_conversation

    def tracking_recall(*, conversation_id: str, types: List[str], limit: int) -> List[Any]:
        recalled.append(conversation_id)
        return orig_recall(conversation_id=conversation_id, types=types, limit=limit)

    mem.recall_conversation = tracking_recall  # type: ignore[method-assign]
    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(
        transcript_codec,
        "serialize_transcript_items",
        lambda items: [{"role": i.role, "content": i.content} for i in items],
    )

    result = transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="research pricing")],
        assistant_response="price is 12",
        agent_id="core.default",
        transcript_sequence=20,
        conversation_id="conv-1__spawn_1",
        include_tool_invocations=True,
    )

    assert result["canonical_transcript_stored"] is True
    assert "conv-1__spawn_1" in recalled
    parent_rows = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)
    child_rows = mem.recall_conversation(conversation_id="conv-1__spawn_1", types=["conversation_transcript"], limit=10)
    assert parent_rows == []
    assert child_rows[0].metadata.get("conversation_id") == "conv-1__spawn_1"
    items = child_rows[0].metadata.get("items") or []
    assert any(item.get("role") == "user" and item.get("content") == "research pricing" for item in items)
    assert any(item.get("content") == "price is 12" for item in items)


def test_store_turn_transcript_persists_spawn_children(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()
    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(
        transcript_codec,
        "serialize_transcript_items",
        lambda items: [{"role": i.role, "content": i.content} for i in items],
    )

    result = transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="fan out")],
        assistant_response="here is the synthesis",
        agent_id="core.default",
        transcript_sequence=21,
        spawn_children=[
            {
                "child_conversation_id": "conv-1__spawn_1",
                "agent_id": "core.default.spawn-1",
                "title": "research pricing",
                "preview": "price is 12",
                "cost_usd": 0.004,
                "thinking_text": "look it up",
                "tool_summaries": [
                    {"tool_name": "core.web_search", "status": "success", "preview": "list price"}
                ],
            }
        ],
    )

    assert result["canonical_transcript_stored"] is True
    md = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)[0].metadata
    assert md.get("spawn_children") == [
        {
            "child_conversation_id": "conv-1__spawn_1",
            "agent_id": "core.default.spawn-1",
            "title": "research pricing",
            "preview": "price is 12",
            "cost_usd": 0.004,
            "thinking_text": "look it up",
            "tool_summaries": [
                {"tool_name": "core.web_search", "status": "success", "preview": "list price"}
            ],
        }
    ]


def test_store_turn_transcript_ignores_spawn_children_on_motet_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()
    leaked = [
        {"child_conversation_id": "iso-should-not-persist", "title": "leaked"},
    ]
    motet.distributed_context.metadata = {"spawn_children": leaked}
    motet.metadata = motet.distributed_context.metadata
    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(
        transcript_codec,
        "serialize_transcript_items",
        lambda items: [{"role": i.role, "content": i.content} for i in items],
    )

    transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="fan out")],
        assistant_response="done",
        agent_id="core.default",
        transcript_sequence=22,
    )
    md = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)[0].metadata
    assert "spawn_children" not in md


def _tool_invocation_row(*, tool_name: str, tool_call_id: str, agent_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        metadata={
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "provider": "builtin",
            "status": "success",
            "task_id": "task-1",
            "conversation_id": "conv-1",
            "schema_version": "1.0",
            "agent_id": agent_id,
            "arguments_json": "{}",
            "started_at": "2026-08-31T01:09:18.000000Z",
        },
        tags=[tool_name, f"agent:{agent_id}", "conversation:conv-1", "stm"],
    )


def test_tool_invocations_for_agent_keeps_only_this_author() -> None:
    parent = _tool_invocation_row(
        tool_name="core.tools_search",
        tool_call_id="call-parent",
        agent_id="core.default",
    )
    child = _tool_invocation_row(
        tool_name="expert-panel.recall_discussion",
        tool_call_id="call-child",
        agent_id="expert-panel.synthesizer",
    )
    kept = transcript_storage._tool_invocations_for_agent([parent, child], "expert-panel.synthesizer")
    assert [row.metadata["tool_call_id"] for row in kept] == ["call-child"]
    unscoped = transcript_storage._tool_invocations_for_agent([parent, child], None)
    assert len(unscoped) == 2


def test_store_turn_transcript_persists_empty_tool_summaries(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()
    monkeypatch.setattr(transcript_service, "parse_and_dedupe_tool_invocation_memories", lambda *a, **k: [])
    monkeypatch.setattr(
        transcript_codec,
        "serialize_transcript_items",
        lambda items: [{"role": i.role, "content": i.content} for i in items],
    )

    result = transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="analyze fast food")],
        assistant_response="optimistic take",
        agent_id="expert-panel.optimist",
        transcript_sequence=30,
        tool_summaries=[],
    )
    assert result["canonical_transcript_stored"] is True
    md = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)[0].metadata
    assert md.get("tool_summaries") == []


def test_store_turn_transcript_items_exclude_other_agent_tools() -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()
    parent = _tool_invocation_row(
        tool_name="core.tools_search",
        tool_call_id="call-parent",
        agent_id="core.default",
    )
    mem.store(
        content="Executed tool 'core.tools_search': success",
        type="tool_invocation",
        item_id="tool_invocation:conv-1:call-parent",
        metadata=dict(parent.metadata),
    )
    mem._items["tool_invocation:conv-1:call-parent"]["tags"] = list(parent.tags)

    result = transcript_storage.store_turn_transcript(
        motet,
        messages=[Message(role="user", content="analyze fast food")],
        assistant_response="optimistic take",
        agent_id="expert-panel.optimist",
        root_turn=False,
        transcript_sequence=31,
        tool_summaries=[],
    )
    assert result["canonical_transcript_stored"] is True
    md = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)[0].metadata
    assert md.get("tool_summaries") == []
    items = transcript_codec.deserialize_transcript_items(md.get("items"))
    tool_names = [getattr(item, "tool_name", None) for item in items]
    assert "core.tools_search" not in tool_names
