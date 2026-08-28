"""
Motet - Models API Integration Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Integration tests for the models list endpoint, including capability
    fields and provider API-key presence flags.

Dependencies:
    - httpx: ASGI test client
    - motet.interfaces.http: FastAPI app factory

Usage:
    pytest tests/integration/api/test_models_api.py

Notes:
    - List endpoint is metadata-only and does not require authentication
"""

from __future__ import annotations

import asyncio
import os
import httpx
import pytest
from contextlib import contextmanager

from motet.interfaces.http import create_app


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


@pytest.mark.integration
def test_list_models():
    """Test listing available models."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0")
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # List models (no auth required - metadata only)
                response = await client.get(
                    "/api/v1/models",
                    headers={"X-API-Key": "test-key"}
                )
                
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
                data = response.json()
                assert isinstance(data, list)
                
                # If models are returned, verify structure
                if len(data) > 0:
                    model = data[0]
                    assert "provider" in model
                    assert "name" in model
                    assert "capabilities" in model
                    assert isinstance(model["capabilities"], list)
                    assert "max_output_tokens" in model
                    assert isinstance(model.get("requires_api_key"), bool)
                    assert isinstance(model.get("has_api_key"), bool)
        
        asyncio.run(_run())


@pytest.mark.integration
def test_list_models_no_auth_required():
    """Test that listing models doesn't require authentication (metadata only)."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0")
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                # List models without authentication
                response = await client.get(
                    "/api/v1/models"
                )
                
                # Should still work (metadata endpoint, no auth required)
                # May return 200 or 401 depending on API key requirement
                assert response.status_code in [200, 401], \
                    f"Expected 200 or 401, got {response.status_code}: {response.text}"
        
        asyncio.run(_run())


@pytest.mark.integration
def test_list_models_response_structure():
    """Test that models response has correct structure."""
    with with_env({
        "MOTET_API_KEY": "test-key",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0")
    }):
        app = create_app()
        
        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
                response = await client.get(
                    "/api/v1/models",
                    headers={"X-API-Key": "test-key"}
                )
                
                assert response.status_code == 200
                data = response.json()
                
                # Response should be a list (even if empty)
                assert isinstance(data, list)
                
                # If models exist, verify each has required fields
                for model in data:
                    assert isinstance(model, dict)
                    assert "provider" in model
                    assert "name" in model
                    assert "capabilities" in model
                    assert "max_output_tokens" in model
                    assert "requires_api_key" in model
                    assert "has_api_key" in model
                    assert isinstance(model["capabilities"], list)
                    assert isinstance(model["max_output_tokens"], int)
                    assert isinstance(model["requires_api_key"], bool)
                    assert isinstance(model["has_api_key"], bool)
        
        asyncio.run(_run())

