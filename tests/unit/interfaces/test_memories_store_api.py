"""
Tests for POST /api/v1/memories/store and response parsing.

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-06

Description:
    Unit tests for memory store API helpers and the store endpoint with a
    stubbed distributed invoker (no Celery).

Dependencies:
    - pytest, httpx
    - motet.interfaces.http

Usage:
    pytest tests/unit/interfaces/test_memories_store_api.py -v
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from motet.core.config import Config
from motet.interfaces.api.v1.memories import _parse_memory_store_command_result
from motet.interfaces.http import create_app


def test_parse_memory_store_success_adr_payload() -> None:
    out = _parse_memory_store_command_result(
        {"status": "success", "data": {"memory_id": "m1", "stored": True}}
    )
    assert out.memory_id == "m1"
    assert out.stored is True


def test_parse_memory_store_success_flat_payload() -> None:
    out = _parse_memory_store_command_result({"memory_id": "m2", "stored": True})
    assert out.memory_id == "m2"
    assert out.stored is True


def test_parse_memory_store_error_status_raises() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_memory_store_command_result(
            {"status": "error", "error": {"message": "no memory"}}
        )
    assert exc.value.status_code == 500
    assert "no memory" in (exc.value.detail or "")


def test_parse_memory_store_invalid_raises() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_memory_store_command_result("not-a-dict")  # type: ignore[arg-type]
    assert exc.value.status_code == 500


def test_parse_memory_store_unexpected_shape_raises() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_memory_store_command_result({"foo": "bar"})
    assert exc.value.status_code == 500


def _store_client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOTET_JWT_JWKS_URL", "")
    monkeypatch.setenv("MOTET_JWT_PUBLIC_KEY_PEM", "")
    monkeypatch.setenv("MOTET_API_KEY", "")
    monkeypatch.setenv("MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS", "true")


def test_store_memory_endpoint_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _store_client_env(monkeypatch)

    def _fake_stack_ok(_principal: object) -> tuple[Config, SimpleNamespace]:
        return Config(), SimpleNamespace(memory=object())

    monkeypatch.setattr(
        "motet.interfaces.api.v1.memories._stack_for_principal",
        _fake_stack_ok,
    )

    import motet.core.workers.command_invoker as command_invoker

    def _fake_execute(_command: object) -> dict:
        return {"status": "success", "data": {"memory_id": "mem-api-test", "stored": True}}

    monkeypatch.setattr(command_invoker.new_global_invoker, "initialize", lambda: None)
    monkeypatch.setattr(command_invoker.new_global_invoker, "execute_command", _fake_execute)

    async def _run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
            r = await client.post(
                "/api/v1/memories/store",
                json={
                    "content": "hello from store test",
                    "type": "note",
                    "tags": ["unit_test"],
                },
                headers={
                    "X-Principal-Id": "p-store",
                    "X-Tenant-Id": "t-store",
                },
            )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("memory_id") == "mem-api-test"
        assert data.get("stored") is True

    asyncio.run(_run())


def test_store_memory_endpoint_no_memory_manager_503(monkeypatch: pytest.MonkeyPatch) -> None:
    _store_client_env(monkeypatch)

    def _fake_stack(_principal: object) -> tuple[Config, SimpleNamespace]:
        return Config(), SimpleNamespace(memory=None)

    monkeypatch.setattr(
        "motet.interfaces.api.v1.memories._stack_for_principal",
        _fake_stack,
    )

    async def _run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
            r = await client.post(
                "/api/v1/memories/store",
                json={"content": "x"},
                headers={
                    "X-Principal-Id": "p-store",
                    "X-Tenant-Id": "t-store",
                },
            )
        assert r.status_code == 503
        assert "not available" in r.json().get("detail", "").lower()

    asyncio.run(_run())


def test_store_memory_endpoint_empty_content_422(monkeypatch: pytest.MonkeyPatch) -> None:
    _store_client_env(monkeypatch)

    async def _run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
            r = await client.post(
                "/api/v1/memories/store",
                json={"content": ""},
                headers={
                    "X-Principal-Id": "p-store",
                    "X-Tenant-Id": "t-store",
                },
            )
        assert r.status_code == 422

    asyncio.run(_run())
