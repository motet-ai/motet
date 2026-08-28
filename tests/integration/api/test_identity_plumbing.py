"""
Integration tests covering identity plumbing from FastAPI down into the
distributed command context.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from motet.core.types import Principal
from motet.interfaces.http import create_app
from motet.interfaces.api.shared.auth import get_current_principal
from motet.core.commands.command_type_registry import (
    command_type_registry,
)
from motet.core.commands.command_data_registry import (
    command_data_registry,
)
import motet.core.workers as workers_module
import motet.core.workers.invoker_context as invoker_context
import motet.core.workers.celery_app as celery_app_module
import motet.core.security.oauth_token_refresher as oauth_token_refresher
import motet.core.workers.event_observer_manager as observer_manager
@pytest.fixture(autouse=True)
def disable_background_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop_async(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(workers_module, "start_event_observers", _noop_async)
    monkeypatch.setattr(workers_module, "stop_event_observers", _noop_async)
    monkeypatch.setattr(observer_manager, "start_event_observers", _noop_async)
    monkeypatch.setattr(observer_manager, "stop_event_observers", _noop_async)
    monkeypatch.setattr(oauth_token_refresher, "start_token_refresher", _noop_async)
    monkeypatch.setattr(oauth_token_refresher, "stop_token_refresher", _noop_async)


def _build_client(principal: Principal) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_principal] = lambda: principal
    return TestClient(app)


def test_run_command_uses_verified_principal_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """API should pass verified principal (tenant_id, principal_id) into command execution."""

    principal = Principal(
        id="user-123",
        tenant_id="tenant-xyz",
        motet_id="motet-prod",
        roles=["user"],
        claims={"source": "jwt"},
    )

    captured: Dict[str, Any] = {}

    class DummyCommand:
        pass

    def capturing_impl(*args: Any, **kwargs: Any) -> DummyCommand:
        captured["instantiate_kwargs"] = kwargs
        captured["invoker_called"] = True
        return DummyCommand()

    def fake_get(command_type: str, version: str | None = None) -> Any:
        return SimpleNamespace(
            implementation=capturing_impl,
            data_class=None,
            metadata=None,
        )

    class DummyInvoker:
        def execute_command(self, command: Any, target_worker_id: str | None = None, strategy_override: str | None = None) -> Dict[str, Any]:
            return {"ok": True}

    dummy_invoker = DummyInvoker()
    monkeypatch.setattr(command_type_registry, "get", fake_get)
    monkeypatch.setattr(workers_module, "global_invoker", dummy_invoker)

    with _build_client(principal) as client:
        resp = client.post(
            "/api/v1/commands/core.tool_list/execute",
            json={"data": {}, "timeout_seconds": 5},
        )

    assert resp.status_code == 200
    kwargs = captured["instantiate_kwargs"]
    assert kwargs["tenant_id"] == principal.tenant_id
    assert kwargs["principal_id"] == principal.id
    assert captured["invoker_called"] is True


def test_run_command_honors_service_account_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Service account principals should propagate the same way as JWT principals."""

    principal = Principal(
        id="service-account:ci-bot",
        tenant_id="tenant-ci",
        motet_id="motet-staging",
        roles=["automation"],
        claims={"type": "service_account", "name": "ci-bot"},
    )

    captured: Dict[str, Any] = {}

    class DummyCommand:
        pass

    def capturing_impl(*args: Any, **kwargs: Any) -> DummyCommand:
        captured["instantiate_kwargs"] = kwargs
        return DummyCommand()

    def fake_get(command_type: str, version: str | None = None) -> Any:
        return SimpleNamespace(
            implementation=capturing_impl,
            data_class=None,
            metadata=None,
        )

    class DummyInvoker:
        def execute_command(self, command: Any, target_worker_id: str | None = None, strategy_override: str | None = None) -> Dict[str, Any]:
            return {"ok": True}

    monkeypatch.setattr(command_type_registry, "get", fake_get)
    monkeypatch.setattr(workers_module, "global_invoker", DummyInvoker())

    with _build_client(principal) as client:
        resp = client.post(
            "/api/v1/commands/core.tool_list/execute",
            json={"data": {}, "timeout_seconds": 5},
        )

    assert resp.status_code == 200
    kwargs = captured["instantiate_kwargs"]
    assert kwargs["tenant_id"] == "tenant-ci"
    assert kwargs["principal_id"] == "service-account:ci-bot"


def test_hot_loaded_command_includes_identity_in_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unregistered command type returns 404; identity is still applied for registered execute path."""

    principal = Principal(
        id="user-hot",
        tenant_id="tenant-hot",
        motet_id="motet-hot",
        roles=["user"],
        claims={"source": "jwt"},
    )

    def fake_get(command_type: str, version: str | None = None) -> Any:
        return None  # Not in local registry

    monkeypatch.setattr(command_type_registry, "get", fake_get)

    with _build_client(principal) as client:
        resp = client.post(
            "/api/v1/commands/hot.command/execute",
            json={"data": {"payload": 42}, "timeout_seconds": 10},
        )

    # Command type not in registry or bundle catalog -> 404 (no Celery send in API tier)
    assert resp.status_code == 404


