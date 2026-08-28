import asyncio

import pytest

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


class _FakeHTTPTransport:
    def __init__(self, config):
        self.config = config
        self.is_running = False
        self.start_server = bool(config.get("start_server", False))
        self._process = None

    async def start(self):
        self.is_running = True
        if self.start_server and self._process is None:
            self._process = await asyncio.create_subprocess_exec("sleep", "60")
        return True

    async def stop(self):
        self.is_running = False
        if self._process is not None:
            if self._process.returncode is None:
                self._process.terminate()
                await self._process.wait()
        return True


@pytest.mark.asyncio
async def test_http_singleton_attach_mode_disables_duplicate_start_server(monkeypatch):
    manager = MCPInstanceManager(config_dict={})
    manager.service_configs["svc"] = MCPInstanceConfig(
        service_id="svc",
        transport="http",
        command="npx",
        args=["@modelcontextprotocol/server-everything", "streamableHttp"],
        base_url="http://127.0.0.1:3301/mcp",
        port=3301,
        start_server=True,
        state_model=StateModel.STATELESS,
        credential_scope=CredentialScope.USER,
        visibility=Visibility.USER,
        lifecycle_duration=LifecycleDuration.CONVERSATION,
    )

    create_calls = []

    def _fake_create_transport(*, transport_type, service_id, config, worker_id, startup_command_context):
        create_calls.append(config)
        return _FakeHTTPTransport(config)

    monkeypatch.setattr(
        "motet.core.tools.mcp_motet.transports.MCPTransportFactory.create_transport",
        _fake_create_transport,
    )

    first = await manager.create_instance(
        service_id="svc",
        conversation_id="conv-a",
        tenant_id="tenant-1",
        principal_id="user-1",
    )
    second = await manager.create_instance(
        service_id="svc",
        conversation_id="conv-b",
        tenant_id="tenant-1",
        principal_id="user-1",
    )

    assert create_calls[0]["start_server"] is True
    assert create_calls[1]["start_server"] is False

    await manager.destroy_instance(second.instance_id, reason="test_cleanup")
    await manager.destroy_instance(first.instance_id, reason="test_cleanup")


@pytest.mark.asyncio
async def test_http_singleton_owner_transfers_process_on_destroy(monkeypatch):
    manager = MCPInstanceManager(config_dict={})
    manager.service_configs["svc"] = MCPInstanceConfig(
        service_id="svc",
        transport="http",
        command="npx",
        args=["@modelcontextprotocol/server-everything", "streamableHttp"],
        base_url="http://127.0.0.1:3301/mcp",
        port=3301,
        start_server=True,
        state_model=StateModel.STATELESS,
        credential_scope=CredentialScope.USER,
        visibility=Visibility.USER,
        lifecycle_duration=LifecycleDuration.CONVERSATION,
    )

    def _fake_create_transport(*, transport_type, service_id, config, worker_id, startup_command_context):
        return _FakeHTTPTransport(config)

    monkeypatch.setattr(
        "motet.core.tools.mcp_motet.transports.MCPTransportFactory.create_transport",
        _fake_create_transport,
    )

    owner = await manager.create_instance(
        service_id="svc",
        conversation_id="conv-a",
        tenant_id="tenant-1",
        principal_id="user-1",
    )
    attached = await manager.create_instance(
        service_id="svc",
        conversation_id="conv-b",
        tenant_id="tenant-1",
        principal_id="user-1",
    )

    owner_process = owner.process
    assert owner_process is not None
    assert attached.process is None

    await manager.destroy_instance(owner.instance_id, reason="test_destroy_owner")

    successor = manager.instances[attached.instance_id]
    assert successor.process is owner_process
    assert successor.owns_http_singleton_process is True
    assert manager._http_singleton_owner_by_service["svc"] == attached.instance_id
    assert owner_process.returncode is None

    await manager.destroy_instance(attached.instance_id, reason="test_cleanup")
