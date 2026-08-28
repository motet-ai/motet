import httpx
import asyncio
import json
from motet.interfaces.http import create_app


def test_jwt_missing_bearer_rejected(monkeypatch):
    # Configure a fake JWKS URL so middleware is active
    monkeypatch.setenv("MOTET_JWT_JWKS_URL", "http://jwks.local/.well-known/jwks.json")

    # Mock requests.get to return a minimal JWKS
    import types
    class DummyResp:
        def __init__(self, data):
            self._data = data
        def raise_for_status(self):
            return None
        def json(self):
            return self._data
    def fake_get(url, timeout=3):
        return DummyResp({"keys": []})

    import motet.interfaces.http as api_mod
    # Patch requests in module scope at runtime use
    api_mod.requests = types.SimpleNamespace(get=fake_get)

    app = create_app()
    transport = httpx.ASGITransport(app=app)

    async def _run():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/v1/tools", headers={"X-API-Key": ""})
            # Missing bearer should yield 401 due to JWT being configured
            assert r.status_code == 401

    asyncio.run(_run())


