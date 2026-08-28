"""Parallel MCP service bootstrap at instance-manager startup (Docker / cold npx)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from motet.core.tools.mcp_motet.proxy.mcp_instance_manager import (
    MCPInstanceConfig,
    MCPInstanceManager,
)


@pytest.mark.asyncio
async def test_bootstrap_configured_services_runs_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = MCPInstanceManager(
        config_dict={"services": [], "redis_url": "redis://localhost:6379/0"},
    )
    for sid in ("svc_a", "svc_b", "svc_c"):
        mgr.service_configs[sid] = MCPInstanceConfig(service_id=sid, command="true", args=[])

    async def slow_create(self: MCPInstanceManager, service_id: str, cfg: MCPInstanceConfig) -> None:
        await asyncio.sleep(0.07)

    monkeypatch.setattr(
        MCPInstanceManager,
        "_create_initial_instances",
        slow_create,
    )
    monkeypatch.setattr(MCPInstanceManager, "_cleanup_failed_service", AsyncMock())

    monkeypatch.delenv("MOTET_MCP_STARTUP_MAX_PARALLEL", raising=False)

    t0 = time.monotonic()
    await mgr._bootstrap_configured_services(per_service_timeout=30)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.2, (
        f"expected ~parallel wall time (~0.07s), got {elapsed:.3f}s "
        "(serial would be ~0.21s for three sleeps)"
    )


@pytest.mark.asyncio
async def test_bootstrap_respects_motet_mcp_startup_max_parallel_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = MCPInstanceManager(
        config_dict={"services": [], "redis_url": "redis://localhost:6379/0"},
    )
    for sid in ("svc_a", "svc_b", "svc_c"):
        mgr.service_configs[sid] = MCPInstanceConfig(service_id=sid, command="true", args=[])

    async def slow_create(self: MCPInstanceManager, service_id: str, cfg: MCPInstanceConfig) -> None:
        await asyncio.sleep(0.05)

    monkeypatch.setattr(MCPInstanceManager, "_create_initial_instances", slow_create)
    monkeypatch.setattr(MCPInstanceManager, "_cleanup_failed_service", AsyncMock())
    monkeypatch.setenv("MOTET_MCP_STARTUP_MAX_PARALLEL", "1")

    t0 = time.monotonic()
    await mgr._bootstrap_configured_services(per_service_timeout=30)
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.12, f"expected serialized ~0.15s+, got {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_cleanup_failed_service_publishes_service_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = MCPInstanceManager(
        config_dict={"services": [], "redis_url": "redis://localhost:6379/0"},
    )
    inst = MagicMock()
    inst.service_id = "svc_a"
    inst.transport = None
    inst.process = MagicMock()
    mgr.instances["inst-1"] = inst

    publish = AsyncMock()
    monkeypatch.setattr(mgr, "_publish_per_service_signal", publish)

    await mgr._cleanup_failed_service("svc_a")

    publish.assert_awaited_once_with("svc_a", "service_removed")
