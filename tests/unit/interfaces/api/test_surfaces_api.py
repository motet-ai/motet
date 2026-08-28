"""
Motet - Surfaces Catalog API Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-08

Description:
    Unit tests for /api/v1/surfaces and agent surface allow-list overlays.

Usage:
    pytest tests/unit/interfaces/api/test_surfaces_api.py -q
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, Optional, Set

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from motet.core.surfaces import registry as sr
from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.api.v1.agents import router as agents_router
from motet.interfaces.api.v1.surfaces import router as surfaces_router


class _FakeRedis:
    """Minimal Redis subset for SurfaceRegistry tests."""

    def __init__(self) -> None:
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.sets: Dict[str, Set[str]] = {}
        self.kv: Dict[str, str] = {}

    def exists(self, key: str) -> int:
        return 1 if key in self.hashes or key in self.sets or key in self.kv else 0

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
            if self.kv.pop(key, None) is not None:
                removed += 1
        return removed

    def get(self, key: str) -> Optional[str]:
        return self.kv.get(key)

    def set(self, key: str, value: str) -> bool:
        self.kv[key] = str(value)
        return True


ADMIN = Principal(id="admin-user", roles=["admin"], tenant_id="motet-global")
USER = Principal(id="user", roles=["user"], tenant_id="acme")


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(sr, "get_sync_redis_client", lambda _client_id: fake)
    return fake


def _client(principal: Principal) -> TestClient:
    app = FastAPI()
    app.include_router(surfaces_router)
    app.include_router(agents_router)
    app.dependency_overrides[get_current_principal] = lambda: principal
    return TestClient(app)


@pytest.fixture()
def admin_client() -> Iterator[TestClient]:
    yield _client(ADMIN)


def test_list_seeds_builtins(admin_client: TestClient) -> None:
    response = admin_client.get("/api/v1/surfaces")
    assert response.status_code == 200, response.text
    body = response.json()
    ids = {s["id"] for s in body["surfaces"]}
    assert {"demo_chat", "openai_compat", "ops_dashboard", "cli"} <= ids
    assert body["total"] >= 4
    assert body["can_manage"] is True


def test_create_and_delete_custom_surface(admin_client: TestClient) -> None:
    create = admin_client.post(
        "/api/v1/surfaces",
        json={"id": "partner_portal", "display_name": "Partner Portal"},
    )
    assert create.status_code == 201, create.text
    assert create.json()["id"] == "partner_portal"
    assert create.json()["builtin"] is False

    listed = admin_client.get("/api/v1/surfaces")
    assert "partner_portal" in {s["id"] for s in listed.json()["surfaces"]}

    deleted = admin_client.delete("/api/v1/surfaces/partner_portal")
    assert deleted.status_code == 204


def test_cannot_delete_builtin(admin_client: TestClient) -> None:
    admin_client.get("/api/v1/surfaces")
    response = admin_client.delete("/api/v1/surfaces/demo_chat")
    assert response.status_code == 400


def test_non_admin_cannot_create() -> None:
    client = _client(USER)
    response = client.post("/api/v1/surfaces", json={"id": "x_surface"})
    assert response.status_code == 403


def test_agent_surface_allowlist_overlay(admin_client: TestClient) -> None:
    admin_client.get("/api/v1/surfaces")
    updated = admin_client.put(
        "/api/v1/agents/core.default/surfaces",
        json={"allowed_surface_ids": ["demo_chat", "cli"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["allowed_surface_ids"] == ["demo_chat", "cli"]

    cleared = admin_client.put(
        "/api/v1/agents/core.default/surfaces",
        json={"clear": True},
    )
    assert cleared.status_code == 200
    assert cleared.json()["allowed_surface_ids"] is None


def test_invalid_surface_id_rejected(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/api/v1/surfaces",
        json={"id": "bad.id"},
    )
    assert response.status_code == 400


def test_kebab_case_surface_id_accepted(admin_client: TestClient) -> None:
    """Product surfaces may use hyphens (e.g. MEMO memo-intake)."""
    create = admin_client.post(
        "/api/v1/surfaces",
        json={
            "id": "memo-intake",
            "display_name": "MEMO Intake",
            "description": "MEMO upload/intake Archivist chat",
        },
    )
    assert create.status_code == 201, create.text
    assert create.json()["id"] == "memo-intake"
    listed = admin_client.get("/api/v1/surfaces")
    assert "memo-intake" in {s["id"] for s in listed.json()["surfaces"]}
    deleted = admin_client.delete("/api/v1/surfaces/memo-intake")
    assert deleted.status_code == 204
