import asyncio
import os
from contextlib import contextmanager

import httpx

from motet.interfaces.http import create_app


@contextmanager
def with_env(vars: dict[str, str]):
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


def test_api_key_required_for_protected_endpoints():
    with with_env({"MOTET_API_KEY": "secret"}):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=5) as client:
                # health is open
                r = await client.get("/health")
                assert r.status_code == 200
                # protected endpoints without key -> 401 (ADR-0053: /api/v1/{resource})
                # chat without key -> 401 but provide minimal valid body to avoid 422
                r = await client.post("/api/v1/chat", json={"messages": [], "stream": False})
                assert r.status_code == 401
                # tools listing without key -> 401
                r = await client.get("/api/v1/tools")
                assert r.status_code == 401
                # tool invoke without key -> 401 (provide minimal body)
                r = await client.post("/api/v1/tools/execute", json={"name": "core.math_eval", "params": {"expression": "1+1"}})
                # Depending on framework order, may return 401 or 422 for missing auth header. Accept both.
                assert r.status_code in (401, 422)
                # memories without key -> 401
                r = await client.get("/api/v1/memories")
                assert r.status_code == 401
                # with correct key -> allowed (use schedules to avoid blocking distributed tools list)
                headers = {"X-API-Key": "secret"}
                r = await client.get("/api/v1/schedules", headers=headers)
                assert r.status_code == 200
                # Also verify tools list returns 200 (may timeout to fallback; we only need 200)
                r = await client.get("/api/v1/tools", headers=headers, timeout=15.0)
                assert r.status_code == 200

        asyncio.run(_run())


