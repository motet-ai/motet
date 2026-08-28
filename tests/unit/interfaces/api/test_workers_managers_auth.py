"""
Motet - Workers Managers Status Auth Tests (ADR-0066 / #68)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-14

Description:
    GET /api/v1/workers/managers/status requires authentication. Any
    authenticated role may read; unauthenticated callers are 401.

Usage:
    pytest tests/unit/interfaces/api/test_workers_managers_auth.py -q
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.api.v1.workers import router


USER = Principal(id="user-1", roles=["user"], tenant_id="acme")


def test_unauthenticated_managers_status_is_401() -> None:
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/v1/workers/managers/status")
    assert response.status_code == 401


def test_authenticated_user_may_read_managers_status() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: USER

    registry = MagicMock()
    registry.get_all_statuses.return_value = []
    readiness = MagicMock()
    readiness.get_all_workers.return_value = {}
    with (
        patch(
            "motet.core.distributed.manager_status.ManagerStatusRegistry",
            return_value=registry,
        ),
        patch(
            "motet.core.distributed.worker_readiness.WorkerReadinessService",
            return_value=readiness,
        ),
    ):
        response = TestClient(app).get("/api/v1/workers/managers/status")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success"
