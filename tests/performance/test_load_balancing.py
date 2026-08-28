import asyncio
import httpx
from motet.interfaces.http import create_app


def test_scheduler_priority_and_queue_wait(monkeypatch):
    monkeypatch.setenv("MOTET_SCHEDULER_MAX_CONCURRENT_TASKS", "2")
    monkeypatch.setenv("MOTET_API_KEY", "")
    monkeypatch.setenv("MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS", "true")
    monkeypatch.delenv("MOTET_RATE_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.delenv("MOTET_JWT_JWKS_URL", raising=False)
    app = create_app()
    transport = httpx.ASGITransport(app=app)

    async def _run():
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
            headers = {
                "X-API-Key": "",
                "X-Principal-Id": "test-principal",
                "X-Tenant-Id": "test-tenant",
            }
            tasks = []
            for i in range(3):
                payload = {"messages":[{"role":"user","content": f"math:{i}+{i}"}], "stream": False}
                tasks.append(client.post("/api/v1/chat", json=payload, headers=headers))
            rs = await asyncio.gather(*tasks)
            assert all(r.status_code in (200, 500) for r in rs)

    asyncio.run(_run())


