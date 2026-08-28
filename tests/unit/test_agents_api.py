"""
Motet - Agents API Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for `/api/v1/agents` behavior. Verifies the API uses the
    distributed `core.agent_list` execution path and falls back to local
    visibility listing when distributed execution is unavailable.

Dependencies:
    - fastapi.testclient: In-process API testing
    - unittest.mock: Patching distributed invoker dependencies
    - motet.interfaces.http: FastAPI application under test

Usage:
    pytest tests/unit/test_agents_api.py -q

Notes:
    - Uses header-based principal authentication in test mode.
    - Focuses on API contract and fallback behavior, not worker internals.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.http import app

client = TestClient(app)

MOCK_PRINCIPAL_HEADERS = {
    "X-Principal-Id": "test-user",
    "X-Tenant-Id": "test-tenant",
    "X-Motet-Id": "test-motet",
}


@pytest.fixture(autouse=True)
def override_principal_dependency() -> Iterator[None]:
    """Bypass auth backend and inject a stable principal for tests."""
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        id="test-user",
        roles=["member"],
        tenant_id="test-tenant",
        motet_id="test-motet",
    )
    yield
    app.dependency_overrides.pop(get_current_principal, None)


def test_agents_list_uses_distributed_command() -> None:
    """Endpoint returns distributed `core.agent_list` payload."""

    def _execute_command(command: object) -> dict:
        data = getattr(command, "data", None)
        roles = list(getattr(data, "principal_roles", []) or [])
        assert "ops" in roles
        return {
            "status": "completed",
            "result": {
                "status": "success",
                "data": {
                    "agents": [
                        {
                            "qualified_id": "core.default",
                            "agent_id": "default",
                            "bundle_id": None,
                            "display_name": "Motet Agent",
                            "description": "Core default",
                            "allowed_roles": ["*"],
                            "aliases": ["agent"],
                            "tool_filter": {"mode": "discovery"},
                            "turn_hooks": {},
                        }
                    ],
                    "total": 1,
                },
            },
        }

    with patch("motet.core.workers.global_invoker.execute_command", side_effect=_execute_command):
        response = client.get(
            "/api/v1/agents",
            headers={**MOCK_PRINCIPAL_HEADERS, "X-Role": "ops"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["agents"][0]["qualified_id"] == "core.default"


def test_agents_list_fails_when_distributed_unavailable() -> None:
    """Endpoint fails fast when workers are unavailable (no fallback)."""
    with patch(
        "motet.core.workers.global_invoker.execute_command",
        side_effect=RuntimeError("worker unavailable"),
    ):
        response = client.get("/api/v1/agents", headers=MOCK_PRINCIPAL_HEADERS)

    assert response.status_code == 503

