"""
Motet - Agent Memory Tool Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-21

Description:
    Unit tests for the issue #217 agent memory facade: conversation-scoped
    store with long-term persist, query-based hybrid recall, tag
    refusal without ids or a conversation filter, and targeted forget.

Dependencies:
    - unittest.mock: MotetContext and MemoryManager stubs
    - motet.core.tools.builtin.memory_store / memory_recall / memory_tag

Usage:
    pytest tests/unit/core/tools/test_memory_tools.py
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from motet.core.commands.command_data_classes import (
    MemoryForgetData,
    MemoryRecallData,
    MemoryTagData,
)
from motet.core.commands.decorator import MotetContext
from motet.core.memory.inmemory import InMemoryStore
from motet.core.memory.manager import MemoryManager
from motet.core.tools.builtin import memory_forget, memory_recall, memory_store, memory_tag
from motet.core.tools.builtin import register_all_builtin_tools
from motet.core.tools.registry import ToolRegistry
from motet.core.types import MemoryItem, MemoryScopeType, serialize_memory_items


def _motet(
    *,
    conversation_id: Optional[str] = "conv-1",
    agent_id: Optional[str] = "core.default",
    store_result: Optional[Dict[str, Any]] = None,
    recall_result: Optional[List[Dict[str, Any]]] = None,
) -> SimpleNamespace:
    memory = MagicMock()
    memory.store.return_value = store_result or {
        "memory_id": "mem-1",
        "stored": True,
        "stored_in": ["memory", "vector_pending"],
    }
    memory.recall.return_value = recall_result or [
        {"id": "mem-1", "type": "note", "content": "kids like soccer"}
    ]
    memory.tag.return_value = {"updated": 1, "ids": ["mem-1"]}
    memory.forget.return_value = {"deleted": 1, "ids": ["mem-1"], "vector_deleted": 1}
    return SimpleNamespace(
        conversation_id=conversation_id,
        agent_id=agent_id,
        tenant_id="t1",
        motet_id="m1",
        principal_id="p1",
        memory=memory,
    )


def test_memory_store_stamps_conversation_and_persists_long_term() -> None:
    motet = _motet()
    with patch.object(memory_store, "_get_motet_context_optional", return_value=motet):
        result = memory_store.run(
            {"content": "Remember the kids like soccer", "tags": ["family"]}
        )

    assert result["status"] == "success"
    assert result["memory_id"] == "mem-1"
    assert result["persist"] is True
    assert result["conversation_id"] == "conv-1"
    kwargs = motet.memory.store.call_args.kwargs
    assert kwargs["content"] == "Remember the kids like soccer"
    assert kwargs["long_term"] is True
    assert kwargs["working"] is True
    assert kwargs["motet_context"] is motet
    assert kwargs["scope"] == MemoryScopeType.CONVERSATION


def test_memory_store_profile_type_uses_principal_scope() -> None:
    motet = _motet()
    with patch.object(memory_store, "_get_motet_context_optional", return_value=motet):
        memory_store.run({"content": "Prefers tabs", "type": "user_preference"})

    assert motet.memory.store.call_args.kwargs["scope"] == MemoryScopeType.PRINCIPAL


def test_memory_store_persist_false_skips_ltm() -> None:
    motet = _motet()
    with patch.object(memory_store, "_get_motet_context_optional", return_value=motet):
        result = memory_store.run({"content": "scratch", "persist": False})

    assert result["persist"] is False
    assert motet.memory.store.call_args.kwargs["long_term"] is False


def test_memory_store_requires_motet_context() -> None:
    with patch.object(memory_store, "_get_motet_context_optional", return_value=None):
        result = memory_store.run({"content": "no context"})
    assert "error" in result


def test_memory_recall_queries_hybrid_path() -> None:
    motet = _motet()
    with patch.object(memory_recall, "_get_motet_context_optional", return_value=motet):
        result = memory_recall.run({"query": "what about the kids?"})

    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["items"][0]["content"] == "kids like soccer"
    kwargs = motet.memory.recall.call_args.kwargs
    assert kwargs["query"] == "what about the kids?"
    assert kwargs["conversation_id"] is None


def test_memory_recall_passes_explicit_conversation() -> None:
    motet = _motet()
    with patch.object(memory_recall, "_get_motet_context_optional", return_value=motet):
        memory_recall.run({"query": "kids", "conversation_id": "conv-9"})

    assert motet.memory.recall.call_args.kwargs["conversation_id"] == "conv-9"


def test_memory_recall_requires_query() -> None:
    motet = _motet()
    with patch.object(memory_recall, "_get_motet_context_optional", return_value=motet):
        result = memory_recall.run({"query": "   "})
    assert result["error"] == "query is required and cannot be empty"
    motet.memory.recall.assert_not_called()


def test_memory_tag_refuses_unscoped_recent() -> None:
    motet = _motet()
    with patch.object(memory_tag, "_get_motet_context_optional", return_value=motet):
        result = memory_tag.run({"tags": ["important"]})

    assert "error" in result
    assert "memory_ids" in result["error"]
    motet.memory.tag.assert_not_called()


def test_memory_tag_requires_motet_context() -> None:
    with patch.object(memory_tag, "_get_motet_context_optional", return_value=None):
        result = memory_tag.run({"memory_ids": ["mem-1"], "tags": ["important"]})
    assert "error" in result


def test_memory_tag_with_ids_delegates_to_manager() -> None:
    motet = _motet()
    with patch.object(memory_tag, "_get_motet_context_optional", return_value=motet):
        result = memory_tag.run(
            {"memory_ids": ["mem-1"], "tags": ["important"], "op": "add"}
        )

    assert result["status"] == "success"
    assert result["updated"] == 1
    kwargs = motet.memory.tag.call_args.kwargs
    assert kwargs["memory_ids"] == ["mem-1"]
    assert kwargs["tags"] == ["important"]
    assert kwargs["op"] == "add"


def test_memory_recall_registered_and_find_by_tag_removed() -> None:
    registry = ToolRegistry()
    register_all_builtin_tools(registry, strict=True)
    items = registry.list_items()
    assert "core.memory_recall" in items
    assert "core.memory_store" in items
    assert "core.memory_forget" in items
    assert "core.memory_find_by_tag" not in items
    assert items["core.memory_recall"].expose_to_agents is True
    assert items["core.memory_forget"].expose_to_agents is True


def test_memory_helper_tag_forwards_conversation_id() -> None:
    manager = MagicMock()
    motet = MotetContext(task_id="task-1", worker_context={"memory_manager": manager})
    motet.do = MagicMock(return_value={"updated": 1, "ids": ["mem-1"]})

    motet.memory.tag(tags=["important"], op="add", conversation_id="conv-1")

    data = motet.do.call_args.kwargs.get("data") or motet.do.call_args[0][1]
    assert isinstance(data, MemoryTagData)
    assert data.conversation_id == "conv-1"
    motet.do.reset_mock()
    motet.memory.tag(tags=["important"], op="add", conversation_id="conv-1")
    alias_data = motet.do.call_args.kwargs.get("data") or motet.do.call_args[0][1]
    assert isinstance(alias_data, MemoryTagData)
    assert alias_data.conversation_id == "conv-1"


def test_memory_helper_recall_forwards_conversation_id() -> None:
    manager = MagicMock()
    motet = MotetContext(task_id="task-1", worker_context={"memory_manager": manager})
    motet.do = MagicMock(return_value={"items": [], "count": 0})

    motet.memory.recall(query="kids", conversation_id="conv-1")

    data = motet.do.call_args.kwargs.get("data") or motet.do.call_args[0][1]
    assert isinstance(data, MemoryRecallData)
    assert data.conversation_id == "conv-1"


def test_serialize_memory_items_accepts_models_and_dicts() -> None:
    item = MemoryItem(id="m1", type="note", content="hello")
    rows = serialize_memory_items([item, {"id": "m2", "content": "world"}])
    assert rows[0]["id"] == "m1"
    assert rows[0]["content"] == "hello"
    assert rows[1]["id"] == "m2"


def test_memory_tag_with_conversation_filter() -> None:
    motet = _motet()
    with patch.object(memory_tag, "_get_motet_context_optional", return_value=motet):
        result = memory_tag.run({"tags": ["important"], "conversation_id": "conv-1"})

    assert result["status"] == "success"
    kwargs = motet.memory.tag.call_args.kwargs
    assert kwargs["conversation_id"] == "conv-1"
    assert kwargs["memory_ids"] is None


def test_memory_forget_refuses_unscoped() -> None:
    motet = _motet()
    with patch.object(memory_forget, "_get_motet_context_optional", return_value=motet):
        result = memory_forget.run({})

    assert "error" in result
    assert "memory_ids" in result["error"]
    motet.memory.forget.assert_not_called()


def test_memory_forget_requires_motet_context() -> None:
    with patch.object(memory_forget, "_get_motet_context_optional", return_value=None):
        result = memory_forget.run({"memory_ids": ["mem-1"]})
    assert "error" in result


def test_memory_forget_with_ids_delegates_to_manager() -> None:
    motet = _motet()
    with patch.object(memory_forget, "_get_motet_context_optional", return_value=motet):
        result = memory_forget.run({"memory_ids": ["mem-1"]})

    assert result["status"] == "success"
    assert result["deleted"] == 1
    kwargs = motet.memory.forget.call_args.kwargs
    assert kwargs["memory_ids"] == ["mem-1"]
    assert kwargs["motet_context"] is motet


def test_memory_forget_with_conversation_filter() -> None:
    motet = _motet()
    with patch.object(memory_forget, "_get_motet_context_optional", return_value=motet):
        result = memory_forget.run({"conversation_id": "conv-1"})

    assert result["status"] == "success"
    kwargs = motet.memory.forget.call_args.kwargs
    assert kwargs["conversation_id"] == "conv-1"
    assert kwargs["memory_ids"] is None


def test_memory_helper_forget_forwards_conversation_id() -> None:
    manager = MagicMock()
    motet = MotetContext(task_id="task-1", worker_context={"memory_manager": manager})
    motet.do = MagicMock(return_value={"deleted": 1, "ids": ["mem-1"], "vector_deleted": 0})

    motet.memory.forget(conversation_id="conv-1")

    data = motet.do.call_args.kwargs.get("data") or motet.do.call_args[0][1]
    assert isinstance(data, MemoryForgetData)
    assert data.conversation_id == "conv-1"


def _identity() -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id="t1",
        motet_id="m1",
        principal_id="p1",
        conversation_id="conv-1",
        agent_id="core.default",
    )


def test_memory_manager_forget_deletes_kv_and_vector() -> None:
    kv = InMemoryStore()
    kv.upsert(MemoryItem(id="mem-1", type="note", content="kids like soccer"))
    vec = MagicMock()
    vec.delete_ids.return_value = 1
    manager = MemoryManager(SimpleNamespace(memory=kv, vector=vec))

    result = manager.forget(memory_ids=["mem-1"], motet_context=_identity())

    assert result["deleted"] == 1
    assert result["ids"] == ["mem-1"]
    assert result["vector_deleted"] == 1
    assert kv.get("mem-1") is None
    vec.delete_ids.assert_called_once()
    assert vec.delete_ids.call_args.args[0] == ["mem-1"]


def test_memory_manager_forget_intersects_conversation_and_tag() -> None:
    kv = InMemoryStore()
    kv.upsert(
        MemoryItem(
            id="both",
            type="note",
            content="x",
            tags=["conversation:conv-1", "family"],
        )
    )
    kv.upsert(
        MemoryItem(id="conv-only", type="note", content="y", tags=["conversation:conv-1"])
    )
    kv.upsert(MemoryItem(id="tag-only", type="note", content="z", tags=["family"]))
    manager = MemoryManager(SimpleNamespace(memory=kv, vector=None))

    result = manager.forget(
        conversation_id="conv-1",
        filter_tag="family",
        motet_context=_identity(),
    )

    assert result["ids"] == ["both"]
    assert kv.get("both") is None
    assert kv.get("conv-only") is not None
    assert kv.get("tag-only") is not None


def test_memory_manager_retag_intersects_conversation_and_tag() -> None:
    kv = InMemoryStore()
    kv.upsert(
        MemoryItem(
            id="both",
            type="note",
            content="x",
            tags=["conversation:conv-1", "family"],
        )
    )
    kv.upsert(
        MemoryItem(id="conv-only", type="note", content="y", tags=["conversation:conv-1"])
    )
    manager = MemoryManager(SimpleNamespace(memory=kv, vector=None))

    result = manager.retag(
        tags=["keep"],
        op="add",
        conversation_id="conv-1",
        filter_tag="family",
        motet_context=_identity(),
    )

    assert result["ids"] == ["both"]
    assert "keep" in (kv.get("both").tags or [])
    assert "keep" not in (kv.get("conv-only").tags or [])
