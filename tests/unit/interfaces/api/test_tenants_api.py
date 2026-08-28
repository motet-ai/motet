"""
Motet - Tenants / Motets Catalog API Tests (ADR-0126)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-28

Description:
    Unit tests for /api/v1/tenants covering the authorization matrix (admin,
    ops_dashboard, tenant_scope=global, and plain tenant principals) and the
    registry-error-to-HTTP-status mapping for tenant and Motet CRUD.

Dependencies:
    - fastapi.testclient: exercises the router without a live Redis
    - motet.interfaces.api.v1.tenants: router under test
    - motet.core.tenancy.tenant_registry: patched to use an in-memory Redis fake

Usage:
    pytest tests/unit/interfaces/api/test_tenants_api.py -q

Notes:
    - The registry logic is exercised for real; only the Redis client is faked,
      so key layout and index cleanup stay covered end to end.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, Optional, Set

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from motet.core.tenancy import tenant_registry as tr
from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.api.v1.tenants import router


class _FakeRedis:
    """Minimal Redis subset backing TenantRegistry during tests."""

    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.sets: Dict[str, Set[str]] = {}

    def exists(self, key: str) -> int:
        return 1 if key in self.hashes or key in self.sets else 0

    def hset(
        self, key: str, mapping: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> int:
        data = dict(mapping or {})
        data.update(kwargs)
        bucket = self.hashes.setdefault(key, {})
        for field, value in data.items():
            bucket[str(field)] = "" if value is None else str(value)
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
        for member in members:
            if str(member) in bucket:
                bucket.remove(str(member))
                removed += 1
        return removed

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.hashes.pop(key, None) is not None:
                removed += 1
            if self.sets.pop(key, None) is not None:
                removed += 1
        return removed


ADMIN = Principal(id="admin-user", roles=["admin"], tenant_id="motet-global")
OPS_DASHBOARD = Principal(id="ops_dashboard", roles=[], tenant_id="default")
GLOBAL_SCOPE = Principal(
    id="svc-global",
    roles=["user"],
    tenant_id="motet-global",
    claims={"tenant_scope": "global"},
)
ACME_USER = Principal(id="acme-user", roles=["user"], tenant_id="acme")
NO_TENANT_USER = Principal(id="stray-user", roles=["user"])


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    """Back every TenantRegistry instance with one shared in-memory store."""
    fake = _FakeRedis()
    monkeypatch.setattr(tr, "get_sync_redis_client", lambda _client_id: fake)
    return fake


def _client(principal: Principal) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: principal
    return TestClient(app)


@pytest.fixture()
def admin_client() -> Iterator[TestClient]:
    yield _client(ADMIN)


@pytest.fixture()
def seeded(admin_client: TestClient) -> TestClient:
    """Catalog with acme/prod and globex/dev already created."""
    for tenant_id, name in (("acme", "Acme Corp"), ("globex", "Globex")):
        assert admin_client.post(
            "/api/v1/tenants", json={"id": tenant_id, "name": name}
        ).status_code == 201
    assert admin_client.post(
        "/api/v1/tenants/acme/motets", json={"id": "prod", "name": "Production"}
    ).status_code == 201
    assert admin_client.post(
        "/api/v1/tenants/globex/motets", json={"id": "dev", "name": "Development"}
    ).status_code == 201
    return admin_client


def test_admin_lists_all_tenants_with_nested_motets(seeded: TestClient) -> None:
    """include_motets=true nests environments for the scope selector."""
    response = seeded.get("/api/v1/tenants", params={"include_motets": "true"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["can_access_all_tenants"] is True
    assert [t["id"] for t in body["tenants"]] == ["acme", "globex"]
    assert [m["id"] for m in body["tenants"][0]["motets"]] == ["prod"]


def test_list_omits_motets_unless_requested(seeded: TestClient) -> None:
    """Default listing stays flat so scope-agnostic callers skip the extra reads."""
    body = seeded.get("/api/v1/tenants").json()

    assert body["tenants"]
    assert all(t["motets"] is None for t in body["tenants"])


def test_non_admin_sees_only_own_tenant(seeded: TestClient) -> None:
    """A plain principal's catalog view is filtered to its own tenant."""
    body = _client(ACME_USER).get("/api/v1/tenants").json()

    assert body["can_access_all_tenants"] is False
    assert [t["id"] for t in body["tenants"]] == ["acme"]


def test_principal_without_tenant_sees_empty_catalog(seeded: TestClient) -> None:
    """No tenant claim means nothing to scope to, not an error."""
    response = _client(NO_TENANT_USER).get("/api/v1/tenants")

    assert response.status_code == 200
    assert response.json() == {"tenants": [], "can_access_all_tenants": False}


@pytest.mark.parametrize("principal", [OPS_DASHBOARD, GLOBAL_SCOPE])
def test_ops_dashboard_and_global_scope_see_full_catalog(
    seeded: TestClient, principal: Principal
) -> None:
    """ops_dashboard and tenant_scope=global are admin-equivalent for reads."""
    body = _client(principal).get("/api/v1/tenants").json()

    assert body["can_access_all_tenants"] is True
    assert [t["id"] for t in body["tenants"]] == ["acme", "globex"]


