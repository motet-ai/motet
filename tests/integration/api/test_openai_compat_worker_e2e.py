"""
Motet - OpenAI Compatible Facade End-to-End Integration Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
    End-to-end coverage for the OpenAI-compatible facade (ADR-0125) with real
    command execution behind it.

    Unlike the contract suite, these tests let the facade actually run its
    distributed commands: ``model_inference`` and ``model_stream`` execute for
    real against the deterministic mock adapter, streaming tokens through real
    encrypted Redis stream frames that the facade then decrypts and reframes as
    Server-Sent Events. That covers the seam most likely to break in production:
    canonical command results and Redis stream envelopes being translated into
    the OpenAI wire format a third-party client expects.

    Commands run through an in-process invoker rather than Celery, following the
    pattern established by test_artifact_rag_e2e.py. This keeps real command
    bodies, real Redis, real encryption, and real translation in the path while
    removing broker routing, which is not a facade concern and is covered
    elsewhere.

    Hosted-tools allowlist coverage forces one canned tool call from the mock
    adapter (it never emits tools on its own) and runs a real allowlisted
    builtin through ``core.agent_loop`` → ``tool_execution`` so schema → execute →
    observation → second model turn is proven on the OpenAI wire.

Dependencies:
    - httpx: ASGI transport client for in-process HTTP calls
    - motet.interfaces.http.create_app: FastAPI application under test
    - motet.core.models.adapters.providers.mock: deterministic model responses
    - motet.core.tools.registry: real builtin registry for hosted tool execution
    - Redis: command stream frames, service accounts, session mappings

Usage:
    docker-compose -f tests/docker-compose.test.yml run --rm test-runner \\
        python -m pytest tests/integration/api/test_openai_compat_worker_e2e.py -v

Notes:
    - mock/mock-small needs no provider credentials and echoes "You said: ..."
    - Each test uses a fresh tenant so tenant data keys never collide
    - The invoker records dispatched commands so tests can assert on routing
    - Hosted-tools tests clear the process-local worker tool listing cache
"""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from motet.core.config import Config
from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.security.service_accounts import ServiceAccountManager
from motet.core.tools.registry import registry as tool_registry
from motet.core.types import StopEvent, StopReason, ToolCallCompleteEvent
from motet.interfaces.api.openai_compat import execution as openai_compat_execution
from motet.interfaces.http import create_app

pytestmark = [pytest.mark.integration, pytest.mark.requires_redis]

MOCK_MODEL = "mock/mock-small"
TEST_MASTER_KEY = "test-openai-compat-master-key"
HOSTED_TOOL = "core.tools_list"


# ---------------------------------------------------------------------------
# In-process command execution
# ---------------------------------------------------------------------------


class InProcessInvoker:
    """Execute distributed commands in this process, recording what ran.

    Model commands only need Redis. Hosted-tools paths also need a stack with
    the builtin tool registry so ``tool_list`` / ``tool_execution`` can run
    for real (same registry workers use after builtin registration).

    ``motet.join`` / ``motet.dispatch`` are executed in-process: this invoker
    runs gather/dispatch children directly instead of waiting on Celery
    completion events that never arrive without a worker.
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
        stack = SimpleNamespace(
            config=Config(),
            tool_registry=tool_registry,
            memory=None,
            memory_manager=None,
        )
        return {
            "redis": self._redis,
            "distributed_invoker": self,
            "stack": stack,
            "tool_registry": tool_registry,
        }

    def data_for(self, command_type: str) -> List[Any]:
        """Return the payloads of every recorded command of one type."""
        return [
            command.data
            for command in self.commands
            if command.get_command_type() == command_type
        ]

    def types(self) -> List[str]:
        return [command.get_command_type() for command in self.commands]


@pytest.fixture(autouse=True)
def _async_redis(isolated_async_redis):
    """Stream reads and session writes need connections on this test's loop."""


