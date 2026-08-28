"""
Motet - Transcript Storage Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Verifies deterministic transcript sequence behavior and finalize storage path.
    Ensures pre-reserved transcript_sequence is honored and first-turn system-message
    inclusion logic remains correct.

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


def test_store_subagent_reply_is_non_root_text_only(monkeypatch: pytest.MonkeyPatch) -> None:
    mem = FakeMemory()
    motet = _fake_motet(mem)
    motet.redis = FakeRedis()
    recalled_types: List[str] = []

    orig_recall = mem.recall_conversation

    def tracking_recall(*, conversation_id: str, types: List[str], limit: int) -> List[Any]:
        recalled_types.extend(types)
        return orig_recall(conversation_id=conversation_id, types=types, limit=limit)

    mem.recall_conversation = tracking_recall  # type: ignore[method-assign]
    monkeypatch.setattr(
        transcript_codec,
        "serialize_transcript_items",
        lambda items: [{"role": i.role, "content": i.content, "agent_id": getattr(i, "agent_id", None)} for i in items],
    )

    result = transcript_storage.store_subagent_reply(
        motet,
        "price is 12",
        agent_id="core.default.spawn-1",
        root_agent_id="core.default",
    )

    assert result["canonical_transcript_stored"] is True
    assert "tool_invocation" not in recalled_types
    rows = mem.recall_conversation(conversation_id="conv-1", types=["conversation_transcript"], limit=10)
    md = rows[0].metadata
    assert md.get("root_turn") is False
    assert md.get("root_agent_id") == "core.default"
    assert md.get("parent_agent_id") == "core.default"
    assert md.get("agent_id") == "core.default.spawn-1"
    assert any(item.get("content") == "price is 12" for item in md.get("items") or [])
