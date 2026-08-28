"""
Motet - Vault Ops Auth Tests (ADR-0066 / #68)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Description:
    Proves vault list/stats no longer fabricate an ops_dashboard admin
    principal for unauthenticated callers, and that non-admins cannot
    list credentials or name another tenant.

Usage:
    pytest tests/unit/interfaces/api/test_vault_ops_auth.py -q
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.api.v1.vault import router


ADMIN = Principal(id="admin-user", roles=["admin"], tenant_id="acme")
USER = Principal(id="user-1", roles=["user"], tenant_id="acme")


def _app(principal: Principal | None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if principal is not None:
        app.dependency_overrides[get_current_principal] = lambda: principal
    return TestClient(app)


def test_unauthenticated_list_is_401() -> None:
    response = _app(None).get("/api/v1/vault/credentials")
    assert response.status_code == 401


def test_unauthenticated_stats_is_401() -> None:
    response = _app(None).get("/api/v1/vault/stats")
    assert response.status_code == 401


def test_non_admin_list_is_403() -> None:
    response = _app(USER).get("/api/v1/vault/credentials")
    assert response.status_code == 403


def test_non_admin_stats_is_403() -> None:
    response = _app(USER).get("/api/v1/vault/stats")
    assert response.status_code == 403


def test_admin_list_succeeds() -> None:
    fake = MagicMock()
    fake.list_credentials.return_value = []
    with patch("motet.interfaces.api.v1.vault.get_vault_service", return_value=fake):
        response = _app(ADMIN).get("/api/v1/vault/credentials")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success"
