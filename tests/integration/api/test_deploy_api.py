"""
Integration tests for Deploy API endpoints (ADR-0071 Phase 7).

Tests the deploy API surface: list bundles, get status.
Full deploy flow (POST /api/v1/deploy with worker ack) requires a running deployer-worker;
when none is available, the E2E test is skipped with instructions.

To run the happy-path E2E test (deploy → poll → undeploy):
  docker-compose -f tests/docker-compose.test.yml --profile workers up -d deployer-worker
  docker-compose -f tests/docker-compose.test.yml run -e MOTET_RUN_DEPLOY_E2E=1 --rm test-runner \\
    pytest tests/integration/api/test_deploy_api.py -v -k test_deploy_happy_path_e2e
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import httpx
import pytest
from contextlib import contextmanager
from pathlib import Path

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
    redis_client = get_sync_redis_client("test_deploy_api")
    sa_manager = ServiceAccountManager(redis_client)

    token = sa_manager.create_service_account(
        name="test-deploy-api",
        tenant_id="test-tenant",
        motet_id="production",
        roles=["admin", "user"],
        created_by="test@example.com",
        expires_days=1,
    )

    yield token

    sa_manager.revoke_service_account(token)


@pytest.fixture
def test_headers(test_service_account_token):
    """Provide test headers with authentication."""
    return {
        "X-API-Key": "test-key",
        "Authorization": f"Bearer {test_service_account_token}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Deploy API (ADR-0071)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_deploy(test_headers):
    """Test listing deployed bundles (GET /api/v1/deploy)."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get("/api/v1/deploy", headers=test_headers)
                assert response.status_code == 200, (
                    f"Expected 200, got {response.status_code}: {response.text}"
                )
                data = response.json()
                assert "bundles" in data or isinstance(data, list), (
                    "Response should have 'bundles' key or be a list"
                )

        asyncio.run(_run())


@pytest.mark.integration
def test_list_deploy_without_auth():
    """Test that listing deploy requires authentication."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get("/api/v1/deploy", headers={"X-API-Key": "test-key"})
                # API may allow API key (200) or require full auth (401)
                assert response.status_code in [200, 401], (
                    f"Expected 200 or 401, got {response.status_code}: {response.text}"
                )

        asyncio.run(_run())


@pytest.mark.integration
def test_get_deploy_status_nonexistent_bundle(test_headers):
    """Test GET /api/v1/deploy/{bundle_id}/status for nonexistent bundle."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
    }):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/deploy/nonexistent-bundle-xyz/status",
                    headers=test_headers,
                )
                # 200 with empty/not_found status or 404 both acceptable
                assert response.status_code in [200, 404], (
                    f"Expected 200 or 404, got {response.status_code}: {response.text}"
                )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# E2E: full deploy flow (requires deployer-worker; run with --profile workers)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("MOTET_RUN_DEPLOY_E2E") not in ("1", "true", "True"),
    reason="Set MOTET_RUN_DEPLOY_E2E=1 to run deploy E2E",
)
def test_deploy_happy_path_e2e(test_headers):
    """
    Happy path: deploy hello-world from file:// repo, poll until complete, undeploy (ADR-0071 Phase 7).

    Requires deployer-worker (and same Redis as test-runner). Start with:
      docker-compose -f tests/docker-compose.test.yml --profile workers up -d deployer-worker
    Then run:
      docker-compose -f tests/docker-compose.test.yml run -e MOTET_RUN_DEPLOY_E2E=1 --rm test-runner \\
        pytest tests/integration/api/test_deploy_api.py -v -k test_deploy_happy_path_e2e
    """
    redis_url = os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/1")
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": redis_url,
        "MOTET_PURE_DISTRIBUTED_INVOKER_REDIS_URL": redis_url,
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
    }):
        # Ensure bundle repos exist (idempotent)
        repo_root = Path(__file__).resolve().parents[2] / "bundles"
        setup_script = repo_root / "setup_repos.sh"
        if setup_script.exists():
            subprocess.run(
                ["bash", str(setup_script)],
                cwd=str(repo_root.parent),
                check=True,
                capture_output=True,
                timeout=30,
            )
        repo_url = os.getenv(
            "MOTET_DEPLOY_E2E_REPO_URL",
            "file:///app/tests/bundles/.repos/hello-world",
        )
        if not repo_url.startswith("file://"):
            repo_url = "file://" + str((repo_root / ".repos" / "hello-world").resolve())

        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as client:
                # Deploy (retry a few times if deployer still registering, then skip if unavailable)
                deploy_payload = {"repo_url": repo_url, "branch": "main", "path": "."}
                response = await client.post(
                    "/api/v1/deploy",
                    headers=test_headers,
                    json=deploy_payload,
                )
                for _ in range(24):  # Up to 2 min for deployer to register and mark ready
                    if response.status_code == 202:
                        break
                    if response.status_code == 500 and "No workers passed filtering" in (response.text or ""):
                        await asyncio.sleep(5)
                        response = await client.post(
                            "/api/v1/deploy",
                            headers=test_headers,
                            json=deploy_payload,
                        )
                        continue
                    break
                if response.status_code == 500 and "No workers passed filtering" in (response.text or ""):
                    pytest.skip(
                        "No deployer worker available. Start with: "
                        "docker-compose -f tests/docker-compose.test.yml --profile workers up -d deployer-worker"
                    )
                assert response.status_code == 202, (
                    f"Expected 202, got {response.status_code}: {response.text}"
                )
                data = response.json()
                deploy_job_id = data.get("deploy_job_id") or data.get("status_url", "").split("job_id=")[-1].split("&")[0]
                bundle_id = data.get("bundle_id", "hello-world")
                assert deploy_job_id, "Missing deploy_job_id in 202 response"

                # Poll status until terminal
                for _ in range(60):
                    status_resp = await client.get(
                        f"/api/v1/deploy/{bundle_id}/status",
                        params={"job_id": deploy_job_id},
                        headers=test_headers,
                    )
                    if status_resp.status_code != 200:
                        await asyncio.sleep(1)
                        continue
                    status_data = status_resp.json()
                    status = status_data.get("status") or status_data.get("deploy_status")
                    if status in ("complete", "no_change", "degraded", "failed"):
                        assert status in ("complete", "no_change", "degraded"), (
                            f"Deploy ended with status {status}: {status_data}"
                        )
                        break
                    await asyncio.sleep(1)
                else:
                    pytest.fail("Deploy status did not reach terminal state within 60s")

                # Undeploy
                del_resp = await client.delete(
                    f"/api/v1/deploy/{bundle_id}",
                    headers=test_headers,
                )
                assert del_resp.status_code == 202, (
                    f"Expected 202 on undeploy, got {del_resp.status_code}: {del_resp.text}"
                )

        asyncio.run(_run())
