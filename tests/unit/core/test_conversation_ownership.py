"""
Motet - Conversation Ownership Unit Tests (issue #139)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-17

Description:
    Unit tests for conversation ownership binding and cross-principal rejection.
    Verifies first-use bind, owner continuation, foreign-principal denial,
    registry lazy-migration, and ownership cleanup.

Usage:
    pytest tests/unit/core/test_conversation_ownership.py -q
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from motet.core.conversations import ownership, registry


class _FakeLock:
    def release_sync(self) -> None:
        return None


@pytest.fixture
def fake_ownership_store(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Dict[str, Any]]:
    """In-memory fake for ownership + registry structured data and locks."""
    store: Dict[str, Dict[str, Any]] = {}

    def fake_retrieve(client_id: str, key: str, format_type: str = "json_string") -> Optional[Dict[str, Any]]:
        assert format_type == "json_string"
        return store.get(f"{client_id}:{key}")

    def fake_store(client_id: str, key: str, value: Dict[str, Any], format_type: str = "json_string") -> None:
        assert format_type == "json_string"
        store[f"{client_id}:{key}"] = value

    def fake_acquire(client_id: str, lock_key: str, lock_value: Any = None, ttl_seconds: int = 90) -> _FakeLock:
        return _FakeLock()

    class _FakeRedis:
        def delete(self, *keys: str) -> int:
            # redis-py delete(*names). store_structured_data_sync fakes
            # keys as "{client_id}:{redis_key}".
            deleted = 0
            for key in keys:
                full = f"{ownership.OWNERSHIP_CLIENT_ID}:{key}"
                if full in store:
                    del store[full]
                    deleted += 1
            return deleted

    monkeypatch.setattr(ownership, "retrieve_structured_data_sync", fake_retrieve)
    monkeypatch.setattr(ownership, "store_structured_data_sync", fake_store)
    monkeypatch.setattr(ownership, "acquire_distributed_lock_sync", fake_acquire)
    monkeypatch.setattr(ownership, "get_sync_redis_client", lambda _cid: _FakeRedis())
    monkeypatch.setattr(registry, "retrieve_structured_data_sync", fake_retrieve)
    monkeypatch.setattr(registry, "store_structured_data_sync", fake_store)
    return store


def test_bind_on_first_use_and_owner_continues(
    fake_ownership_store: Dict[str, Dict[str, Any]],
) -> None:
    owner = ownership.authorize_conversation_access_sync(
        motet_id="default",
        tenant_id="acme",
        principal_id="service-account:victim",
        conversation_id="native-conv-1",
        bind_if_unclaimed=True,
    )
    assert owner == "service-account:victim"
    again = ownership.authorize_conversation_access_sync(
        motet_id="default",
        tenant_id="acme",
        principal_id="service-account:victim",
        conversation_id="native-conv-1",
        bind_if_unclaimed=True,
    )
    assert again == "service-account:victim"


def test_cross_principal_access_denied(
    fake_ownership_store: Dict[str, Dict[str, Any]],
) -> None:
    ownership.authorize_conversation_access_sync(
        motet_id="default",
        tenant_id="acme",
        principal_id="service-account:victim",
        conversation_id="native-conv-1",
        bind_if_unclaimed=True,
    )
    with pytest.raises(ownership.ConversationAccessDenied) as exc_info:
        ownership.authorize_conversation_access_sync(
            motet_id="default",
            tenant_id="acme",
            principal_id="service-account:attacker",
            conversation_id="native-conv-1",
            bind_if_unclaimed=True,
        )
    assert ownership.is_conversation_access_denied(exc_info.value)
    assert exc_info.value.owner_principal_id == "service-account:victim"


def test_cross_tenant_ids_are_independent(
    fake_ownership_store: Dict[str, Dict[str, Any]],
) -> None:
    """Same conversation_id in another tenant is a separate ownership record."""
    ownership.authorize_conversation_access_sync(
        motet_id="default",
        tenant_id="acme",
        principal_id="service-account:victim",
        conversation_id="shared-looking-id",
        bind_if_unclaimed=True,
    )
    owner = ownership.authorize_conversation_access_sync(
        motet_id="default",
        tenant_id="globex",
        principal_id="service-account:other",
        conversation_id="shared-looking-id",
        bind_if_unclaimed=True,
    )
    assert owner == "service-account:other"


def test_read_path_denies_unclaimed_without_registry(
    fake_ownership_store: Dict[str, Dict[str, Any]],
) -> None:
    with pytest.raises(ownership.ConversationAccessDenied):
        ownership.authorize_conversation_access_sync(
            motet_id="default",
            tenant_id="acme",
            principal_id="service-account:attacker",
            conversation_id="never-seen",
            bind_if_unclaimed=False,
        )


def test_read_path_lazy_binds_from_registry(
    fake_ownership_store: Dict[str, Dict[str, Any]],
) -> None:
    registry.register_or_touch_conversation_sync(
        motet_id="default",
        tenant_id="acme",
        principal_id="service-account:victim",
        conversation_id="legacy-conv",
        title="Legacy",
        agent_id="core.default",
    )
    owner = ownership.authorize_conversation_access_sync(
        motet_id="default",
        tenant_id="acme",
        principal_id="service-account:victim",
        conversation_id="legacy-conv",
        bind_if_unclaimed=False,
    )
    assert owner == "service-account:victim"
    assert (
        ownership.get_conversation_owner_sync("default", "acme", "legacy-conv")
        == "service-account:victim"
    )


def test_delete_ownership_allows_rebind(
    fake_ownership_store: Dict[str, Dict[str, Any]],
) -> None:
    ownership.authorize_conversation_access_sync(
        motet_id="default",
        tenant_id="acme",
        principal_id="service-account:victim",
        conversation_id="to-clear",
        bind_if_unclaimed=True,
    )
    assert ownership.delete_conversation_owner_sync("default", "acme", "to-clear") is True
    owner = ownership.authorize_conversation_access_sync(
        motet_id="default",
        tenant_id="acme",
        principal_id="service-account:new-owner",
        conversation_id="to-clear",
        bind_if_unclaimed=True,
    )
    assert owner == "service-account:new-owner"


def test_require_not_owned_by_other_allows_owner_and_unclaimed(
    fake_ownership_store: Dict[str, Dict[str, Any]],
) -> None:
    """API-boundary guard passes for the owner and for an unclaimed id."""
    assert (
        ownership.require_not_owned_by_other_sync(
            motet_id="default",
            tenant_id="acme",
            principal_id="service-account:victim",
            conversation_id="fresh-id",
        )
        is None
    )
    # Non-binding: probing must not create an ownership record.
    assert ownership.get_conversation_owner_sync("default", "acme", "fresh-id") is None

    ownership.authorize_conversation_access_sync(
        motet_id="default",
        tenant_id="acme",
        principal_id="service-account:victim",
        conversation_id="fresh-id",
        bind_if_unclaimed=True,
    )
    assert (
        ownership.require_not_owned_by_other_sync(
            motet_id="default",
            tenant_id="acme",
            principal_id="service-account:victim",
            conversation_id="fresh-id",
        )
        == "service-account:victim"
    )


def test_require_not_owned_by_other_denies_foreign_principal(
    fake_ownership_store: Dict[str, Dict[str, Any]],
) -> None:
    ownership.authorize_conversation_access_sync(
        motet_id="default",
        tenant_id="acme",
        principal_id="service-account:victim",
        conversation_id="native-conv-1",
        bind_if_unclaimed=True,
    )
    with pytest.raises(ownership.ConversationAccessDenied):
        ownership.require_not_owned_by_other_sync(
            motet_id="default",
            tenant_id="acme",
            principal_id="service-account:attacker",
            conversation_id="native-conv-1",
        )


def test_authorize_motet_conversation_access_noop_without_id() -> None:
    motet = MagicMock()
    motet.conversation_id = ""
    motet.motet_id = "default"
    motet.tenant_id = "acme"
    motet.principal_id = "p1"
    assert ownership.authorize_motet_conversation_access(motet) is None