@pytest.fixture
def invoker(monkeypatch) -> InProcessInvoker:
    """Route facade command execution into this process."""
    client = get_sync_redis_client("test_openai_compat_e2e")
    in_process = InProcessInvoker(client)
    monkeypatch.setattr(
        "motet.core.workers.global_invoker.execute_command",
        in_process.execute_command,
    )
    return in_process


@pytest.fixture
def tenant_id() -> str:
    """A fresh tenant per test so encryption keys and streams never collide."""
    return f"tenant-openai-compat-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def token(tenant_id: str) -> Any:
    """A real service account token scoped to this test's tenant."""
    manager = ServiceAccountManager(get_sync_redis_client("test_openai_compat_e2e_sa"))
    issued = manager.create_service_account(
        name=f"facade-e2e-{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        motet_id="production",
        roles=["admin", "user"],
        created_by="test@example.com",
        expires_days=1,
        allowed_models=[MOCK_MODEL],
    )
    yield issued
    try:
        manager.revoke_service_account(issued)
    except Exception:
        pass


@pytest.fixture
def app(monkeypatch, invoker):
    """A facade-enabled app wired to the deterministic mock model."""
    env = {
        "MOTET_OPENAI_COMPAT_ENABLED": "true",
        "MOTET_OPENAI_COMPAT_DEFAULT_ALLOWED_MODELS": MOCK_MODEL,
        # Clear operator .env default agent (e.g. cursor.backend) for hermetic runs.
        "MOTET_OPENAI_COMPAT_DEFAULT_AGENT_ID": "",
        "MOTET_MODEL_PROVIDER": "mock",
        "MOTET_MODEL_NAME": "mock-small",
        "MOTET_VAULT_MASTER_KEY": TEST_MASTER_KEY,
        "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
        "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return create_app()


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=60,
    )


def auth(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def parse_sse(body: str) -> List[Tuple[Optional[str], str]]:
    """Parse an SSE body into (event name, data) pairs, ignoring comments."""
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
    """Parse SSE data payloads as JSON, skipping the terminating sentinel."""
    out: List[Dict[str, Any]] = []
    for _event, data in parse_sse(body):
        if data == "[DONE]":
            continue
        out.append(json.loads(data))
    return out


def assert_terminated(body: str) -> None:
    assert body.rstrip().endswith("data: [DONE]"), f"stream not terminated: {body[-200:]}"


# ---------------------------------------------------------------------------
# Chat Completions
# ---------------------------------------------------------------------------


async def test_chat_completion_returns_model_output(app, token, invoker):
    """A non-streaming completion carries real model output in OpenAI shape."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "hello facade"}],
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == MOCK_MODEL
    assert body["id"].startswith("chatcmpl-")

    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    # The mock adapter echoes the user turn, proving the message survived
    # translation into canonical form and back.
    assert "hello facade" in choice["message"]["content"]

    assert set(body["usage"]) >= {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert invoker.data_for("core.model_inference"), "inference command never ran"


async def test_chat_completion_sends_resolved_model_and_settings(app, token, invoker):
    """The resolved registry entry and sampling params reach the command."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "settings"}],
                "temperature": 0.25,
                "max_tokens": 128,
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    settings = invoker.data_for("core.model_inference")[0].model_settings
    assert settings["provider"] == "mock"
    assert settings["model_name"] == "mock-small"
    assert settings["temperature"] == 0.25
    assert settings["max_tokens"] == 128


async def test_chat_completion_carries_correlation_headers(app, token):
    """Successful responses expose the ids an operator needs to trace a call."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": MOCK_MODEL, "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert response.headers["X-Motet-Facade-Mode"] == "passthrough"
    assert response.headers["X-Motet-Model"] == MOCK_MODEL
    assert response.headers["X-Motet-Task-Id"]
    assert response.headers["X-Motet-Conversation-Id"].startswith("openai-")


async def test_chat_completion_streams_chunks(app, token, invoker):
    """Streaming emits an opening role delta, text deltas, then a finish chunk."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "stream please"}],
                "stream": True,
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert_terminated(response.text)

    chunks = json_frames(response.text)
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    ids = {chunk["id"] for chunk in chunks}
    assert len(ids) == 1, "every chunk in one stream shares a completion id"

    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"

    text = "".join(
        chunk["choices"][0]["delta"].get("content") or ""
        for chunk in chunks
        if chunk["choices"]
    )
    assert "stream please" in text, f"no streamed text recovered from {text!r}"

    finish_reasons = [
        chunk["choices"][0].get("finish_reason")
        for chunk in chunks
        if chunk["choices"] and chunk["choices"][0].get("finish_reason")
    ]
    assert finish_reasons == ["stop"]
    assert invoker.data_for("core.model_stream"), "stream command never ran"


async def test_chat_completion_stream_omits_usage_by_default(app, token):
    """Usage chunks appear only when the client opts in, as OpenAI does."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "no usage"}],
                "stream": True,
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert all(chunk.get("usage") is None for chunk in json_frames(response.text))


async def test_chat_completion_stream_includes_usage_when_requested(app, token):
    """stream_options.include_usage adds a final usage-bearing chunk."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "with usage"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    chunks = json_frames(response.text)
    usage_chunks = [chunk for chunk in chunks if chunk.get("usage")]
    assert len(usage_chunks) == 1
    usage = usage_chunks[0]["usage"]
    assert set(usage) >= {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert usage_chunks[0] is chunks[-1], "usage must be the last data frame"


# ---------------------------------------------------------------------------
# Responses API
# ---------------------------------------------------------------------------


async def test_responses_returns_output_text(app, token):
    """The Responses endpoint returns a completed response with output text."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/responses",
            json={"model": MOCK_MODEL, "input": "responses hello"},
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["id"].startswith("resp_")
    assert "responses hello" in body["output_text"]

    message = body["output"][0]
    assert message["type"] == "message"
    assert message["role"] == "assistant"
    assert message["content"][0]["type"] == "output_text"
    assert "responses hello" in message["content"][0]["text"]


async def test_responses_input_message_list_is_accepted(app, token):
    """Cursor-shaped bodies posting a structured input list are understood."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/responses",
            json={
                "model": MOCK_MODEL,
                "instructions": "You are terse.",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "structured input"}],
                    }
                ],
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert "structured input" in response.json()["output_text"]


