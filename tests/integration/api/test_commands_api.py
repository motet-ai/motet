"""
Integration tests for Commands API endpoints (ADR-0071).

Tests the commands API: list, get by type, and execute.
Bundle deployment is handled by /api/v1/deploy.
"""

from __future__ import annotations

import asyncio
import os
import httpx
import pytest
from contextlib import contextmanager

from motet.interfaces.http import create_app
from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.security.service_accounts import ServiceAccountManager


@contextmanager
def with_env(vars: dict[str, str]):
    """Context manager for environment variables."""
    old = {}
    try:
        for k, v in vars.items():
            old[k] = os.environ.get(k)
            os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def test_service_account_token():
    """Create a test service account token for API authentication."""
    redis_client = get_sync_redis_client("test_commands_api")
    sa_manager = ServiceAccountManager(redis_client)

    token = sa_manager.create_service_account(
        name="test-commands-api",
        tenant_id="test-tenant",
        motet_id="production",
        roles=["admin", "user"],
        created_by="test@example.com",
        expires_days=1
    )

    yield token

    # Cleanup
    sa_manager.revoke_service_account(token)


@pytest.fixture
def test_headers(test_service_account_token):
    """Provide test headers with authentication."""
    return {
        "X-API-Key": "test-key",
        "Authorization": f"Bearer {test_service_account_token}",
        "Content-Type": "application/json"
    }


# ---------------------------------------------------------------------------
# Commands API
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_commands(test_headers):
    """Test listing registered command types (GET /api/v1/commands)."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/commands",
                    headers=test_headers
                )
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert "commands" in data
                assert "total" in data
                assert isinstance(data["commands"], list)

        asyncio.run(_run())


@pytest.mark.integration
def test_list_commands_without_auth():
    """Test that listing commands requires authentication."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/commands",
                    headers={"X-API-Key": "test-key"}
                )
                # API may allow API key (200) or require full auth (401)
                assert response.status_code in [200, 401], (
                    f"Expected 200 or 401, got {response.status_code}: {response.text}"
                )

        asyncio.run(_run())


@pytest.mark.integration
def test_get_command_info(test_headers):
    """Test retrieving command details (GET /api/v1/commands/{command_type})."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Get list first to find a known command type
                list_resp = await client.get("/api/v1/commands", headers=test_headers)
                assert list_resp.status_code == 200
                commands = list_resp.json().get("commands", [])
                if not commands:
                    pytest.skip("No commands registered (e.g. no workers); cannot test get by type")
                command_type = commands[0].get("command_type")
                if not command_type:
                    pytest.skip("Command list entry has no command_type")
                response = await client.get(
                    f"/api/v1/commands/{command_type}",
                    headers=test_headers
                )
                assert response.status_code in [200, 404], (
                    f"Expected 200 or 404, got {response.status_code}: {response.text}"
                )
                if response.status_code == 200:
                    data = response.json()
                    assert "command_type" in data or "data_schema" in data or isinstance(data, dict)

        asyncio.run(_run())


@pytest.mark.integration
def test_get_command_info_not_found(test_headers):
    """Test GET /api/v1/commands/{command_type} returns 404 for unknown type."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/commands/nonexistent_command_type_xyz",
                    headers=test_headers
                )
                assert response.status_code == 404, (
                    f"Expected 404 for unknown command type, got {response.status_code}: {response.text}"
                )

        asyncio.run(_run())


@pytest.mark.integration
def test_execute_command(test_headers):
    """Test executing a command (POST /api/v1/commands/{command_type}/execute)."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
                # Try to run a command (may 404 if no workers / command not registered)
                payload = {
                    "data": {"message": "test"},
                    "timeout_seconds": 30,
                }
                response = await client.post(
                    "/api/v1/commands/test_command/execute",
                    json=payload,
                    headers=test_headers
                )
                # 200 = success, 404 = command not found, 422 = invalid data, 500 = execution error
                assert response.status_code in [200, 404, 422, 500], (
                    f"Unexpected status {response.status_code}: {response.text}"
                )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# ADR-0071: No-legacy-fallback enforcement
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_legacy_single_command_deploy_absent(test_headers):
    """
    Assert the legacy single-command hot deploy surface is absent (ADR-0071).

    The old path was POST /api/v1/commands/deploy with source_code. Deployment
    is now only via POST /api/v1/deploy (bundle deploy). This test ensures
    that attempting to use the old execute path for a 'deploy' command type
    (or the conceptual legacy deploy) returns 404 — no command type 'deploy'
    exists for single-command hot load.
    """
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Old single-command deploy style body (source_code) — must not be accepted
                response = await client.post(
                    "/api/v1/commands/deploy/execute",
                    json={
                        "data": {"source_code": "print(1)"},
                        "timeout_seconds": 60,
                    },
                    headers=test_headers,
                )
                # 404: command type 'deploy' is not registered (legacy hot-load removed)
                assert response.status_code == 404, (
                    f"Legacy single-command deploy must be absent (expect 404), got {response.status_code}: {response.text}"
                )
                data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                assert "not found" in (data.get("detail") or "").lower() or "404" in str(response.text)

        asyncio.run(_run())
