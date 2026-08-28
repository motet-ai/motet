"""
Motet - Tenant / Motet Catalog Registry Unit Tests (ADR-0126)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Unit tests for TenantRegistry Redis catalog CRUD.

Usage:
    pytest tests/unit/core/test_tenant_registry.py -q
"""

from __future__ import annotations

from typing import Any, Dict, Set

import pytest

from motet.core.tenancy import tenant_registry as tr
from motet.core.tenancy.tenant_registry import (
    MotetConflictError,
    MotetNotFoundError,
    TenantConflictError,
    TenantNotFoundError,
    TenantRegistry,
    TenantValidationError,
)


class _FakeRedis:
    """Minimal Redis subset for TenantRegistry."""

    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.sets: Dict[str, Set[str]] = {}

    def exists(self, key: str) -> int:
        return 1 if key in self.hashes or key in self.sets else 0

    def hset(self, key: str, mapping: Dict[str, Any] | None = None, **kwargs: Any) -> int:
        data = dict(mapping or {})
        data.update(kwargs)
        bucket = self.hashes.setdefault(key, {})
        for k, v in data.items():
            bucket[str(k)] = "" if v is None else str(v)
        return len(data)

    def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def sadd(self, key: str, *members: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.update(str(m) for m in members)
        return len(bucket) - before

    def smembers(self, key: str) -> Set[str]:
        return set(self.sets.get(key, set()))

    def srem(self, key: str, *members: str) -> int:
        bucket = self.sets.get(key)
        if not bucket:
            return 0
        removed = 0
        for m in members:
            if str(m) in bucket:
                bucket.remove(str(m))
                removed += 1
        return removed

    def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            if key in self.hashes:
                del self.hashes[key]
                n += 1
            if key in self.sets:
                del self.sets[key]
                n += 1
        return n


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> TenantRegistry:
    fake = _FakeRedis()
    monkeypatch.setattr(tr, "get_sync_redis_client", lambda _cid: fake)
    return TenantRegistry()


def test_create_list_get_tenant_and_motet(registry: TenantRegistry) -> None:
    tenant = registry.create_tenant(tenant_id="Acme", name="Acme Corp")
    assert tenant.id == "acme"
    motet = registry.create_motet(
        tenant_id="acme",
        motet_id="Prod",
        name="Production",
    )
    assert motet.id == "prod"
    assert motet.tenant_id == "acme"

    listed = registry.list_tenants(include_motets=True)
    assert len(listed) == 1
    assert listed[0].id == "acme"
    assert listed[0].motets is not None
    assert [m.id for m in listed[0].motets] == ["prod"]

    fetched = registry.get_motet("acme", "prod")
    assert fetched.name == "Production"


def test_conflict_and_not_found(registry: TenantRegistry) -> None:
    registry.create_tenant(tenant_id="acme")
    with pytest.raises(TenantConflictError):
        registry.create_tenant(tenant_id="acme")
    with pytest.raises(TenantNotFoundError):
        registry.get_tenant("missing")
    with pytest.raises(MotetNotFoundError):
        registry.get_motet("acme", "prod")


def test_delete_tenant_requires_force_when_motets_exist(
    registry: TenantRegistry,
) -> None:
    registry.create_tenant(tenant_id="acme")
    registry.create_motet(tenant_id="acme", motet_id="prod")
    with pytest.raises(TenantValidationError):
        registry.delete_tenant("acme")
    registry.delete_tenant("acme", force=True)
    with pytest.raises(TenantNotFoundError):
        registry.get_tenant("acme")


def test_update_and_status_filter(registry: TenantRegistry) -> None:
    registry.create_tenant(tenant_id="acme", name="Acme")
    registry.create_motet(tenant_id="acme", motet_id="prod")
    registry.update_tenant("acme", status="disabled")
    registry.update_motet("acme", "prod", name="Prod US")

    assert registry.list_tenants(status="active") == []
    disabled = registry.list_tenants(status="disabled")
    assert len(disabled) == 1
    motets = registry.list_motets("acme")
    assert motets[0].name == "Prod US"


def test_invalid_id(registry: TenantRegistry) -> None:
    with pytest.raises(TenantValidationError):
        registry.create_tenant(tenant_id="Bad Id!")


def test_reserved_product_prefix_tenant_id_rejected(registry: TenantRegistry) -> None:
    """``motet`` would collide with ``motet:`` shared keys and ``~motet:*`` ACL."""
    with pytest.raises(TenantValidationError, match="Reserved"):
        registry.create_tenant(tenant_id="motet")
    with pytest.raises(TenantValidationError, match="Reserved"):
        registry.create_tenant(tenant_id="IMF")
    with pytest.raises(TenantValidationError, match="Reserved"):
        registry.create_tenant(tenant_id="celery")
    with pytest.raises(TenantValidationError, match="Reserved"):
        registry.create_tenant(tenant_id="mcp")
    registry.create_tenant(tenant_id="acme")
    motet_env = registry.create_motet(tenant_id="acme", motet_id="motet")
    assert motet_env.id == "motet"


def test_ensure_defaults_idempotent(registry: TenantRegistry) -> None:
    first = registry.ensure_defaults()
    second = registry.ensure_defaults()
    assert first["tenants"] >= 1
    assert first["motets"] >= 1
    assert second["tenants"] == 0
    assert second["motets"] == 0
    by_id = {t.id: t for t in registry.list_tenants()}
    assert {"motet-global", "default", "demo"} <= set(by_id)
    global_motets = {m.id for m in registry.list_motets("motet-global")}
    assert "default" in global_motets


def test_platform_tenant_is_labelled_as_a_tenant_not_a_scope(
    registry: TenantRegistry,
) -> None:
    """motet-global must not read as 'all tenants' in the scope selector."""
    registry.ensure_defaults()

    platform = registry.get_tenant("motet-global")
    assert platform.name == "Motet Platform"
    assert "global" not in platform.name.lower()