def test_non_admin_cannot_read_other_tenant(seeded: TestClient) -> None:
    """Cross-tenant reads are refused for both tenants and their Motets."""
    client = _client(ACME_USER)

    assert client.get("/api/v1/tenants/globex").status_code == 403
    assert client.get("/api/v1/tenants/globex/motets").status_code == 403
    assert client.get("/api/v1/tenants/acme").status_code == 200


def test_non_admin_cannot_mutate_catalog(seeded: TestClient) -> None:
    """Every mutation path requires admin, including its own tenant."""
    client = _client(ACME_USER)

    assert client.post("/api/v1/tenants", json={"id": "evil"}).status_code == 403
    assert client.patch("/api/v1/tenants/acme", json={"name": "x"}).status_code == 403
    assert client.delete("/api/v1/tenants/acme").status_code == 403
    assert client.post("/api/v1/tenants/acme/motets", json={"id": "qa"}).status_code == 403
    assert client.post("/api/v1/tenants/ensure-defaults").status_code == 403


def test_duplicate_tenant_conflicts_and_invalid_id_rejected(
    seeded: TestClient,
) -> None:
    """Conflicts map to 409 and slug violations to 400."""
    assert seeded.post("/api/v1/tenants", json={"id": "acme"}).status_code == 409
    assert seeded.post("/api/v1/tenants", json={"id": "Bad Id!"}).status_code == 400
    assert (
        seeded.post("/api/v1/tenants", json={"id": "acme", "status": "paused"}).status_code
        == 400
    )


def test_missing_tenant_and_motet_return_404(seeded: TestClient) -> None:
    """Unknown ids are 404 rather than empty payloads."""
    assert seeded.get("/api/v1/tenants/nope").status_code == 404
    assert seeded.get("/api/v1/tenants/acme/motets/nope").status_code == 404
    assert (
        seeded.post("/api/v1/tenants/nope/motets", json={"id": "prod"}).status_code == 404
    )


def test_tenant_id_is_normalized_on_create(admin_client: TestClient) -> None:
    """Mixed-case ids are lowercased so scope values stay canonical."""
    created = admin_client.post("/api/v1/tenants", json={"id": "ACME"})

    assert created.status_code == 201
    assert created.json()["id"] == "acme"
    assert admin_client.get("/api/v1/tenants/acme").status_code == 200


def test_update_tenant_and_motet_status(seeded: TestClient) -> None:
    """PATCH changes display fields and status without touching created_at."""
    original = seeded.get("/api/v1/tenants/acme").json()

    updated = seeded.patch(
        "/api/v1/tenants/acme", json={"name": "Acme Corporation", "status": "disabled"}
    ).json()
    assert updated["name"] == "Acme Corporation"
    assert updated["status"] == "disabled"
    assert updated["created_at"] == original["created_at"]

    motet = seeded.patch(
        "/api/v1/tenants/acme/motets/prod", json={"status": "disabled"}
    ).json()
    assert motet["status"] == "disabled"

    active = seeded.get("/api/v1/tenants", params={"status": "active"}).json()
    assert [t["id"] for t in active["tenants"]] == ["globex"]


def test_delete_tenant_requires_force_when_motets_remain(seeded: TestClient) -> None:
    """Deleting a populated tenant is refused until force=true."""
    assert seeded.delete("/api/v1/tenants/acme").status_code == 400

    assert seeded.delete("/api/v1/tenants/acme", params={"force": "true"}).status_code == 204
    assert seeded.get("/api/v1/tenants/acme").status_code == 404
    assert seeded.get("/api/v1/tenants/acme/motets").status_code == 404


def test_delete_motet_then_tenant_without_force(seeded: TestClient) -> None:
    """Removing the last Motet clears the index so a plain delete succeeds."""
    assert seeded.delete("/api/v1/tenants/acme/motets/prod").status_code == 204
    assert seeded.get("/api/v1/tenants/acme/motets").json() == {"motets": []}
    assert seeded.delete("/api/v1/tenants/acme").status_code == 204


def test_ensure_defaults_is_idempotent(admin_client: TestClient) -> None:
    """Seeding twice creates nothing new but leaves the catalog populated."""
    first = admin_client.post("/api/v1/tenants/ensure-defaults").json()["created"]
    second = admin_client.post("/api/v1/tenants/ensure-defaults").json()["created"]

    assert first["tenants"] > 0 and first["motets"] > 0
    assert second == {"tenants": 0, "motets": 0}

    listed = admin_client.get(
        "/api/v1/tenants", params={"include_motets": "true"}
    ).json()["tenants"]
    by_id = {t["id"]: t for t in listed}
    assert {"motet-global", "default", "demo"} <= set(by_id)
    assert {m["id"] for m in by_id["default"]["motets"]} == {"default", "prod"}
