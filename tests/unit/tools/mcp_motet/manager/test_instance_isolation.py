"""
Motet - MCP instance isolation unit tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Create/destroy serialization, register/unregister, restart-budget isolation,
    and observer-only create-path regression (SCAN loop must not spawn).

Dependencies:
    - pytest / asyncio
    - MCPInstanceManager with mocked transports

Usage:
    pytest tests/unit/tools/mcp_motet/manager/test_instance_isolation.py
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from motet.core.tools.mcp_motet.manager.restart_budget import ServiceRestartBudget
from motet.core.tools.mcp_motet.manager.supervisor import SupervisorMixin
from motet.core.tools.mcp_motet.protocol import (
    CredentialScope,
    LifecycleDuration,
    StateModel,
    Visibility,
)
from motet.core.tools.mcp_motet.proxy.mcp_instance_manager import (
    MCPInstanceConfig,
    MCPInstanceManager,
)


class _FakeTransport:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.is_running = False
        self._process = None
        self.start_count = 0

    async def start(self) -> bool:
        self.start_count += 1
        await asyncio.sleep(0.05)
        self.is_running = True
        self._process = MagicMock()
        self._process.pid = 4242
        self._process.returncode = None
        return True

    async def stop(self) -> bool:
        self.is_running = False
        if self._process is not None:
            self._process.returncode = 0
        return True

    async def list_tools(self, timeout_seconds: int = 30) -> List[Any]:
        return []


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> MCPInstanceManager:
    mgr = MCPInstanceManager(config_dict={"services": []})
    mgr.service_configs["weather"] = MCPInstanceConfig(
        service_id="weather",
        command="true",
        state_model=StateModel.STATELESS,
        credential_scope=CredentialScope.MOTET,
        visibility=Visibility.MOTET,
        lifecycle_duration=LifecycleDuration.PERMANENT,
    )

    def _factory(**kwargs: Any) -> _FakeTransport:
        return _FakeTransport(kwargs.get("config") or {})

    monkeypatch.setattr(
        "motet.core.tools.mcp_motet.transports.MCPTransportFactory.create_transport",
        lambda **kwargs: _factory(**kwargs),
    )
    return mgr


@pytest.mark.asyncio
async def test_concurrent_creates_yield_one_process(manager: MCPInstanceManager) -> None:
    results = await asyncio.gather(
        manager.create_instance(
            "weather",
            motet_id="default",
            tenant_id="t1",
            reason="test",
            origin="test",
        ),
        manager.create_instance(
            "weather",
            motet_id="default",
            tenant_id="t1",
            reason="test",
            origin="test",
        ),
    )
    assert results[0].instance_id == results[1].instance_id
    assert len([i for i in manager.instances.values() if i.service_id == "weather"]) == 1
    starts = results[0].transport.start_count  # type: ignore[union-attr]
    assert starts == 1


@pytest.mark.asyncio
async def test_register_and_unregister_service(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = MCPInstanceManager(config_dict={"services": []})
    monkeypatch.setattr(mgr, "_create_initial_instances", AsyncMock())
    monkeypatch.setattr(mgr, "_publish_per_service_signal", AsyncMock())
    monkeypatch.setattr(mgr, "destroy_instance", AsyncMock())

    await mgr.register_server_config(
        "bundle.weather",
        {"command": "mcp-weather", "transport": "stdio"},
    )
    assert "bundle.weather" in mgr.service_configs
    mgr._create_initial_instances.assert_awaited_once()  # type: ignore[attr-defined]

    await mgr.unregister_server_config("bundle.weather")
    assert "bundle.weather" not in mgr.service_configs


@pytest.mark.asyncio
async def test_disabled_service_rejects_create(manager: MCPInstanceManager) -> None:
    manager._disabled_services.add("weather")
    with pytest.raises(RuntimeError, match="disabled"):
        await manager.create_instance("weather", motet_id="default", tenant_id="t1")


def test_restart_budget_marks_failed(manager: MCPInstanceManager) -> None:
    manager._restart_budget = ServiceRestartBudget(max_restarts=0, window_seconds=60)
    assert manager._restart_budget.is_exhausted("weather")
    assert manager._service_status_label("weather") == "failed"


@pytest.mark.asyncio
async def test_health_restart_exhausted_does_not_touch_other_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = MCPInstanceManager(config_dict={"services": []})
    mgr.running = True
    mgr._restart_budget = ServiceRestartBudget(max_restarts=0, window_seconds=60)
    mgr.service_configs["weather"] = MCPInstanceConfig(service_id="weather", command="true")
    mgr.service_configs["playwright"] = MCPInstanceConfig(
        service_id="playwright", command="true"
    )

    dead = MagicMock()
    dead.service_id = "playwright"
    dead.process = MagicMock(returncode=1)
    live = MagicMock()
    live.service_id = "weather"
    live.process = MagicMock(returncode=None)
    live.is_healthy = True
    mgr.instances["pw"] = dead
    mgr.instances["w"] = live

    create = AsyncMock()
    destroy = AsyncMock()
    monkeypatch.setattr(mgr, "create_instance", create)
    monkeypatch.setattr(mgr, "destroy_instance", destroy)
    monkeypatch.setattr(mgr, "_publish_per_service_signal", AsyncMock())
    monkeypatch.setattr(mgr, "_publish_one_service_status", AsyncMock())
    monkeypatch.setattr(mgr, "_publish_status_to_redis", AsyncMock())
    monkeypatch.setattr(mgr, "_publish_all_service_statuses", AsyncMock())

    async def _stop_loop(*_args: Any, **_kwargs: Any) -> None:
        mgr.running = False

    monkeypatch.setattr(asyncio, "sleep", _stop_loop)

    await mgr._health_monitor_loop()

    create.assert_not_called()
    destroy.assert_awaited_once_with("pw", reason="health_check_budget_exhausted")
    assert "w" in mgr.instances


@pytest.mark.asyncio
async def test_health_retries_failed_service_with_no_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = MCPInstanceManager(config_dict={"services": []})
    mgr.running = True
    mgr.service_configs["everything_http_test"] = MCPInstanceConfig(
        service_id="everything_http_test",
        command="true",
        transport="http",
        start_server=True,
        port=3301,
        restart_on_failure=True,
    )
    mgr._service_last_error["everything_http_test"] = (
        "Failed to start http transport for everything_http_test"
    )

    async def _ok_create(*_args: Any, **_kwargs: Any) -> None:
        mgr._service_last_error.pop("everything_http_test", None)

    create_initial = AsyncMock(side_effect=_ok_create)
    monkeypatch.setattr(mgr, "_create_initial_instances", create_initial)
    monkeypatch.setattr(mgr, "_publish_per_service_signal", AsyncMock())
    monkeypatch.setattr(mgr, "_publish_one_service_status", AsyncMock())
    monkeypatch.setattr(mgr, "_publish_status_to_redis", AsyncMock())
    monkeypatch.setattr(mgr, "_publish_all_service_statuses", AsyncMock())

    async def _stop_loop(*_args: Any, **_kwargs: Any) -> None:
        mgr.running = False

    monkeypatch.setattr(asyncio, "sleep", _stop_loop)

    await mgr._health_monitor_loop()

    create_initial.assert_awaited_once()
    assert create_initial.await_args.args[0] == "everything_http_test"
    assert mgr.stats["restarts"] == 1


@pytest.mark.asyncio
async def test_health_does_not_retry_not_started_without_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = MCPInstanceManager(config_dict={"services": []})
    mgr.running = True
    mgr.service_configs["playwright"] = MCPInstanceConfig(
        service_id="playwright",
        command="true",
        restart_on_failure=True,
    )

    create_initial = AsyncMock()
    monkeypatch.setattr(mgr, "_create_initial_instances", create_initial)
    monkeypatch.setattr(mgr, "_publish_per_service_signal", AsyncMock())
    monkeypatch.setattr(mgr, "_publish_one_service_status", AsyncMock())
    monkeypatch.setattr(mgr, "_publish_status_to_redis", AsyncMock())
    monkeypatch.setattr(mgr, "_publish_all_service_statuses", AsyncMock())

    async def _stop_loop(*_args: Any, **_kwargs: Any) -> None:
        mgr.running = False

    monkeypatch.setattr(asyncio, "sleep", _stop_loop)

    await mgr._health_monitor_loop()

    create_initial.assert_not_called()


def test_context_monitor_does_not_create_instances() -> None:
    source = inspect.getsource(SupervisorMixin._context_monitor_loop)
    assert "create_instance" not in source
    assert "skip_validation" not in source
