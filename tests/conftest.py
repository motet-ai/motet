"""
Global test configuration for the distributed AI framework.

Provides common fixtures, test utilities, and pytest configuration.
"""
import sys
import os
import pytest
import pytest_asyncio
import asyncio
from pathlib import Path
import httpx

# Add the project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import common fixtures
from tests.fixtures.distributed_fixtures import *

# Test environment configuration
os.environ.setdefault("MOTET_TEST_MODE", "true")
os.environ.setdefault("MOTET_LOG_LEVEL", "INFO")
os.environ.setdefault("MOTET_STATE_REGISTRY_ENABLED", "true")
# Default API key for test_client so e2e/UI tests pass when app requires auth
os.environ.setdefault("MOTET_API_KEY", "test-api-key")
# Allow principal headers in tests so test_client can authenticate without JWT/Keycloak
os.environ.setdefault("MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS", "true")
# Enable debug mode for tests that exercise debug endpoints
os.environ.setdefault("MOTET_DEBUG_MODE", "true")
# Disable file-based tracing to avoid slow glob on large trace directories
os.environ.setdefault("MOTET_TRACE_ENABLED", "false")

# Redis configuration for tests (use different DB to avoid conflicts)
os.environ.setdefault("MOTET_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("MOTET_PURE_DISTRIBUTED_INVOKER_REDIS_URL", "redis://localhost:6379/2")

# Test timeouts (aggressive to prevent hanging)
# Default: 30 seconds per test
# Integration: 60 seconds (for tests that create app, connect to services)
# Performance: 120 seconds (for slow benchmarks)
TEST_TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "30"))
INTEGRATION_TEST_TIMEOUT = int(os.environ.get("INTEGRATION_TEST_TIMEOUT", "60"))
PERFORMANCE_TEST_TIMEOUT = int(os.environ.get("PERFORMANCE_TEST_TIMEOUT", "120"))


def pytest_configure(config):
    """Configure pytest with custom markers and settings."""
    # Register custom markers
    config.addinivalue_line("markers", "unit: Unit tests for individual components")
    config.addinivalue_line("markers", "integration: Integration tests for component interactions")
    config.addinivalue_line("markers", "performance: Performance and benchmark tests")
    config.addinivalue_line("markers", "e2e: End-to-end user scenario tests")
    config.addinivalue_line("markers", "distributed: Tests requiring distributed system")
    config.addinivalue_line("markers", "mcp: Tests related to MCP server integration")
    config.addinivalue_line("markers", "slow: Slow-running tests (> 10 seconds)")
    config.addinivalue_line("markers", "requires_redis: Tests requiring Redis connection")
    config.addinivalue_line("markers", "requires_external: Tests requiring external services")
    config.addinivalue_line("markers", "stress: Stress tests with high resource usage")
    config.addinivalue_line("markers", "requires_vault: Tests requiring Vault/encryption infrastructure")
    config.addinivalue_line(
        "markers",
        "live_llm: Live provider API adapter matrix gated by MOTET_LIVE_ADAPTER_MATRIX=1",
    )


def pytest_collection_modifyitems(config, items):
    """Modify test collection to add automatic markers and timeouts."""
    for item in items:
        # Add timeout based on markers
        if (
            item.get_closest_marker("performance")
            or item.get_closest_marker("slow")
            or item.get_closest_marker("live_llm")
        ):
            item.add_marker(pytest.mark.timeout(PERFORMANCE_TEST_TIMEOUT))
        elif item.get_closest_marker("integration"):
            item.add_marker(pytest.mark.timeout(INTEGRATION_TEST_TIMEOUT))
        else:
            item.add_marker(pytest.mark.timeout(TEST_TIMEOUT))
        
        # Add asyncio marker to async tests
        if asyncio.iscoroutinefunction(item.function):
            item.add_marker(pytest.mark.asyncio)


