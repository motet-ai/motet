"""
Motet - conversation_clear Cascade Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Unit tests for conversation_clear: isolated descendants are cleared
    with the parent; a child clear does not delete the parent; a descendant
    the caller cannot access is skipped; descendants from the durable
    registry parent pointers cascade even when the lineage index is empty
    (expired TTL), and ids present in both sources are cleared once.

Dependencies:
    - pytest
    - motet.core.commands.builtin.conversation
    - motet.core.commands.command_data_classes.ClearConversationData

Usage:
    pytest tests/unit/core/test_conversation_clear.py -q
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Set
from unittest.mock import patch

import pytest

from motet.core.commands.builtin.conversation import conversation_clear
from motet.core.commands.command_data_classes import ClearConversationData
from motet.core.conversations.ownership import ConversationAccessDenied


class _FakeStore:
    def __init__(self) -> None:
        self.cleared: List[str] = []

    def clear_by_tag(self, tag: str) -> int:
        self.cleared.append(tag)
        return 1

    def delete_by_tag(self, tag: str) -> int:
        self.cleared.append(f"vector:{tag}")
        return 1


@pytest.fixture(autouse=True)
def _allow_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "motet.core.commands.builtin.conversation.authorize_conversation_access_sync",
        lambda **kwargs: kwargs["principal_id"],
    )


def _run_clear(
    *,
    conversation_id: str,
    descendants: List[str],
    registry_descendants: List[str] | None = None,
    denied: Set[str] | None = None,
) -> tuple[Dict[str, Any], List[str], List[str]]:
    removed: List[str] = []
    forgotten: List[str] = []
    store = _FakeStore()
    fake_motet = SimpleNamespace(
        motet_id="default",
        tenant_id="tenant-1",
        principal_id="principal-1",
        conversation_id=conversation_id,
        stack=SimpleNamespace(memory=store, vector=store),
    )
    denied_ids = denied or set()

    def _authorize(**kwargs: Any) -> str:
        cid = kwargs["conversation_id"]
        if cid in denied_ids:
            raise ConversationAccessDenied("denied", conversation_id=cid, principal_id="principal-1")
        return "principal-1"

    with (
        patch(
            "motet.core.commands.builtin.conversation.get_motet_context",
            return_value=fake_motet,
        ),
        patch(
            "motet.core.commands.builtin.conversation.authorize_conversation_access_sync",
            side_effect=_authorize,
        ),
        patch(
            "motet.core.commands.builtin.conversation.list_descendant_conversations_sync",
            return_value=list(descendants),
        ),
        patch(
            "motet.core.commands.builtin.conversation.list_descendant_conversations_from_registry_sync",
            return_value=list(registry_descendants or []),
        ),
        patch(
            "motet.core.commands.builtin.conversation.remove_conversation_sync",
            side_effect=lambda **kwargs: removed.append(kwargs["conversation_id"]),
        ),
        patch(
            "motet.core.commands.builtin.conversation.delete_conversation_owner_sync",
            return_value=True,
        ),
        patch(
            "motet.core.commands.builtin.conversation.forget_conversation_lineage_sync",
            side_effect=lambda **kwargs: forgotten.append(kwargs["conversation_id"]),
        ),
    ):
        out = conversation_clear.__wrapped__(
            ClearConversationData(conversation_id=conversation_id)
        )
    return out, removed, forgotten


def test_conversation_clear_cascades_isolated_descendants() -> None:
    out, removed, forgotten = _run_clear(
        conversation_id="parent",
        descendants=["iso-a", "iso-b"],
    )
    assert out["conversation_id"] == "parent"
    assert out["child_conversation_ids"] == ["iso-a", "iso-b"]
    assert out["cleared"] == {"memory": 3, "vector": 3}
    assert removed == ["iso-a", "iso-b", "parent"]
    assert forgotten == ["iso-a", "iso-b", "parent"]


def test_conversation_clear_child_does_not_delete_parent() -> None:
    out, removed, forgotten = _run_clear(
        conversation_id="iso-a",
        descendants=[],
    )
    assert out["child_conversation_ids"] == []
    assert removed == ["iso-a"]
    assert forgotten == ["iso-a"]
    assert "parent" not in removed


def test_conversation_clear_cascades_registry_descendants_after_lineage_ttl() -> None:
    """Registry parent pointers still cascade when the lineage index expired."""
    out, removed, forgotten = _run_clear(
        conversation_id="parent",
        descendants=[],
        registry_descendants=["iso-old"],
    )
    assert out["child_conversation_ids"] == ["iso-old"]
    assert removed == ["iso-old", "parent"]
    assert forgotten == ["iso-old", "parent"]


def test_conversation_clear_merges_lineage_and_registry_descendants_once() -> None:
    """An id present in both sources is cleared once; the union is sorted."""
    out, removed, _forgotten = _run_clear(
        conversation_id="parent",
        descendants=["iso-a", "iso-b"],
        registry_descendants=["iso-b", "iso-c"],
    )
    assert out["child_conversation_ids"] == ["iso-a", "iso-b", "iso-c"]
    assert removed == ["iso-a", "iso-b", "iso-c", "parent"]


def test_conversation_clear_skips_inaccessible_descendant() -> None:
    out, removed, _forgotten = _run_clear(
        conversation_id="parent",
        descendants=["iso-ok", "iso-other"],
        denied={"iso-other"},
    )
    assert out["child_conversation_ids"] == ["iso-ok"]
    assert removed == ["iso-ok", "parent"]
    assert "iso-other" not in removed
