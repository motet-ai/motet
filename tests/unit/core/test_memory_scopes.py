"""
Motet - Memory Find Scope API Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    HTTP tests for POST /api/v1/memories/find scope filtering (wm / both / ltm).
    Find builds a MotetStack per request; this test reuses the app stack so the
    seeded in-memory KV store is the one recall reads.

Dependencies:
    - httpx: In-process ASGI client
    - motet.interfaces.http.create_app
    - motet.interfaces.api.v1.memories._stack_for_principal

Usage:
    pytest tests/unit/core/test_memory_scopes.py
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from motet.interfaces.api.shared.identity import attach_principal_to_stack
from motet.interfaces.api.v1 import memories as memories_api
from motet.interfaces.http import create_app


async def _post_json(client, url: str, body: dict):
    r = await client.post(url, json=body)
    return r.status_code, r.json()


@pytest.mark.requires_external
def test_memory_find_scopes_via_api(monkeypatch):
    async def _run():
        # Dev-mode principal headers (no X-API-Key — it would win over X-Principal-Id and break identity)
        monkeypatch.delenv("MOTET_API_KEY", raising=False)
        monkeypatch.setenv("MOTET_RATE_LIMIT_PER_MINUTE", "0")
        monkeypatch.setenv("MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS", "true")
        monkeypatch.setenv("MOTET_JWT_JWKS_URL", "")
        monkeypatch.setenv("MOTET_JWT_PUBLIC_KEY_PEM", "")
        app = create_app()
        stack = getattr(app.state, "stack", None)
        mm = getattr(stack, "memory_manager", None)
        if stack is None or mm is None:
            pytest.skip("MemoryManager not available on stack")

        def _shared_stack(principal):
            attach_principal_to_stack(stack, principal)
            return stack.config, stack

        monkeypatch.setattr(memories_api, "_stack_for_principal", _shared_stack)

        transport = httpx.ASGITransport(app=app)
        headers = {
            "X-Principal-Id": "test-principal",
            "X-Tenant-Id": "test-tenant",
            "X-Motet-Id": "default",
        }
        conv_id = "testconv-wm"
        ctx = SimpleNamespace(
            principal_id="test-principal",
            tenant_id="test-tenant",
            motet_id="default",
            conversation_id=conv_id,
        )
        base_tags = [f"conversation:{conv_id}", "conversation"]
        a = mm.store_memory(
            content="assistant reply",
            type="assistant_response",
            tags=list(base_tags),
            motet_context=ctx,
        )
        u = mm.store_memory(
            content="user hi",
            type="user_message",
            tags=list(base_tags),
            motet_context=ctx,
        )
        if not a.get("stored_in") or not u.get("stored_in"):
            pytest.skip("Memory KV seed failed (redis/encryption); check test stack")

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=5, headers=headers) as client:
            status, res = await _post_json(
                client,
                "/api/v1/memories/find",
                {
                    "tags": [f"conversation:{conv_id}", "conversation"],
                    "scope": "wm",
                    "limit": 10,
                },
            )
            assert status == 200
            items = res.get("items") or []
            assert any(i.get("type") == "assistant_response" for i in items)
            assert all("wm" in (i.get("tags") or []) for i in items)

            await asyncio.sleep(0.05)
            status, res2 = await _post_json(
                client,
                "/api/v1/memories/find",
                {
                    "tags": [f"conversation:{conv_id}", "conversation"],
                    "scope": "both",
                    "limit": 10,
                },
            )
            assert status == 200
            types = {i.get("type") for i in (res2.get("items") or [])}
            assert "assistant_response" in types and "user_message" in types

            status, res3 = await _post_json(
                client,
                "/api/v1/memories/find",
                {
                    "tags": [f"conversation:{conv_id}", "conversation"],
                    "scope": "ltm",
                    "limit": 5,
                },
            )
            assert status == 200
            if not getattr(stack, "vector", None):
                assert (res3.get("items") or []) == []

    asyncio.run(_run())
