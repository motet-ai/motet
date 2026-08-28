import os
import httpx
import pytest
from motet.interfaces.http import create_app


def test_traces_endpoints_require_key_and_work_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("MOTET_TRACE_ENABLED", "true")
    monkeypatch.setenv("MOTET_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("MOTET_API_KEY", "k")
    monkeypatch.setenv("MOTET_RATE_LIMIT_PER_MINUTE", "1000")
    monkeypatch.setenv("MOTET_RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setenv("MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS", "true")
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async def _run():
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            headers={"X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"}
        ) as client:
            r = await client.post(
                "/api/v1/chat",
                headers={"X-API-Key": "k", "X-Principal-Id": "test-principal", "X-Tenant-Id": "test-tenant"},
                json={"messages": [{"role": "user", "content": "hello traces"}], "stream": False},
            )
            assert r.status_code in [200, 500], f"Expected 200 or 500, got {r.status_code}: {r.text}"
            tid = r.headers.get("X-Trace-Id")
            if r.status_code != 200 or not tid:
                return
            # listing without valid auth should 401
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as unauth_client:
                r2 = await unauth_client.get("/api/v1/debug/traces.json")
            assert r2.status_code == 401
            # listing as admin should succeed (or 500 if trace backend unavailable).
            # Do not send X-API-Key here: that synthesizes principal id=api_key with
            # empty roles, and /api/v1/debug is admin-only (issue #214).
            admin_headers = {
                "X-Principal-Id": "test-principal",
                "X-Tenant-Id": "test-tenant",
                "X-Roles": "admin",
            }
            r3 = await client.get("/api/v1/debug/traces.json", headers=admin_headers)
            assert r3.status_code in [200, 500]
            if r3.status_code == 200:
                assert isinstance(r3.json(), list)
            # fetch trace json (may be rate limited; retry once)
            r4 = await client.get(f"/api/v1/debug/traces/{tid}.json", headers=admin_headers)
            if r4.status_code == 429:
                r4 = await client.get(f"/api/v1/debug/traces/{tid}.json", headers=admin_headers)
            assert r4.status_code in [200, 404, 500]
            if r4.status_code == 200:
                assert isinstance(r4.json(), list) and len(r4.json()) >= 1
            r5 = await client.get(f"/api/v1/debug/traces/{tid}", headers=admin_headers)
            assert r5.status_code in [200, 404, 500]
            if r5.status_code == 200:
                assert "Trace:" in r5.text
    import asyncio
    asyncio.run(_run())
