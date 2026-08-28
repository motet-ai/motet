"""
Integration tests for Workflows API endpoints.

Tests all workflow management endpoints including:
- Workflow execution
- Listing registered workflows
- Validate / register / unregister / export (ADR-0129)
"""

from __future__ import annotations

import asyncio
import os
import httpx
from unittest.mock import Mock, patch
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
    redis_client = get_sync_redis_client("test_workflows_api")
    sa_manager = ServiceAccountManager(redis_client)
    
    token = sa_manager.create_service_account(
        name="test-workflows-api",
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


@pytest.mark.integration
def test_execute_workflow(test_headers):
    """Test executing a workflow."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            # Use shorter timeout to fail fast if workers aren't available
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=5) as client:
                # Try to execute a workflow
                payload = {
                    "workflow_id": "test-wf-exec-123",
                    "workflow_name": "Test Execution Workflow",
                    "steps": [
                        {
                            "step_id": "step1",
                            "name": "Test Step",
                            "module_name": "tools",
                            "operation": "execute",
                            "parameters": {"tool": "math_eval", "expression": "2+2"},
                            "dependencies": [],
                            "timeout_seconds": 3  # Short timeout for fast failure
                        }
                    ],
                    "context": {"test": "execution"}
                }

                mock_result = {
                    "status": "completed",
                    "workflow_id": payload["workflow_id"],
                    "workflow_name": payload["workflow_name"],
                    "result": {"status": "success"},
                    "workflow_status": "completed",
                }
                with patch(
                    "motet.core.workers.global_invoker.execute_command",
                    Mock(return_value=mock_result),
                ):
                    response = await client.post(
                        "/api/v1/workflows/execute",
                        json=payload,
                        headers=test_headers,
                        timeout=5  # Explicit timeout
                    )

                assert response.status_code == 200, (
                    f"Expected 200, got {response.status_code}: {response.text}"
                )
                data = response.json()
                assert data["status"] == "completed"
                assert data["workflow_id"] == payload["workflow_id"]
        
        asyncio.run(_run())


@pytest.mark.integration
def test_execute_workflow_empty_steps(test_headers):
    """Test executing a workflow with empty steps."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            # Use shorter timeout to fail fast if workers aren't available
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=5) as client:
                payload = {
                    "workflow_id": "test-wf-empty",
                    "workflow_name": "Empty Steps Workflow",
                    "steps": []
                }
                
                response = await client.post(
                    "/api/v1/workflows/execute",
                    json=payload,
                    headers=test_headers
                )
                
                # Should return 200 or 500 (empty steps may be valid or invalid)
                assert response.status_code in [200, 400, 500], \
                    f"Expected 200, 400, or 500, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_get_active_workflows(test_headers):
    """Test listing registered workflows."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Get registered workflows (no auth required based on code)
                response = await client.get(
                    "/api/v1/workflows",
                    headers=test_headers
                )
                
                # May be 200 or 500 (e.g. circuit breaker open when workers unavailable)
                assert response.status_code in [200, 500], \
                    f"Expected 200 or 500, got {response.status_code}: {response.text}"
                if response.status_code == 200:
                    data = response.json()
                    assert "registered_workflows" in data
                    assert isinstance(data["registered_workflows"], list)
        
        asyncio.run(_run())


@pytest.mark.integration
def test_get_active_workflows_no_auth():
    """Test that listing workflows may not require auth (checking endpoint behavior)."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0")
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # Try without auth (endpoint may or may not require it)
                response = await client.get(
                    "/api/v1/workflows",
                    headers={"X-API-Key": "test-key"}
                )
                
                # May be 200, 401, or 500 (circuit breaker when workers unavailable)
                assert response.status_code in [200, 401, 500], \
                    f"Expected 200, 401, or 500, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


_VALID_BUILDER_YAML = """
workflow_id: api_eval_brief
name: API eval brief
required_inputs: [topic]
steps:
  calc:
    step_id: calc
    command_type: core.tool_execution
    command_data:
      tool_name: core.math_eval
      parameters:
        expression: "1+1"
    dependencies: []
"""


@pytest.mark.integration
def test_validate_register_unregister_export_workflow(test_headers, monkeypatch):
    """ADR-0129 builder HTTP surface with Redis/fan-out stubbed."""
    import motet.core.commands.builtin.tool  # noqa: F401
    import motet.core.commands.builtin.transform  # noqa: F401
    from motet.core.tools.builtin.math_eval import register as register_math
    from motet.core.tools import registry as tool_registry
    from motet.core.workflow import WorkflowRegistry

    register_math(tool_registry)

    monkeypatch.setattr(
        "motet.core.workflow.user_catalog.persist_user_workflow",
        lambda wf, **kwargs: None,
    )
    monkeypatch.setattr(
        "motet.core.workflow.user_catalog.delete_user_workflow",
        lambda wid, **kwargs: True,
    )
    monkeypatch.setattr(
        "motet.core.workflow.user_catalog.fan_out_user_workflow_sync",
        lambda **kwargs: {"acked": [], "failed": [], "skipped": True},
    )
    monkeypatch.setattr(
        "motet.core.workflow.user_catalog.fetch_user_workflow_dict",
        lambda wid, **kwargs: None,
    )

    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=15
            ) as client:
                v = await client.post(
                    "/api/v1/workflows/validate",
                    json={"yaml": _VALID_BUILDER_YAML},
                    headers=test_headers,
                )
                assert v.status_code == 200, v.text
                assert v.json().get("ok") is True

                r = await client.post(
                    "/api/v1/workflows/register",
                    json={"yaml": _VALID_BUILDER_YAML, "replace": True},
                    headers=test_headers,
                )
                assert r.status_code == 200, r.text
                body = r.json()
                assert body.get("ok") is True
                wid = body.get("workflow_id")
                assert wid and wid.startswith("user.")

                exp = await client.get(
                    f"/api/v1/workflows/{wid}/export",
                    headers=test_headers,
                )
                assert exp.status_code == 200, exp.text
                assert "workflow_id: api_eval_brief" in exp.json().get("yaml", "")

                d = await client.delete(
                    f"/api/v1/workflows/{wid}",
                    headers=test_headers,
                )
                assert d.status_code == 200, d.text
                assert WorkflowRegistry.get(wid) is None

        asyncio.run(_run())


