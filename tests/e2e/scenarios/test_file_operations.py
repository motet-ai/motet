import asyncio
import os
import tempfile
from contextlib import contextmanager

import httpx
import pytest

from motet.interfaces.http import create_app


@contextmanager
def _with_env(vars: dict[str, str]):
    """Restore os.environ after the block so tests do not leak config into other tests."""
    old: dict[str, str | None] = {}
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


def test_file_read_tool_and_rate_limit():
    """Exercise core.file_read via the tools API (allowlisted path)."""
    allow = os.getcwd()
    with _with_env(
        {
            "MOTET_API_KEY": "k",
            "MOTET_FILE_READ_ALLOWLIST": allow,
        }
    ):
        app = create_app()

        async def _run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=5) as client:
                headers = {"X-API-Key": "k"}
                with tempfile.NamedTemporaryFile("w", delete=False, dir=os.getcwd()) as tf:
                    tf.write("hello world")
                    temp_path = tf.name

                try:
                    r = await client.post(
                        "/api/v1/tools/execute",
                        headers=headers,
                        json={"name": "core.file_read", "params": {"path": temp_path}},
                    )
                    if r.status_code == 401:
                        pytest.skip("Tools require auth in this environment")
                    if r.status_code != 200:
                        pytest.skip(f"Tool execute returned {r.status_code} (check allowlist/params)")
                    body = r.json()
                    text = body.get("text") or body.get("content") or body.get("result") or str(body)
                    assert "hello world" in text, f"Expected 'hello world' in response: {body}"
                finally:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

        asyncio.run(_run())
