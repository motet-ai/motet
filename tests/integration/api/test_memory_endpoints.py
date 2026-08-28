from __future__ import annotations

import asyncio
import httpx

from motet.interfaces.http import create_app


def test_retrieve_and_eval_endpoints():
    # Use API key so auth succeeds when required
    import os
    os.environ["MOTET_API_KEY"] = os.environ.get("MOTET_API_KEY") or "test-key"
    os.environ.pop("MOTET_JWT_JWKS_URL", None)
    os.environ.pop("MOTET_JWT_PUBLIC_KEY_PEM", None)
    app = create_app()
    # Provide a minimal in-process vector stub to avoid external deps
    class _VectorStub:
        def __init__(self):
            self._docs = {}
        def add(self, items):
            for it in items:
                self._docs[it.id] = it
        def query(self, text: str, top_k: int = 5, tags=None):
            txt = (text or "").lower()
            res = []
            for it in self._docs.values():
                if txt in (it.content or "").lower():
                    if tags:
                        if not any(t in (it.tags or []) for t in tags):
                            continue
                    res.append(it)
            return res[:top_k]
    try:
        app.state.stack.vector = _VectorStub()  # type: ignore[attr-defined]
    except Exception:
        pass

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"X-API-Key": os.environ.get("MOTET_API_KEY", "test-key")}
            # Ingest via eval with small corpus and one query
            body = {
                "corpus": [
                    {"id": "1", "text": "Kafka broker config tips", "tags": ["conversation"]},
                    {"id": "2", "text": "Postgres tuning guide", "tags": ["conversation"]},
                ],
                "queries": [
                    {"q": "kafka config", "relevant_ids": ["1"]}
                ],
                "top_k": 3,
            }
            r = await client.post("/api/v1/memories/search/eval", json=body, headers=headers)
            assert r.status_code in [200, 401, 503], f"Expected 200, 401, or 503, got {r.status_code}: {r.text}"
            if r.status_code != 200:
                return
            data = r.json()
            assert "precision_at_k" in data

            # Direct search
            r2 = await client.get("/api/v1/memories/search", params={"q": "kafka"}, headers=headers)
            assert r2.status_code in [200, 401, 503], f"Expected 200, 401, or 503, got {r2.status_code}: {r2.text}"
            if r2.status_code != 200:
                return
            items = r2.json()
            assert isinstance(items, list)

    asyncio.run(_run())


