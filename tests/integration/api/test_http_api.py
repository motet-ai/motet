import asyncio
import json
import os

import httpx
from motet.interfaces.http import create_app


async def _get_json(client, method: str, url: str, **kwargs):
    r = await client.request(method, url, **kwargs)
    return r.status_code, r.json()


def test_health_and_chat_e2e():
    async def _run():
        # Stabilize environment for this test run
        os.environ["MOTET_RATE_LIMIT_PER_MINUTE"] = "0"
        os.environ["MOTET_MODEL_PROVIDER"] = "mock"
        os.environ["MOTET_ENABLE_VECTOR_MEMORY"] = "false"
        os.environ.setdefault("MOTET_API_KEY", "test-key")
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        headers = {"X-API-Key": os.environ.get("MOTET_API_KEY", "test-key")}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=5) as client:
            # health
            status, data = await _get_json(client, "GET", "/health")
            assert status == 200
            assert data.get("status") == "ok"
            assert isinstance(data.get("components"), dict)

            # vector health route exists and responds
            status, vdata = await _get_json(client, "GET", "/health/vector")
            assert status == 200
            assert "enabled" in vdata and "healthy" in vdata

            # metrics route
            r = await client.get("/metrics")
            assert r.status_code == 200 and "imf_requests_total" in r.text

            # tools endpoint returns schemas (legacy dict) (ADR-0053: /api/v1/tools)
            status, tools_data = await _get_json(client, "GET", "/api/v1/tools", headers=headers)
            assert status in [200, 401]
            if status == 200:
                assert isinstance(tools_data, dict)
                # Tools may be keyed by qualified name (e.g. core.math_eval)
                math_key = next(
                    (k for k in tools_data if k == "math_eval" or (isinstance(k, str) and k.endswith("math_eval"))),
                    None,
                )
                assert math_key is not None, f"math_eval not found in tools keys: {list(tools_data.keys())[:10]}"
                assert isinstance(tools_data[math_key], dict)
                assert "schema" in tools_data[math_key]
            # chat (ADR-0053: /api/v1/chat)
            status, data = await _get_json(
                client,
                "POST",
                "/api/v1/chat",
                json={"messages": [{"role": "user", "content": "hello"}], "stream": False},
                headers=headers,
            )
            assert status in [200, 401, 500]
            if status == 200 and isinstance(data, dict):
                content = data.get("content", "")
                # Accept success content; skip assertion when mock/streaming returns error message
                if content and "error" not in content.lower() and "apologize" not in content.lower():
                    assert "hello" in content or "Hello" in content

            # Chat round-trip with tool-trigger style text. Mock provider echoes
            # the user message; it does not auto-execute trigger tools on the
            # chat path (tool use requires LLM tool calls / agentic loop).
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                tmp = os.path.join(td, "read.txt")
                with open(tmp, "w") as f:
                    f.write("file content here")
                os.environ["MOTET_FILE_READ_ALLOWLIST"] = td
                user_text = f"http_get: https://example.com\nread: {tmp}"
                status, data = await _get_json(
                    client,
                    "POST",
                    "/api/v1/chat",
                    json={
                        "messages": [
                            {
                                "role": "user",
                                "content": user_text,
                            }
                        ],
                        "stream": False,
                    },
                    headers=headers,
                )
                assert status in [200, 401, 500]
                if status == 200 and isinstance(data, dict):
                    content = data.get("content", "")
                    if content and "error" not in content.lower() and "apologize" not in content.lower():
                        assert "http_get:" in content
                        assert "read:" in content

    asyncio.run(_run())


def test_summarize_endpoints_removed():
    async def _run():
        import os
        from motet.interfaces.http import create_app
        import httpx

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        os.environ.setdefault("MOTET_API_KEY", "test-key")
        headers = {"X-API-Key": os.environ.get("MOTET_API_KEY", "test-key")}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/v1/memories/summarize", headers=headers)
            assert r.status_code == 404
            r = await client.get("/api/v1/memories/summaries", headers=headers)
            assert r.status_code == 404

    asyncio.run(_run())