async def test_previous_response_id_continues_the_conversation(app, token):
    """Chaining turns by previous_response_id keeps one Motet conversation."""
    async with client_for(app) as client:
        first = await client.post(
            "/v1/responses",
            json={"model": MOCK_MODEL, "input": "first turn"},
            headers=auth(token),
        )
        assert first.status_code == 200, first.text

        second = await client.post(
            "/v1/responses",
            json={
                "model": MOCK_MODEL,
                "input": "second turn",
                "previous_response_id": first.json()["id"],
            },
            headers=auth(token),
        )

    assert second.status_code == 200, second.text
    assert (
        second.headers["X-Motet-Conversation-Id"]
        == first.headers["X-Motet-Conversation-Id"]
    )


async def test_unrelated_requests_get_separate_conversations(app, token):
    """Without a session hint, two calls must not share conversation memory."""
    async with client_for(app) as client:
        first = await client.post(
            "/v1/responses",
            json={"model": MOCK_MODEL, "input": "alpha"},
            headers=auth(token),
        )
        second = await client.post(
            "/v1/responses",
            json={"model": MOCK_MODEL, "input": "beta"},
            headers=auth(token),
        )

    assert first.status_code == 200 and second.status_code == 200
    assert (
        first.headers["X-Motet-Conversation-Id"]
        != second.headers["X-Motet-Conversation-Id"]
    )


