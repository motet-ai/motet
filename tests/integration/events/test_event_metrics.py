from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from motet.interfaces.http import create_app
from motet.core.workers import global_bus

pytestmark = [pytest.mark.integration, pytest.mark.requires_external]


def test_events_stats_endpoint():
    app = create_app()

    async def _run():
        transport = httpx.ASGITransport(app=app)
        api_key = os.getenv("MOTET_API_KEY", "test-key")
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            headers={
                "X-API-Key": api_key,
                "X-Principal-Id": "test-principal",
                "X-Tenant-Id": "test-tenant",
            },
        ) as client:
            # Baseline
            r0 = await client.get(
                "/api/v1/events/stats",
                headers={
                    "X-API-Key": api_key,
                    "X-Principal-Id": "test-principal",
                    "X-Tenant-Id": "test-tenant",
                },
            )
            assert r0.status_code == 200
            base = r0.json()
            # Publish a couple of events (sync API; not awaitable)
            global_bus.publish({"kind": "plan", "names": ["math_eval"]})
            global_bus.publish({"kind": "end"})
            r1 = await client.get(
                "/api/v1/events/stats",
                headers={
                    "X-API-Key": api_key,
                    "X-Principal-Id": "test-principal",
                    "X-Tenant-Id": "test-tenant",
                },
            )
            assert r1.status_code == 200
            after = r1.json()
            assert after["published"] >= base["published"] + 2

    asyncio.run(_run())


