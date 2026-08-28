"""
Motet - Conversation Registry Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-18

Description:
    Unit tests for ADR-0083 conversation registry scoping behavior. Verifies
    agent/surface strict filtering semantics and immutability of
    conversation scope metadata on touch operations.

Dependencies:
    - pytest: test framework
    - motet.core.conversations.registry: registry read/write + filter logic

Usage:
    pytest tests/unit/core/test_conversation_registry.py -q

Notes:
    - Uses monkeypatched in-memory sync Redis adapters so tests stay fast and deterministic.
    - Focuses on sync registry APIs used by distributed commands.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from motet.core.conversations import registry


@pytest.fixture
def fake_registry_store(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Dict[str, Any]]:
    """In-memory fake backing store for sync registry operations."""
    store: Dict[str, Dict[str, Any]] = {}

    def fake_retrieve(client_id: str, key: str, format_type: str = "json_string") -> Dict[str, Any] | None:
        assert client_id == registry.REGISTRY_CLIENT_ID
        assert format_type == "json_string"
        return store.get(key)

    def fake_store(client_id: str, key: str, value: Dict[str, Any], format_type: str = "json_string") -> None:
        assert client_id == registry.REGISTRY_CLIENT_ID
        assert format_type == "json_string"
        store[key] = value

    monkeypatch.setattr(registry, "retrieve_structured_data_sync", fake_retrieve)
    monkeypatch.setattr(registry, "store_structured_data_sync", fake_store)
    return store


def test_list_conversations_sync_filters_by_agent_and_surface(
    fake_registry_store: Dict[str, Dict[str, Any]],
) -> None:
    """ADR-0083: list can scope by agent and optional surface."""
    registry.register_or_touch_conversation_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        conversation_id="c-default-demo",
        title="default demo",
        agent_id="core.default",
        surface_id="demo_chat",
    )
    registry.register_or_touch_conversation_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        conversation_id="c-default-ops",
        title="default ops",
        agent_id="core.default",
        surface_id="ops_dashboard",
    )
    registry.register_or_touch_conversation_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        conversation_id="c-admin-ops",
        title="admin ops",
        agent_id="core.motet_admin",
        surface_id="ops_dashboard",
    )

    default_demo = registry.list_conversations_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        agent_id="core.default",
        surface_id="demo_chat",
    )
    assert [c["id"] for c in default_demo] == ["c-default-demo"]

    default_all_surfaces = registry.list_conversations_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        agent_id="core.default",
    )
    assert {c["id"] for c in default_all_surfaces} == {"c-default-demo", "c-default-ops"}


def test_list_conversations_sync_excludes_unscoped_legacy_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0083 strict mode: records missing scope fields are never returned in scoped lists."""
    key = registry._registry_key("f1", "t1", "p1")
    convs = [
        {
            "id": "legacy-no-scope",
            "title": "legacy",
            "created_at": 1.0,
            "updated_at": 1.0,
        },
        {
            "id": "default-demo",
            "title": "scoped",
            "created_at": 2.0,
            "updated_at": 2.0,
            "agent_id": "core.default",
            "surface_id": "demo_chat",
        },
    ]

    # Monkeypatch store/retrieve just for this test body.
    data = {"conversations": convs}

    def fake_retrieve(_: str, key_in: str, format_type: str = "json_string") -> Dict[str, Any] | None:
        assert format_type == "json_string"
        return data if key_in == key else None

    def fake_store(_: str, __: str, ___: Dict[str, Any], format_type: str = "json_string") -> None:
        assert format_type == "json_string"
        # Not used in this test.
        return None

    monkeypatch.setattr(registry, "retrieve_structured_data_sync", fake_retrieve)
    monkeypatch.setattr(registry, "store_structured_data_sync", fake_store)

    scoped = registry.list_conversations_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        agent_id="core.default",
        surface_id="demo_chat",
    )
    assert [c["id"] for c in scoped] == ["default-demo"]


