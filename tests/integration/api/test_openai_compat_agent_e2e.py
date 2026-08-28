"""
Motet - OpenAI Compatible Facade Agent-Mode End-to-End Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    End-to-end coverage for OpenAI-compat ``agent`` mode (ADR-0125 §5c Phase 3).

    Passthrough and hosted_tools are covered by test_openai_compat_worker_e2e.py.
    Agent mode is a different path: the facade builds a MotetStack and runs
    ``core.agent_turn``, which nests model_stream / prepare_context / finalize_turn
    and emits a terminal ``end`` event carrying turn-aggregated usage. These tests
    exercise that seam with an in-process invoker (same pattern as
    test_artifact_rag_e2e.py) so nested ``motet.do`` calls stay in-process while
    Redis stream frames and OpenAI wire translation remain real.

Dependencies:
    - httpx: ASGI transport client
    - motet.interfaces.http.create_app: FastAPI app under test
    - Redis: task streams, service accounts, session mappings
    - mock adapter: deterministic tokens + usage events

Usage:
    docker compose -f tests/docker-compose.test.yml run --rm test-runner \\
        python -m pytest tests/integration/api/test_openai_compat_agent_e2e.py -v

Notes:
    - Service accounts are minted with facade_mode=agent so mode selection does
      not depend on request overrides
    - Assertions require non-zero usage to prove the agent_turn → end → facade
      plumbing (ADR-0125 v1.3.0) is live on this path
    - Fixture clears MOTET_OPENAI_COMPAT_DEFAULT_AGENT_ID so operator .env
      (e.g. cursor.backend) cannot divert alias / omit-agent_id paths
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.security.service_accounts import ServiceAccountManager
from motet.interfaces.http import create_app

pytestmark = [pytest.mark.integration, pytest.mark.requires_redis]

MOCK_MODEL = "mock/mock-small"
TEST_MASTER_KEY = "test-openai-compat-agent-master-key"


class NestedInProcessInvoker:
    """Execute agent_turn and its nested commands in this process.

    Nested ``motet.do`` / ``motet.join`` resolve the invoker via
    ``get_distributed_invoker()``, which falls back to the patched
    ``global_invoker.execute_command``. Gather and dispatch children run
    in-process so the suite does not wait on Celery completion events.
    """

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client
        self.commands: List[Any] = []

    def execute_command(
        self,
        command: Any,
        target_worker_id: Optional[str] = None,
        strategy_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.commands.append(command)
        command_type = command.get_command_type()
        if command_type == "core.dispatch":
            children = command._deserialize_child_commands(self._worker_context())
            dispatched: List[str] = []
            for child in children:
                dispatched.append(child.command_id)
                self.execute_command(child)
            return {
                "status": "completed",
                "result": {
                    "status": "success",
                    "data": {
                        "dispatched": dispatched,
                        "total_commands": len(dispatched),
                    },
                },
            }
        if command_type == "core.gather":
            children = command._deserialize_child_commands(self._worker_context())
            child_envelopes: List[Any] = []
            for child in children:
                transport = self.execute_command(child)
                envelope = (
                    transport.get("result") if isinstance(transport, dict) else transport
                )
                child_envelopes.append(envelope)
            return {
                "status": "completed",
                "result": command._create_success_response(
                    data={
                        "results": child_envelopes,
                        "total_commands": len(children),
                        "successful": len(children),
                        "failed": 0,
                        "aggregation_strategy": "all_results",
                    },
                    execution_time_ms=0.0,
                ),
            }

        result = command._do_execute(self._worker_context())
        return {"status": "completed", "result": result}

    def _worker_context(self) -> Dict[str, Any]:
        return {
            "redis": self._redis,
            "distributed_invoker": self,
        }

    def types(self) -> List[str]:
        return [command.get_command_type() for command in self.commands]


@pytest.fixture(autouse=True)
def _async_redis(isolated_async_redis):
    """Agent stream reads and session writes need this test's event loop."""


@pytest.fixture
def invoker(monkeypatch) -> NestedInProcessInvoker:
    client = get_sync_redis_client("test_openai_compat_agent_e2e")
    in_process = NestedInProcessInvoker(client)
    monkeypatch.setattr(
        "motet.core.workers.global_invoker.execute_command",
        in_process.execute_command,
    )
    return in_process


@pytest.fixture
def tenant_id() -> str:
    return f"tenant-openai-agent-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def token(tenant_id: str) -> Any:
    """Service account bound to agent mode for the mock model."""
    manager = ServiceAccountManager(get_sync_redis_client("test_openai_compat_agent_sa"))
    issued = manager.create_service_account(
        name=f"facade-agent-{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        motet_id="production",
        roles=["admin", "user", "operator"],
        created_by="test@example.com",
        expires_days=1,
        facade_mode="agent",
        allowed_models=[MOCK_MODEL, f"{MOCK_MODEL}:agent"],
    )
    yield issued
    try:
        manager.revoke_service_account(issued)
    except Exception:
        pass


