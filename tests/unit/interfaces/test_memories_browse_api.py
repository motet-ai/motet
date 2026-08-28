"""
Tests for GET /api/v1/memories/browse, /stats, and POST /forget.

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Unit tests for manage-app memory browse/stats/forget endpoints with a
    stubbed collector and memory manager (no Redis or Celery).

Dependencies:
    - pytest, httpx
    - motet.interfaces.http

Usage:
    pytest tests/unit/interfaces/test_memories_browse_api.py -q
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from motet.core.config import Config
from motet.core.types import MemoryItem
from motet.interfaces.http import create_app


def _client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_JWT_JWKS_URL", "")
    monkeypatch.setenv("MOTET_JWT_PUBLIC_KEY_PEM", "")
    monkeypatch.setenv("MOTET_API_KEY", "")
    monkeypatch.setenv("MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS", "true")


def _headers() -> dict[str, str]:
    return {
        "X-Principal-Id": "p-browse",
        "X-Tenant-Id": "t-browse",
        "X-Motet-Id": "production",
    }


def _sample_items() -> list[MemoryItem]:
    now = datetime.now(timezone.utc)
    return [
        MemoryItem(
            id="mem-note",
            type="note",
            content="Quarterly goals",
            tags=["ltm", "docs", "agent:core.default"],
            metadata={"agent_id": "core.default"},
            tenant_id="t-browse",
            motet_id="production",
            conversation_id="conv-1",
            created_at=now,
        ),
        MemoryItem(
            id="mem-user",
            type="user_message",
            content="hello",
            tags=["wm"],
            tenant_id="t-browse",
            motet_id="production",
            created_at=now,
        ),
    ]


def test_browse_memories_filters_collected_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _client_env(monkeypatch)
    items = _sample_items()
    monkeypatch.setattr(
        "motet.interfaces.api.v1.memories.collect_memories_for_scope",
        lambda *_args, **_kwargs: items,
    )
    monkeypatch.setattr(
        "motet.interfaces.api.v1.memories._stack_for_principal",
        lambda _principal: (Config(), SimpleNamespace(memory=object())),
    )

    async def _run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
            response = await client.get(
                "/api/v1/memories/browse",
                params={"q": "goals", "tier": "ltm", "agent": "core.default"},
                headers=_headers(),
            )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == "mem-note"
        assert data["query"] == "goals"

    asyncio.run(_run())


def test_memory_stats_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _client_env(monkeypatch)
    monkeypatch.setattr(
        "motet.interfaces.api.v1.memories.collect_memories_for_scope",
        lambda *_args, **_kwargs: _sample_items(),
    )
    monkeypatch.setattr(
        "motet.interfaces.api.v1.memories.count_memory_index",
        lambda *_args, **_kwargs: (0, 0),
    )
    cfg = Config()
    cfg.enable_vector_memory = True
    monkeypatch.setattr(
        "motet.interfaces.api.v1.memories._stack_for_principal",
        lambda _principal: (cfg, SimpleNamespace(memory=object())),
    )

    async def _run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
            response = await client.get("/api/v1/memories/stats", headers=_headers())
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total_memories"] == 2
        assert data["last_24h"] == 2
        assert data["vector_enabled"] is True
        assert data["tier_breakdown"]["ltm"] == 1
        assert data["tier_breakdown"]["wm"] == 1

    asyncio.run(_run())


def test_memory_stats_prefers_index_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    _client_env(monkeypatch)
    monkeypatch.setattr(
        "motet.interfaces.api.v1.memories.collect_memories_for_scope",
        lambda *_args, **_kwargs: _sample_items(),
    )
    monkeypatch.setattr(
        "motet.interfaces.api.v1.memories.count_memory_index",
        lambda *_args, **_kwargs: (99, 7),
    )
    monkeypatch.setattr(
        "motet.interfaces.api.v1.memories._stack_for_principal",
        lambda _principal: (Config(), SimpleNamespace(memory=object())),
    )

    async def _run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
            response = await client.get("/api/v1/memories/stats", headers=_headers())
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total_memories"] == 99
        assert data["last_24h"] == 7
        assert data["tier_breakdown"]["ltm"] == 1

    asyncio.run(_run())


def test_forget_memories_requires_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    _client_env(monkeypatch)

    async def _run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
            response = await client.post(
                "/api/v1/memories/forget",
                json={},
                headers=_headers(),
            )
        assert response.status_code == 400
        assert "required" in response.json().get("detail", "").lower()

    asyncio.run(_run())


def test_forget_memories_calls_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    _client_env(monkeypatch)
    captured: dict[str, object] = {}

    class _Manager:
        def forget(self, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {"deleted": 1, "ids": ["mem-note"], "vector_deleted": 0}

    monkeypatch.setattr(
        "motet.interfaces.api.v1.memories._stack_for_principal",
        lambda _principal: (Config(), SimpleNamespace(memory=object(), memory_manager=_Manager())),
    )

    async def _run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
            response = await client.post(
                "/api/v1/memories/forget",
                json={"memory_ids": ["mem-note"]},
                headers=_headers(),
            )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["deleted"] == 1
        assert data["ids"] == ["mem-note"]
        assert captured["memory_ids"] == ["mem-note"]

    asyncio.run(_run())
