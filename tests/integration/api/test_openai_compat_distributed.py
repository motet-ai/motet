"""
Motet - OpenAI Compatible Facade Distributed Integration Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-28

Description:
    Lane C coverage for the OpenAI-compatible facade (ADR-0125): the facade
    running against real Celery workers instead of in-process command execution.

    This suite exists for the one seam the other facade suites cannot reach.
    When inference happens in a worker process, the assistant's tokens cross a
    process boundary as ADR-0056 encrypted Redis stream frames, and the HTTP
    process must resolve the same tenant key to decrypt them. Nothing about that
    handshake is visible when commands execute in the test's own process, so a
    key or routing mismatch would ship undetected.

    Everything else about the facade is covered by the faster suites:
    test_openai_compat_api.py for the HTTP contract and
    test_openai_compat_worker_e2e.py for real command bodies and translation.

Dependencies:
    - Celery workers from the compose "workers" profile
    - Redis: command routing, worker readiness, encrypted stream frames
    - httpx: ASGI transport client for in-process HTTP calls

Usage:
    cd tests
    docker compose -f docker-compose.test.yml --profile workers up -d worker-1
    docker compose -f docker-compose.test.yml run --rm \\
        test-runner python -m pytest \\
        tests/integration/api/test_openai_compat_distributed.py -v -m distributed

Notes:
    - The distributed marker keeps this out of the default lane B suite
    - Ready Celery workers satisfy the marker gate (no Motet HTTP URL required;
      this suite uses ASGI + workers). Full-stack UI e2e still needs
      MOTET_DISTRIBUTED_STACK_HTTP_URL via the distributed_stack fixture.
    - Workers and the runner must share MOTET_VAULT_MASTER_KEY (set in compose)
    - mock/mock-small keeps the assertions deterministic and credential-free
    - Fixture clears MOTET_OPENAI_COMPAT_DEFAULT_AGENT_ID for hermetic runs
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List

import httpx
import pytest

from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.security.service_accounts import ServiceAccountManager
from motet.interfaces.http import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.distributed,
    pytest.mark.requires_redis,
]

MOCK_MODEL = "mock/mock-small"


@pytest.fixture(autouse=True)
def _async_redis(isolated_async_redis):
    """Stream reads need connections bound to this test's event loop."""


@pytest.fixture(autouse=True)
def ready_workers() -> List[str]:
    """Skip unless Celery workers are actually up to serve the commands."""
    from motet.core.distributed.worker_readiness import WorkerReadinessService

    workers = WorkerReadinessService().get_ready_workers()
    if not workers:
        pytest.skip(
            "No ready Celery workers; start them with "
            "`docker compose -f docker-compose.test.yml --profile workers up -d worker-1`"
        )
    return workers


def _mint_token(*, facade_mode: str | None = None) -> Any:
    manager = ServiceAccountManager(get_sync_redis_client("test_facade_distributed"))
    allowed = [MOCK_MODEL]
    if facade_mode == "agent":
        allowed.append(f"{MOCK_MODEL}:agent")
    return manager.create_service_account(
        name=f"facade-distributed-{uuid.uuid4().hex[:8]}",
        tenant_id=f"tenant-facade-dist-{uuid.uuid4().hex[:12]}",
        motet_id="production",
        roles=["admin", "user", "operator"],
        created_by="test@example.com",
        expires_days=1,
        facade_mode=facade_mode,
        allowed_models=allowed,
    )


@pytest.fixture
def token():
    """Passthrough-capable credential for the distributed passthrough suite."""
    issued = _mint_token(facade_mode="passthrough")
    yield issued
    try:
        ServiceAccountManager(
            get_sync_redis_client("test_facade_distributed")
        ).revoke_service_account(issued)
    except Exception:
        pass


@pytest.fixture
def agent_token():
    """Credential bound to agent mode for the distributed agent assertion."""
    issued = _mint_token(facade_mode="agent")
    yield issued
    try:
        ServiceAccountManager(
            get_sync_redis_client("test_facade_distributed")
        ).revoke_service_account(issued)
    except Exception:
        pass


@pytest.fixture
def app(monkeypatch):
    """A facade-enabled app that dispatches to real workers."""
    env = {
        "MOTET_OPENAI_COMPAT_ENABLED": "true",
        "MOTET_OPENAI_COMPAT_DEFAULT_MODE": "passthrough",
        "MOTET_OPENAI_COMPAT_DEFAULT_ALLOWED_MODELS": MOCK_MODEL,
        # Clear operator .env default agent (e.g. cursor.backend) for hermetic runs.
        "MOTET_OPENAI_COMPAT_DEFAULT_AGENT_ID": "",
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_MODEL_PROVIDER": "mock",
        "MOTET_MODEL_NAME": "mock-small",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return create_app()


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=120,
    )


def auth(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def data_frames(body: str) -> List[Dict[str, Any]]:
    """Parse JSON payloads out of an SSE body, skipping the sentinel."""
    out: List[Dict[str, Any]] = []
    for block in body.split("\n\n"):
        for line in block.strip("\n").split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload and payload != "[DONE]":
                out.append(json.loads(payload))
    return out


async def test_chat_completion_runs_on_a_real_worker(app, token):
    """A completion dispatched to a Celery worker comes back in OpenAI shape."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "distributed hello"}],
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert "distributed hello" in body["choices"][0]["message"]["content"]
    assert body["choices"][0]["finish_reason"] == "stop"


async def test_streamed_tokens_cross_the_process_boundary(app, token):
    """Worker-written encrypted stream frames decrypt in the HTTP process.

    This is the assertion that only holds when both sides resolve the same
    tenant key: the text below exists solely in frames the worker encrypted.
    """
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "distributed stream"}],
                "stream": True,
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert response.text.rstrip().endswith("data: [DONE]")

    chunks = data_frames(response.text)
    text = "".join(
        chunk["choices"][0]["delta"].get("content") or ""
        for chunk in chunks
        if chunk.get("choices")
    )
    assert "distributed stream" in text, f"no decrypted stream text in {text!r}"

    finish_reasons = [
        chunk["choices"][0].get("finish_reason")
        for chunk in chunks
        if chunk.get("choices") and chunk["choices"][0].get("finish_reason")
    ]
    assert finish_reasons == ["stop"]


async def test_responses_endpoint_runs_on_a_real_worker(app, token):
    """The Responses endpoint works over the same distributed path."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/responses",
            json={"model": MOCK_MODEL, "input": "distributed responses"},
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert "distributed responses" in body["output_text"]


async def test_agent_mode_runs_on_a_real_worker(app, agent_token):
    """Agent mode over Celery returns content and turn-aggregated usage."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": f"{MOCK_MODEL}:agent",
                "messages": [{"role": "user", "content": "distributed agent hello"}],
                "motet_agent_id": "core.default",
            },
            headers=auth(agent_token),
        )

    assert response.status_code == 200, response.text
    assert response.headers.get("X-Motet-Facade-Mode") == "agent"
    body = response.json()
    assert body["object"] == "chat.completion"
    assert (body["choices"][0]["message"]["content"] or "").strip()
    assert body["usage"]["total_tokens"] > 0, body["usage"]