@pytest.fixture
def app(monkeypatch, invoker):
    env = {
        "MOTET_OPENAI_COMPAT_ENABLED": "true",
        "MOTET_OPENAI_COMPAT_DEFAULT_MODE": "passthrough",
        # Empty: do not inherit operator .env (e.g. cursor.backend) into hermetic
        # facade tests — alias/omit-motet_agent_id paths must resolve to core.default.
        "MOTET_OPENAI_COMPAT_DEFAULT_AGENT_ID": "",
        "MOTET_OPENAI_COMPAT_DEFAULT_ALLOWED_MODELS": MOCK_MODEL,
        "MOTET_MODEL_PROVIDER": "mock",
        "MOTET_MODEL_NAME": "mock-small",
        "MOTET_VAULT_MASTER_KEY": TEST_MASTER_KEY,
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        "MOTET_ENABLE_MODEL_PROFILES": "false",
        "MOTET_MEMORY_BACKEND": "redis",
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


def parse_sse(body: str) -> List[Tuple[Optional[str], str]]:
    frames: List[Tuple[Optional[str], str]] = []
    for block in body.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        event: Optional[str] = None
        data_lines: List[str] = []
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            frames.append((event, "\n".join(data_lines)))
    return frames


def json_frames(body: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for _event, data in parse_sse(body):
        if data == "[DONE]":
            continue
        out.append(json.loads(data))
    return out


async def test_agent_mode_chat_completion_runs_agent_turn(app, token, invoker):
    """Agent-mode completions go through agent_turn and return OpenAI shape."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "hello agent facade"}],
                "motet_agent_id": "core.default",
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert response.headers.get("X-Motet-Facade-Mode") == "agent"

    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    content = body["choices"][0]["message"]["content"] or ""
    assert content.strip(), "agent mode returned empty assistant content"

    types = invoker.types()
    assert "core.agent_turn" in types, f"agent_turn never ran; saw {types}"


async def test_agent_mode_reports_aggregated_usage(app, token, invoker):
    """Turn-aggregated usage from agent_turn reaches the OpenAI usage block."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": f"{MOCK_MODEL}:agent",
                "messages": [{"role": "user", "content": "count my tokens"}],
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    usage = body["usage"]
    assert usage["prompt_tokens"] > 0, usage
    assert usage["completion_tokens"] > 0, usage
    assert usage["total_tokens"] >= usage["prompt_tokens"], usage


async def test_agent_mode_streams_and_includes_usage(app, token):
    """Streaming agent mode emits deltas and a final usage-bearing chunk."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "stream agent please"}],
                "stream": True,
                "stream_options": {"include_usage": True},
                "motet_agent_id": "core.default",
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert response.headers.get("X-Motet-Facade-Mode") == "agent"
    body = response.text
    assert body.rstrip().endswith("data: [DONE]"), body[-200:]

    frames = json_frames(body)
    assert frames, "no SSE frames"
    deltas = [
        (frame.get("choices") or [{}])[0].get("delta", {}).get("content")
        for frame in frames
        if frame.get("choices")
    ]
    assert any(deltas), "stream produced no text deltas"

    usage_frames = [frame for frame in frames if frame.get("usage")]
    assert usage_frames, "include_usage requested but no usage chunk arrived"
    usage = usage_frames[-1]["usage"]
    assert usage["total_tokens"] > 0, usage


async def test_agent_mode_previous_response_id_continues_conversation(app, token):
    """Agent mode reuses Motet conversation_id via previous_response_id."""
    async with client_for(app) as client:
        first = await client.post(
            "/v1/responses",
            json={
                "model": MOCK_MODEL,
                "input": "remember the marker agent-chain-alpha",
                "motet_agent_id": "core.default",
            },
            headers=auth(token),
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        response_id = first_body["id"]
        assert first.headers.get("X-Motet-Conversation-Id")

        second = await client.post(
            "/v1/responses",
            json={
                "model": MOCK_MODEL,
                "input": "what marker did I mention?",
                "previous_response_id": response_id,
                "motet_agent_id": "core.default",
            },
            headers=auth(token),
        )

    assert second.status_code == 200, second.text
    assert second.headers.get("X-Motet-Facade-Mode") == "agent"
    assert (
        second.headers.get("X-Motet-Conversation-Id")
        == first.headers.get("X-Motet-Conversation-Id")
    )
    assert second.json().get("usage", {}).get("total_tokens", 0) > 0


async def test_agent_mode_alias_selects_agent_without_request_override(app, token, invoker):
    """Model alias ``:agent`` selects agent mode under the credential ceiling."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": f"{MOCK_MODEL}:agent",
                "messages": [{"role": "user", "content": "alias path"}],
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert response.headers.get("X-Motet-Facade-Mode") == "agent"
    assert "core.agent_turn" in invoker.types()