def pytest_runtest_setup(item):
    """Setup for each test run."""
    # Skip tests based on markers and environment
    if item.get_closest_marker("requires_redis"):
        # Check if Redis is available
        try:
            import redis
            r = redis.Redis.from_url(os.environ.get("MOTET_REDIS_URL", "redis://localhost:6379/0"))
            r.ping()
        except Exception:
            pytest.skip("Redis not available - install Redis and start server")
    
    if item.get_closest_marker("requires_vault"):
        # Full round-trip: store → retrieve → verify → delete
        try:
            os.environ.setdefault("MOTET_VAULT_MASTER_KEY", "test-vault-master-key")
            from motet.core.security.vault_service import (
                DistributedVaultService, CredentialType, CredentialScope,
                CredentialSecurityLevel, CredentialAccessRequest,
            )
            svc = DistributedVaultService()
            ok = svc.store_credential(
                credential_id="_vault_check",
                credential_data={"_check": True},
                credential_type=CredentialType.CUSTOM,
                scope=CredentialScope.PRINCIPAL,
                security_level=CredentialSecurityLevel.CONFIDENTIAL,
                principal_id="_vault_check",
            )
            if not ok:
                pytest.skip("Vault store_credential returned False")
            req = CredentialAccessRequest(
                principal_id="_vault_check",
                credential_key="_vault_check",
            )
            resp = svc.retrieve_credential(req)
            try:
                svc.delete_credential("_vault_check", "_vault_check")
            except Exception:
                pass
            if not resp.success or resp.credential_data != {"_check": True}:
                pytest.skip("Vault round-trip (store/retrieve) failed")
        except pytest.skip.Exception:
            raise
        except Exception:
            pytest.skip("Vault/encryption infrastructure not available")
    
    if item.get_closest_marker("distributed"):
        # Two Lane-C shapes share the marker:
        # 1) Full HTTP stack e2e — needs MOTET_DISTRIBUTED_STACK_HTTP_URL
        # 2) ASGI + real Celery workers (e.g. openai_compat_distributed) — needs
        #    ready workers only; the distributed_stack fixture still gates (1).
        distributed_http = os.environ.get("MOTET_DISTRIBUTED_STACK_HTTP_URL", "").strip()
        if not distributed_http:
            try:
                from motet.core.distributed.worker_readiness import WorkerReadinessService

                ready = WorkerReadinessService().get_ready_workers()
            except Exception:
                ready = []
            if not ready:
                pytest.skip(
                    "Distributed stack not available (set MOTET_DISTRIBUTED_STACK_HTTP_URL "
                    "or start workers with "
                    "`docker compose -f docker-compose.test.yml --profile workers up -d worker-1`)"
                )

    if item.get_closest_marker("requires_external"):
        external_services_available = os.environ.get("EXTERNAL_SERVICES_AVAILABLE", "false").lower() == "true"
        if not external_services_available:
            pytest.skip("External services not available - start with docker-compose up")


def pytest_runtest_logstart(nodeid, location):
    """Log test start."""
    try:
        print(f"START {nodeid}", flush=True)
    except Exception:
        pass


def pytest_runtest_logreport(report):
    """Log test completion."""
    try:
        if getattr(report, "when", "") == "call":
            print(f"DONE {report.nodeid} [{report.outcome}]", flush=True)
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Setup test environment for the entire test session."""
    print("Setting up test environment...")
    
    # Ensure test directories exist
    test_dirs = ["logs", "traces", "tmp"]
    for dir_name in test_dirs:
        dir_path = project_root / dir_name
        dir_path.mkdir(exist_ok=True)
    
    yield
    
    print("Cleaning up test environment...")
    # Cleanup can be added here if needed


@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temporary directory for tests."""
    return tmp_path


@pytest.fixture
def project_root_path():
    """Provide the project root path."""
    return project_root


@pytest_asyncio.fixture
async def test_client():
    """Provide an HTTP test client for API testing."""
    from motet.interfaces.http import app

    transport = httpx.ASGITransport(app=app)
    api_key = os.environ.get("MOTET_API_KEY", "test-api-key")
    headers = {
        "X-API-Key": api_key,
        "X-Principal-Id": "test-principal",
        "X-Tenant-Id": "test-tenant",
    }
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        timeout=30.0,
        headers=headers,
    ) as client:
        yield client


@pytest.fixture
def distributed_stack():
    """Provide distributed stack URL for WebSocket/integration tests. Skips when not running full stack."""
    http_url = os.environ.get("MOTET_DISTRIBUTED_STACK_HTTP_URL", "").strip()
    if not http_url:
        pytest.skip(
            "distributed_stack required (set MOTET_DISTRIBUTED_STACK_HTTP_URL or run with Docker for full stack)"
        )
    worker_urls = os.environ.get("MOTET_DISTRIBUTED_STACK_WORKER_URLS", "").strip().split()
    return {"http_url": http_url, "worker_urls": worker_urls or []}


@pytest.fixture
def ui_test_scenarios():
    """Provide test scenarios for UI testing."""
    return {
        "basic_chat": {
            "method": "POST",
            "endpoint": "/api/v1/chat",
            "payload": {
                "messages": [{"role": "user", "content": "Hello, this is a test message"}],
                "stream": False
            },
            "expected_status": 200,
            "expected_fields": ["content"]
        },
        "streaming_chat": {
            "method": "POST",
            "endpoint": "/api/v1/chat",
            "payload": {
                "messages": [{"role": "user", "content": "Tell me a short story"}],
                "stream": True
            },
            "expected_status": 200,
            "expected_content_type": "text/event-stream"
        },
        "tool_execution": {
            "method": "POST",
            "endpoint": "/api/v1/tools/execute",
            "payload": {
                "name": "core.math_eval",
                "params": {"expression": "2 + 2"}
            },
            "expected_status": 200
        },
        "memory_operations": {
            "method": "GET",
            "endpoint": "/api/v1/memories",
            "expected_status": 200
        }
    }