async def test_responses_stream_emits_lifecycle_events(app, token):
    """The Responses stream follows the documented event progression."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/responses",
            json={"model": MOCK_MODEL, "input": "stream responses", "stream": True},
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert_terminated(response.text)

    frames = parse_sse(response.text)
    names = [event for event, _data in frames if event]
    for expected in (
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ):
        assert expected in names, f"missing {expected} in {names}"
    assert names.index("response.created") == 0
    assert names[-1] == "response.completed"

    payloads = json_frames(response.text)
    assert [frame["sequence_number"] for frame in payloads] == list(
        range(len(payloads))
    ), "sequence numbers must be contiguous and ordered"

    deltas = "".join(
        frame.get("delta", "")
        for frame in payloads
        if frame.get("type") == "response.output_text.delta"
    )
    assert "stream responses" in deltas

    completed = payloads[-1]["response"]
    assert completed["status"] == "completed"
    assert "stream responses" in completed["output_text"]


async def test_streamed_response_id_is_resumable(app, token):
    """A streamed response records its session mapping like a buffered one."""
    async with client_for(app) as client:
        streamed = await client.post(
            "/v1/responses",
            json={"model": MOCK_MODEL, "input": "streamed turn", "stream": True},
            headers=auth(token),
        )
        assert streamed.status_code == 200, streamed.text
        response_id = json_frames(streamed.text)[-1]["response"]["id"]

        follow_up = await client.post(
            "/v1/responses",
            json={
                "model": MOCK_MODEL,
                "input": "follow up",
                "previous_response_id": response_id,
            },
            headers=auth(token),
        )

    assert follow_up.status_code == 200, follow_up.text
    assert (
        follow_up.headers["X-Motet-Conversation-Id"]
        == streamed.headers["X-Motet-Conversation-Id"]
    )


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


async def test_hosted_tools_mode_runs_without_exposing_tools(monkeypatch, app, token):
    """With no tool allowlist, hosted_tools grants nothing and still answers.

    This is the deny-by-default posture: enabling the deeper mode must not by
    itself expose a single Motet tool to a third-party client.
    """
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_ALLOW_REQUEST_MODE_OVERRIDE", "true")
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "hosted_tools")
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_HOSTED_TOOLS_ALLOWLIST", "")

    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "hosted mode"}],
                "motet_mode": "hosted_tools",
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert response.headers["X-Motet-Facade-Mode"] == "hosted_tools"
    assert "hosted mode" in response.json()["choices"][0]["message"]["content"]


async def test_hosted_tools_mode_streams(monkeypatch, app, token):
    """The hosted-tools loop streams text like passthrough when no tool fires."""
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_ALLOW_REQUEST_MODE_OVERRIDE", "true")
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "hosted_tools")

    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "hosted stream"}],
                "motet_mode": "hosted_tools",
                "stream": True,
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert_terminated(response.text)
    chunks = json_frames(response.text)
    text = "".join(
        chunk["choices"][0]["delta"].get("content") or ""
        for chunk in chunks
        if chunk["choices"]
    )
    assert "hosted stream" in text


async def test_hosted_tools_allowlist_executes_and_resumes(
    monkeypatch, app, token, invoker
):
    """Allowlisted Motet tools run server-side, then the model turn continues.

    The mock adapter never emits tool calls, so the first ``stream`` is
    patched to request ``core.tools_list`` when that schema is advertised.
    Everything else — core.agent_loop, tool_execution, observation append, second
    model_stream, OpenAI wire — stays real.
    """
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_ALLOW_REQUEST_MODE_OVERRIDE", "true")
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "hosted_tools")
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_HOSTED_TOOLS_ALLOWLIST", HOSTED_TOOL)
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_MAX_TOOL_ITERATIONS", "4")

    openai_compat_execution._tool_cache["expires_at"] = 0.0
    openai_compat_execution._tool_cache["tools"] = []

    from motet.core.models.adapters.providers import mock as mock_adapter_module

    original_stream = mock_adapter_module.MockAdapter.stream
    call_count = {"n": 0}

    def _stream_with_hosted_tool(self, request):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        tool_names = {
            (getattr(tool, "name", None) or "").replace("__", ".", 2)
            for tool in (request.tools or [])
        }
        if call_count["n"] == 1 and HOSTED_TOOL in tool_names:
            yield ToolCallCompleteEvent(
                call_id="call_hosted_tools_list",
                tool_name=HOSTED_TOOL,
                arguments_json="{}",
            )
            yield StopEvent(reason=StopReason.TOOL_CALLS)
            return
        yield from original_stream(self, request)

    monkeypatch.setattr(
        mock_adapter_module.MockAdapter, "stream", _stream_with_hosted_tool
    )

    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "list available tools"}],
                "motet_mode": "hosted_tools",
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert response.headers["X-Motet-Facade-Mode"] == "hosted_tools"

    body = response.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"].get("tool_calls") in (None, [])
    assert "list available tools" in (choice["message"].get("content") or "")

    command_types = invoker.types()
    assert "core.tool_list" in command_types
    assert "core.agent_loop" in command_types
    assert "core.tool_execution" in command_types
    assert command_types.count("core.model_stream") >= 2

    executions = invoker.data_for("core.tool_execution")
    assert executions, "allowlisted tool never reached tool_execution"
    assert executions[0].tool_name == HOSTED_TOOL
    assert call_count["n"] >= 2, "model did not resume after the tool observation"


async def test_chat_completion_opt_in_thinking_non_stream(app, token, invoker):
    """Client opt-in + CAP_REASONING surfaces reasoning_content on the wire."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "think hard"}],
                "reasoning_effort": "medium",
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    settings = invoker.data_for("core.model_inference")[0].model_settings
    assert settings.get("enable_thinking") is True
    message = response.json()["choices"][0]["message"]
    assert "think hard" in (message.get("content") or "")
    assert "think about this" in (message.get("reasoning_content") or "").lower()


