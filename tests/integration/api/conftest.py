"""
Shared test configuration and fixtures for API integration tests.

This module provides common fixtures, utilities, and configuration
for all API integration tests.
"""

from __future__ import annotations

import os
import pytest
from contextlib import contextmanager
from typing import Dict, Any

from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.security.service_accounts import ServiceAccountManager


# Test environment configuration
DEFAULT_TEST_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_TEST_API_KEY = "test-key"


@contextmanager
def with_env(vars: dict[str, str]):
    """
    Context manager for temporarily setting environment variables.
    
    Args:
        vars: Dictionary of environment variables to set
        
    Yields:
        None
        
    Example:
        >>> with with_env({"MOTET_API_KEY": "test"}):
        ...     # Environment variables are set
        ...     pass
        # Environment variables are restored
    """
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


def get_test_env_vars(**overrides) -> Dict[str, str]:
    """
    Get standard test environment variables with optional overrides.
    
    Args:
        **overrides: Additional or override environment variables
        
    Returns:
        Dictionary of environment variables for testing
        
    Example:
        >>> env = get_test_env_vars(MOTET_LOG_LEVEL="DEBUG")
        >>> with with_env(env):
        ...     # Test code here
        ...     pass
    """
    base_env = {
        "MOTET_API_KEY": DEFAULT_TEST_API_KEY,
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", DEFAULT_TEST_REDIS_URL),
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false"
    }
    base_env.update(overrides)
    return base_env


def is_oauth_configured() -> bool:
    """
    Check if OAuth/JWT is configured for testing.
    
    Returns:
        True if OAuth is configured, False otherwise
    """
    return bool(
        os.getenv("MOTET_JWT_JWKS_URL") and 
        os.getenv("MOTET_JWT_ISSUER") and
        os.getenv("MOTET_KEYCLOAK_CLIENT_ID")
    )


def skip_if_oauth_not_configured(test_func):
    """
    Decorator to skip tests if OAuth is not configured.
    
    Args:
        test_func: Test function to wrap
        
    Returns:
        Wrapped test function that skips if OAuth not configured
        
    Example:
        >>> @skip_if_oauth_not_configured
        ... def test_oauth_flow():
        ...     # Test code
        ...     pass
    """
    return pytest.mark.skipif(
        not is_oauth_configured(),
        reason="OAuth/JWT not configured (set MOTET_JWT_JWKS_URL, MOTET_JWT_ISSUER, MOTET_KEYCLOAK_CLIENT_ID)"
    )(test_func)


@pytest.fixture(scope="function")
def isolated_async_redis():
    """
    Give the calling test fresh async Redis connections.

    The Redis manager caches async clients and their connection pool for the
    process, but each test function runs on its own event loop. A cached client
    therefore holds connections bound to a loop that has already closed, and
    every await against it raises "Event loop is closed" — which silently turns
    Redis-backed assertions into vacuous ones. Dropping the async caches around
    the test forces connections to be rebuilt on the live loop.

    Sync clients are left alone: they are not loop-bound.

    Example:
        >>> @pytest.fixture(autouse=True)
        ... def _redis(isolated_async_redis):
        ...     '''Every test in this module talks to Redis asynchronously.'''
    """
    from motet.core.distributed import redis_manager

    def reset() -> None:
        manager = redis_manager.get_redis_manager()
        manager._async_clients.clear()
        manager._pubsub_clients.clear()
        manager._async_connection_pool = None
        manager._pubsub_connection_pool = None
        manager._initialized = False

    reset()
    yield
    reset()


@pytest.fixture(scope="function")
def test_service_account_token_factory():
    """
    Factory fixture for creating test service account tokens.
    
    Yields:
        Function that creates service account tokens
        
    The factory function signature:
        create_token(name: str = "test-api", tenant_id: str = "test-tenant") -> str
        
    Example:
        >>> def test_something(test_service_account_token_factory):
        ...     token = test_service_account_token_factory("my-test")
        ...     # Use token in test
        ...     pass
    """
    redis_client = get_sync_redis_client("test_api_fixture")
    sa_manager = ServiceAccountManager(redis_client)
    created_tokens = []
    
    def create_token(name: str = "test-api", tenant_id: str = "test-tenant") -> str:
        """Create a test service account token."""
        token = sa_manager.create_service_account(
            name=name,
            tenant_id=tenant_id,
            motet_id="production",
            roles=["admin", "user"],
            created_by="test@example.com",
            expires_days=1
        )
        created_tokens.append(token)
        return token
    
    yield create_token
    
    # Cleanup all created tokens
    for token in created_tokens:
        try:
            sa_manager.revoke_service_account(token)
        except Exception:
            pass  # Ignore cleanup errors


@pytest.fixture(scope="function")
def test_service_account_token(test_service_account_token_factory):
    """
    Fixture providing a single test service account token.
    
    Yields:
        Service account token string
        
    Example:
        >>> def test_api(test_service_account_token):
        ...     headers = {"Authorization": f"Bearer {test_service_account_token}"}
        ...     # Use headers in API call
        ...     pass
    """
    return test_service_account_token_factory()


@pytest.fixture(scope="function")
def test_headers(test_service_account_token):
    """
    Fixture providing standard test headers with authentication.
    
    Args:
        test_service_account_token: Service account token fixture
        
    Returns:
        Dictionary of HTTP headers for API testing
        
    Example:
        >>> def test_api_endpoint(test_headers):
        ...     response = await client.get("/api/v1/test", headers=test_headers)
        ...     assert response.status_code == 200
    """
    return {
        "X-API-Key": DEFAULT_TEST_API_KEY,
        "Authorization": f"Bearer {test_service_account_token}",
        "Content-Type": "application/json"
    }


# Export utility functions and constants
__all__ = [
    "with_env",
    "get_test_env_vars",
    "is_oauth_configured",
    "skip_if_oauth_not_configured",
    "DEFAULT_TEST_REDIS_URL",
    "DEFAULT_TEST_API_KEY",
]

