"""
Motet - Conversation Scope Authorization Tests (ADR-0083 / #55)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Unit tests for conversation list surface validation, agent visibility,
    and clear_conversation envelope unwrapping.

Dependencies:
    - fastapi.testclient
    - motet.interfaces.api.v1.conversations

Usage:
    pytest tests/unit/interfaces/api/test_conversations_scope_auth.py -q
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from motet.core.types import Principal
from motet.interfaces.api.shared.auth import get_current_principal
from motet.interfaces.api.v1.conversations import router


USER = Principal(id="user-1", roles=["user"], tenant_id="acme")
ADMIN = Principal(id="admin-1", roles=["admin"], tenant_id="acme")


class _FakeAgent:
    def __init__(self, allowed_roles: list[str]) -> None:
        self.allowed_roles = allowed_roles


class _FakeRegistry:
    def __init__(self, agents: Dict[str, _FakeAgent]) -> None:
        self._agents = agents

    def get(self, qualified_id: str) -> _FakeAgent | None:
        return self._agents.get(qualified_id)


def _client(principal: Principal) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: principal
    return TestClient(app)


def _ok_list_result() -> Dict[str, Any]:
    return {
        "status": "completed",
        "result": {
            "status": "completed",
            "data": {
                "conversations": [
                    {
                        "id": "conv-1",
                        "title": "Chat",
                        "created_at": 1.0,
                        "updated_at": 2.0,
                        "agent_id": "core.default",
                        "surface_id": "demo_chat",
                        "turn_agent_id": "core.subagent",
                        "parent_conversation_id": "conv-parent",
                    }
                ]
            },
        },
    }


@pytest.fixture
def agent_registry() -> _FakeRegistry:
    return _FakeRegistry(
        {
            "core.default": _FakeAgent(["*"]),
            "core.motet_admin": _FakeAgent(["admin", "motet-admin"]),
        }
    )


def test_invalid_surface_id_rejected(agent_registry: _FakeRegistry) -> None:
    with (
        patch(
            "motet.core.agents.resolve_agent_id",
            side_effect=lambda raw: raw or "core.default",
        ),
        patch("motet.core.agents.get_agent_registry", return_value=agent_registry),
    ):
        response = _client(USER).get(
            "/api/v1/conversations",
            params={"surface_id": "NOT VALID"},
        )

    assert response.status_code == 400, response.text


def test_unknown_surface_id_rejected(agent_registry: _FakeRegistry) -> None:
    fake_surfaces = MagicMock()
    fake_surfaces.exists.return_value = False
    with (
        patch(
            "motet.core.agents.resolve_agent_id",
            side_effect=lambda raw: raw or "core.default",
        ),
        patch("motet.core.agents.get_agent_registry", return_value=agent_registry),
        patch(
            "motet.core.surfaces.registry.SurfaceRegistry",
            return_value=fake_surfaces,
        ),
    ):
        response = _client(USER).get(
            "/api/v1/conversations",
            params={"surface_id": "not_a_real_surface"},
        )

    assert response.status_code == 400, response.text
    assert "not_a_real_surface" in response.json()["detail"]


def test_unauthorized_agent_filter_is_403(agent_registry: _FakeRegistry) -> None:
    with (
        patch(
            "motet.core.agents.resolve_agent_id",
            side_effect=lambda raw: raw or "core.default",
        ),
        patch("motet.core.agents.get_agent_registry", return_value=agent_registry),
    ):
        response = _client(USER).get(
            "/api/v1/conversations",
            params={"agent_id": "core.motet_admin"},
        )

    assert response.status_code == 403, response.text
    assert "core.motet_admin" in response.json()["detail"]


def test_admin_may_list_admin_agent(agent_registry: _FakeRegistry) -> None:
    fake_surfaces = MagicMock()
    fake_surfaces.exists.return_value = True
    with (
        patch(
            "motet.core.agents.resolve_agent_id",
            side_effect=lambda raw: raw or "core.default",
        ),
        patch("motet.core.agents.get_agent_registry", return_value=agent_registry),
        patch("motet.core.workers.global_invoker") as invoker,
    ):
        invoker.execute_command.return_value = _ok_list_result()
        response = _client(ADMIN).get(
            "/api/v1/conversations",
            params={"agent_id": "core.motet_admin"},
        )

    assert response.status_code == 200, response.text
    item = response.json()["conversations"][0]
    assert item["id"] == "conv-1"
    assert item["turn_agent_id"] == "core.subagent"
    assert item["parent_conversation_id"] == "conv-parent"


def test_clear_conversation_unwraps_invoker_envelope() -> None:
    """clear_conversation must read payload['data'], not the outer envelope."""
    with patch("motet.core.workers.global_invoker") as invoker:
        invoker.execute_command.return_value = {
            "status": "completed",
            "result": {
                "status": "completed",
                "data": {
                    "conversation_id": "conv-9",
                    "cleared": {"memory": 3, "vector": 1},
                },
            },
        }
        response = _client(USER).post("/api/v1/conversations/conv-9/clear")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["conversation_id"] == "conv-9"
    assert body["cleared"] == {"memory": 3, "vector": 1}