async def test_chat_completion_without_opt_in_omits_thinking(app, token, invoker):
    """Default collapse: no enable_thinking and no reasoning_content on wire."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "no think"}],
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    settings = invoker.data_for("core.model_inference")[0].model_settings
    assert not settings.get("enable_thinking")
    message = response.json()["choices"][0]["message"]
    assert "reasoning_content" not in message


async def test_chat_completion_streams_reasoning_content(app, token, invoker):
    """Streaming opt-in emits reasoning_content deltas before assistant text."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MOCK_MODEL,
                "messages": [{"role": "user", "content": "stream think"}],
                "stream": True,
                "motet_enable_thinking": True,
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert_terminated(response.text)
    settings = invoker.data_for("core.model_stream")[0].model_settings
    assert settings.get("enable_thinking") is True

    chunks = json_frames(response.text)
    reasoning = "".join(
        chunk["choices"][0]["delta"].get("reasoning_content") or ""
        for chunk in chunks
        if chunk.get("choices")
    )
    content = "".join(
        chunk["choices"][0]["delta"].get("content") or ""
        for chunk in chunks
        if chunk.get("choices")
    )
    assert "think about this" in reasoning.lower()
    assert "stream think" in content


async def test_responses_stream_emits_reasoning_item(app, token, invoker):
    """Responses stream places a reasoning item ahead of the message item."""
    async with client_for(app) as client:
        response = await client.post(
            "/v1/responses",
            json={
                "model": MOCK_MODEL,
                "input": "responses think",
                "stream": True,
                "reasoning": {"effort": "low"},
            },
            headers=auth(token),
        )

    assert response.status_code == 200, response.text
    assert_terminated(response.text)
    settings = invoker.data_for("core.model_stream")[0].model_settings
    assert settings.get("enable_thinking") is True
    assert settings.get("reasoning_effort") == "low"

    names = [
        line[len("event: ") :]
        for line in response.text.splitlines()
        if line.startswith("event: ")
    ]
    assert "response.reasoning_summary_text.delta" in names
    assert names.index("response.reasoning_summary_text.delta") < names.index(
        "response.output_text.delta"
    )

    completed = next(
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ") and '"response.completed"' in line
    )
    output_types = [item["type"] for item in completed["response"]["output"]]
    assert output_types[0] == "reasoning"
    assert "message" in output_types
    summary = completed["response"]["output"][0]["summary"][0]["text"]
    assert "think about this" in summary.lower()