def test_register_touch_does_not_mutate_scope_metadata(
    fake_registry_store: Dict[str, Dict[str, Any]],
) -> None:
    """ADR-0083: agent_id/surface_id are immutable on touch (only set on create)."""
    registry.register_or_touch_conversation_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        conversation_id="c1",
        title="first",
        agent_id="core.default",
        surface_id="demo_chat",
    )
    registry.register_or_touch_conversation_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        conversation_id="c1",
        title="renamed",
        agent_id="core.motet_admin",
        surface_id="ops_dashboard",
    )

    listed = registry.list_conversations_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        agent_id="core.default",
        surface_id="demo_chat",
    )
    assert len(listed) == 1
    assert listed[0]["id"] == "c1"
    assert listed[0]["title"] == "renamed"
    assert listed[0]["agent_id"] == "core.default"
    assert listed[0]["surface_id"] == "demo_chat"


def test_register_touch_backfills_missing_scope_on_touch(
    fake_registry_store: Dict[str, Dict[str, Any]],
) -> None:
    """ADR-0083: scope fields are backfilled on touch when the entry was created without them
    (handles race between conversation_rename and fire-and-forget conversation_register)."""
    # Simulate conversation_rename winning the race: creates entry without scope.
    registry.register_or_touch_conversation_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        conversation_id="c-race",
        title="auto-title",
    )

    # Verify entry exists but has no scope fields.
    key = registry._registry_key("f1", "t1", "p1")
    convs = fake_registry_store[key]["conversations"]
    assert len(convs) == 1
    assert "agent_id" not in convs[0]
    assert "surface_id" not in convs[0]

    # Simulate conversation_register arriving later with scope.
    registry.register_or_touch_conversation_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        conversation_id="c-race",
        agent_id="core.default",
        surface_id="demo_chat",
    )

    # Scope should now be backfilled.
    convs = fake_registry_store[key]["conversations"]
    assert convs[0]["agent_id"] == "core.default"
    assert convs[0]["surface_id"] == "demo_chat"

    # Conversation should now appear in scoped list.
    listed = registry.list_conversations_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        agent_id="core.default",
        surface_id="demo_chat",
    )
    assert len(listed) == 1
    assert listed[0]["id"] == "c-race"
    assert listed[0]["title"] == "auto-title"


def test_list_conversations_sync_reads_collapsed_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #218: registry reads collapsed ``{tenant}:conv:…`` keys only."""
    prefixed = "t1:conv:f1:p1"
    data = {
        "conversations": [
            {
                "id": "prefixed-conv",
                "title": "kept",
                "created_at": 1.0,
                "updated_at": 1.0,
                "agent_id": "core.default",
                "surface_id": "demo_chat",
            }
        ]
    }

    def fake_retrieve(_: str, key_in: str, format_type: str = "json_string") -> Dict[str, Any] | None:
        assert format_type == "json_string"
        return data if key_in == prefixed else None

    monkeypatch.setattr(registry, "retrieve_structured_data_sync", fake_retrieve)
    listed = registry.list_conversations_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        agent_id="core.default",
        surface_id="demo_chat",
    )
    assert [c["id"] for c in listed] == ["prefixed-conv"]


def test_list_conversations_sync_ignores_phase2_leftover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leftover Phase 2 keys are not dual-read."""
    phase2 = "t1:imf:conv:f1:t1:p1"
    data = {
        "conversations": [
            {
                "id": "legacy-conv",
                "title": "kept",
                "created_at": 1.0,
                "updated_at": 1.0,
                "agent_id": "core.default",
                "surface_id": "demo_chat",
            }
        ]
    }

    def fake_retrieve(_: str, key_in: str, format_type: str = "json_string") -> Dict[str, Any] | None:
        return data if key_in == phase2 else None

    monkeypatch.setattr(registry, "retrieve_structured_data_sync", fake_retrieve)
    listed = registry.list_conversations_sync(
        motet_id="f1",
        tenant_id="t1",
        principal_id="p1",
        agent_id="core.default",
        surface_id="demo_chat",
    )
    assert listed == []


def test_registry_key_rejects_empty_identity_fields() -> None:
    """ADR-0090: _registry_key must reject empty identity fields."""
    import pytest

    with pytest.raises(ValueError, match="non-empty"):
        registry._registry_key("f1", "", "p1")
    with pytest.raises(ValueError, match="non-empty"):
        registry._registry_key("f1", "t1", "")
    with pytest.raises(ValueError, match="non-empty"):
        registry._registry_key("", "t1", "p1")
    key = registry._registry_key("f1", "t1", "p1")
    assert key == "t1:conv:f1:p1"
