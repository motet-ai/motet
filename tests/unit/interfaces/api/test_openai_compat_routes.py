"""
Motet - OpenAI Compatible Route Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-30

Description:
    Unit tests for the OpenAI-compatible facade routes (ADR-0125): model listing
    under the credential allowlist, chat completions in streaming and
    non-streaming form, the Responses endpoint, mode selection precedence and
    ceilings, OpenAI-shaped error envelopes, correlation headers, rejection of
    header-derived dev identities (§11e), the 404 on unknown
    previous_response_id, and the agent-mode conversation-ownership pre-flight
    guard (§11f).

    Execution backends are stubbed so these tests cover the HTTP contract only;
    mode behavior against real commands is exercised by integration tests.

Dependencies:
    - pytest / fastapi.testclient: route exercising
    - motet.interfaces.api.openai_compat: system under test

Usage:
    pytest tests/unit/interfaces/api/test_openai_compat_routes.py

Notes:
    - The router is mounted on a bare app so the test does not depend on the
      facade being enabled in the ambient environment
"""

import json
from typing import Any, AsyncGenerator, Dict, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from motet.core.types import Principal
from motet.interfaces.api.openai_compat import execution, routes, sessions
from motet.interfaces.api.shared.auth import get_current_principal

PRINCIPAL = Principal(
    id="service-account:facade",
    roles=["member"],
    tenant_id="test-tenant",
    motet_id="test-motet",
    claims={"type": "service_account", "name": "facade"},
)

HEADER_PRINCIPAL = Principal(
    id="dev-user",
    roles=["member"],
    tenant_id="test-tenant",
    motet_id="test-motet",
    claims={"type": "header", "source": "dev_mode"},
)


@pytest.fixture(autouse=True)
def facade_env(monkeypatch):
    """Allowlist a real registry model and keep request overrides off by default."""
    monkeypatch.setenv(
        "MOTET_OPENAI_COMPAT_DEFAULT_ALLOWED_MODELS",
        "openai/gpt-4o-mini,mock/mock-small",
    )
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "passthrough")
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_ALLOW_REQUEST_MODE_OVERRIDE", "false")
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_STREAM_KEEPALIVE_SECONDS", "0")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    app.dependency_overrides[get_current_principal] = lambda: PRINCIPAL
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def header_client():
    """Client whose principal came from insecure X-Principal-Id headers."""
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    app.dependency_overrides[get_current_principal] = lambda: HEADER_PRINCIPAL
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def stub_run(monkeypatch):
    """Capture the resolved context and return a canned inference result."""
    captured: Dict[str, Any] = {}

    async def _run(ctx: execution.FacadeContext) -> Dict[str, Any]:
        captured["ctx"] = ctx
        return {
            "content": "hello there",
            "finish_reason": "stop",
            "prompt_tokens": 11,
            "completion_tokens": 3,
            "total_tokens": 14,
        }

    monkeypatch.setattr(execution, "run", _run)
    return captured


@pytest.fixture
def stub_stream(monkeypatch):
    """Stream two deltas then a result."""

    def _stream(ctx: execution.FacadeContext) -> AsyncGenerator[Tuple[str, Any], None]:
        async def _gen():
            yield ("delta", "hel")
            yield ("delta", "lo")
            yield (
                "result",
                {
                    "content": "hello",
                    "finish_reason": "stop",
                    "prompt_tokens": 5,
                    "completion_tokens": 1,
                    "total_tokens": 6,
                },
            )

        return _gen()

    monkeypatch.setattr(execution, "stream", _stream)


@pytest.fixture(autouse=True)
def no_session_writes(monkeypatch):
    """Session correlation needs Redis; unit tests only assert it is attempted."""
    recorded: List[Tuple[str, str]] = []

    async def _remember(response_id, conversation_id, principal, cfg):
        recorded.append((response_id, conversation_id))

    monkeypatch.setattr(sessions, "remember_response", _remember)
    return recorded


@pytest.fixture(autouse=True)
def no_transcript_store(monkeypatch):
    """Transcript fingerprinting needs Redis; capture calls instead (§5d)."""
    recorded: List[Dict[str, Any]] = []

    async def _remember(messages, result, conversation_id, principal, cfg):
        recorded.append(
            {
                "conversation_id": conversation_id,
                "message_count": len(messages),
                "content": result.get("content"),
            }
        )

    async def _infer(messages, principal, cfg):
        return None

    monkeypatch.setattr(sessions, "remember_transcript", _remember)
    monkeypatch.setattr(sessions, "infer_conversation_from_transcript", _infer)
    return recorded


def sse_events(body: str) -> List[Dict[str, Any]]:
    """Parse SSE data frames, skipping comments and the terminal sentinel."""
    events = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


class TestModels:
    """Model listing reflects the credential allowlist."""

    def test_list_models_is_filtered(self, client):
        response = client.get("/v1/models")
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "list"
        ids = [m["id"] for m in body["data"]]
        assert ids == ["mock/mock-small", "openai/gpt-4o-mini"] or set(ids) == {
            "mock/mock-small",
            "openai/gpt-4o-mini",
        }
        by_id = {m["id"]: m for m in body["data"]}
        assert by_id["openai/gpt-4o-mini"]["owned_by"] == "openai"

    def test_retrieve_allowlisted_model(self, client):
        response = client.get("/v1/models/openai/gpt-4o-mini")
        assert response.status_code == 200
        assert response.json()["id"] == "openai/gpt-4o-mini"

    def test_retrieve_denied_model_is_404(self, client):
        response = client.get("/v1/models/anthropic/claude-sonnet-4")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "model_not_found"


class TestInsecurePrincipalRejection:
    """Header-derived dev identities are refused on facade routes (§11e)."""

    def test_chat_completions_rejects_header_principal(self, header_client, stub_run):
        response = header_client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 401
        error = response.json()["error"]
        assert error["type"] == "authentication_error"
        assert error["code"] == "insecure_auth_rejected"
        assert "ctx" not in stub_run, "request must be rejected before execution"

    def test_models_rejects_header_principal(self, header_client):
        response = header_client.get("/v1/models")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "insecure_auth_rejected"

    def test_retrieve_model_rejects_header_principal(self, header_client):
        response = header_client.get("/v1/models/openai/gpt-4o-mini")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "insecure_auth_rejected"


class TestChatCompletions:
    """Non-streaming chat completions."""

    def test_happy_path_shape(self, client, stub_run):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["model"] == "openai/gpt-4o-mini"
        assert body["choices"][0]["message"]["content"] == "hello there"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["usage"]["total_tokens"] == 14
        assert body["id"].startswith("chatcmpl-")

    def test_completion_id_is_recorded_for_chaining(
        self, client, stub_run, no_session_writes
    ):
        """A chatcmpl id must resolve later as previous_response_id (§5d step 3)."""
        response = client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        body = response.json()
        assert no_session_writes and no_session_writes[0][0] == body["id"]
        assert no_session_writes[0][1] == stub_run["ctx"].conversation_id

    def test_correlation_headers_present(self, client, stub_run):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.headers["X-Motet-Facade-Mode"] == "passthrough"
        assert response.headers["X-Motet-Model"] == "openai/gpt-4o-mini"
        assert response.headers["X-Motet-Task-Id"]
        assert response.headers["X-Motet-Conversation-Id"]

    def test_responses_shaped_body_accepted(self, client, stub_run):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-4o-mini", "input": "hi from cursor"},
        )
        assert response.status_code == 200
        ctx = stub_run["ctx"]
        assert ctx.messages[0].content == "hi from cursor"

    def test_denied_model_is_openai_error_envelope(self, client):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "anthropic/claude-sonnet-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["type"] == "not_found_error"
        assert error["code"] == "model_not_found"

    def test_unsupported_parameter_rejected(self, client):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "n": 3,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["param"] == "n"

    def test_conversation_id_from_openai_conversation_field(self, client, stub_run):
        client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "conversation": "conv_abc",
            },
        )
        assert stub_run["ctx"].conversation_id == "conv_abc"

    def test_conversation_and_previous_response_are_mutually_exclusive(self, client):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "conversation": "conv_abc",
                "previous_response_id": "resp_1",
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_session_reference"

    def test_unknown_previous_response_id_is_404(self, client, stub_run, monkeypatch):
        async def _lookup(response_id, principal):
            return None

        monkeypatch.setattr(sessions, "lookup_response_conversation", _lookup)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "previous_response_id": "resp_gone",
            },
        )
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "response_not_found"
        assert error["param"] == "previous_response_id"
        assert "ctx" not in stub_run, "an expired chain must not silently start fresh"


class TestConversationOwnershipGuard:
    """Agent mode pre-flight ownership check (§11f, issue #139)."""

    def test_foreign_conversation_is_404_before_dispatch(self, client, stub_run, monkeypatch):
        from motet.core.conversations.ownership import ConversationAccessDenied

        monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "agent")

        def _deny(**kwargs):
            raise ConversationAccessDenied(conversation_id=kwargs.get("conversation_id"))

        monkeypatch.setattr(sessions, "require_not_owned_by_other_sync", _deny)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "conversation": "conv_theirs",
            },
        )
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "invalid_session_reference"
        assert "ctx" not in stub_run, "cross-principal ids must fail before execution"

    def test_owned_or_unclaimed_conversation_proceeds(self, client, stub_run, monkeypatch):
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "agent")
        monkeypatch.setattr(sessions, "require_not_owned_by_other_sync", lambda **kwargs: None)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "conversation": "conv_mine",
            },
        )
        assert response.status_code == 200
        assert stub_run["ctx"].conversation_id == "conv_mine"

    def test_passthrough_mode_skips_ownership_check(self, client, stub_run, monkeypatch):
        called = {"value": False}

        def _boom(**kwargs):
            called["value"] = True
            raise AssertionError("passthrough must not consult conversation ownership")

        monkeypatch.setattr(sessions, "require_not_owned_by_other_sync", _boom)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "conversation": "conv_abc",
            },
        )
        assert response.status_code == 200
        assert called["value"] is False


class TestTranscriptInference:
    """Transcript-fingerprint continuity for stateless clients (§5d)."""

    def test_agent_mode_records_transcript_on_success(
        self, client, stub_run, no_transcript_store, monkeypatch
    ):
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "agent")
        monkeypatch.setattr(sessions, "require_not_owned_by_other_sync", lambda **kwargs: None)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert len(no_transcript_store) == 1
        record = no_transcript_store[0]
        conversation_id = stub_run["ctx"].conversation_id
        assert record["conversation_id"] == conversation_id
        # Recorded with the banner attached: that is the text the client echoes
        # back next turn, so the fingerprint has to cover it.
        assert record["content"].startswith("hello there")
        assert conversation_id in record["content"]

    def test_agent_mode_streaming_records_streamed_text(
        self, client, stub_stream, no_transcript_store, monkeypatch
    ):
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "agent")
        monkeypatch.setattr(sessions, "require_not_owned_by_other_sync", lambda **kwargs: None)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert len(no_transcript_store) == 1
        record = no_transcript_store[0]
        # The banner streamed to the client is part of the recorded text, or the
        # next request's prefix would hash to something never stored.
        assert record["content"].startswith("hello")
        assert record["conversation_id"] in record["content"]

    def test_passthrough_mode_does_not_record_transcript(
        self, client, stub_run, no_transcript_store
    ):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert no_transcript_store == []

    def test_agent_mode_rejoins_inferred_conversation(self, client, stub_run, monkeypatch):
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "agent")
        monkeypatch.setattr(sessions, "require_not_owned_by_other_sync", lambda **kwargs: None)

        async def _infer(messages, principal, cfg):
            return "openai-prior"

        monkeypatch.setattr(sessions, "infer_conversation_from_transcript", _infer)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello there"},
                    {"role": "user", "content": "and again"},
                ],
            },
        )
        assert response.status_code == 200
        assert stub_run["ctx"].conversation_id == "openai-prior"

    def test_explicit_conversation_wins_over_inference(self, client, stub_run, monkeypatch):
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "agent")
        monkeypatch.setattr(sessions, "require_not_owned_by_other_sync", lambda **kwargs: None)

        async def _infer(messages, principal, cfg):
            raise AssertionError("explicit session references must skip inference")

        monkeypatch.setattr(sessions, "infer_conversation_from_transcript", _infer)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "conversation": "conv_explicit",
            },
        )
        assert response.status_code == 200
        assert stub_run["ctx"].conversation_id == "conv_explicit"


class TestSessionBanner:
    """The user-visible session footer that carries continuity (§5d)."""

    @staticmethod
    def _agent_mode(monkeypatch):
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "agent")
        monkeypatch.setattr(sessions, "require_not_owned_by_other_sync", lambda **kwargs: None)

    def test_reply_carries_the_conversation_id(self, client, stub_run, monkeypatch):
        self._agent_mode(monkeypatch)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        assert content.startswith("hello there")
        assert stub_run["ctx"].conversation_id in content

    def test_streamed_reply_carries_the_conversation_id(self, client, stub_stream, monkeypatch):
        self._agent_mode(monkeypatch)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        streamed = "".join(
            event["choices"][0]["delta"].get("content") or ""
            for event in sse_events(response.text)
            if event.get("choices")
        )
        assert streamed.startswith("hello")
        assert response.headers["X-Motet-Conversation-Id"] in streamed

    def test_passthrough_mode_gets_no_banner(self, client, stub_run):
        """Only agent mode has cross-turn memory worth reconnecting."""
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello there"

    def test_banner_can_be_turned_off(self, client, stub_run, monkeypatch):
        self._agent_mode(monkeypatch)
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_SESSION_BANNER", "off")
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello there"

    def test_first_mode_skips_a_rejoined_conversation(self, client, stub_run, monkeypatch):
        self._agent_mode(monkeypatch)
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_SESSION_BANNER", "first")
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "conversation": "conv_existing",
            },
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello there"

    def test_echoed_banner_is_hidden_from_the_model(self, client, stub_run, monkeypatch):
        """History reaches the agent clean, so banners cost no context."""
        self._agent_mode(monkeypatch)
        banner = sessions.build_session_banner("openai-prior")
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": f"hello there{banner}"},
                    {"role": "user", "content": "and again"},
                ],
            },
        )
        assert response.status_code == 200
        ctx = stub_run["ctx"]
        assert ctx.conversation_id == "openai-prior"
        assistant = [m for m in ctx.messages if m.role == "assistant"]
        assert [m.content for m in assistant] == ["hello there"]
        # The unstripped copy is retained so fingerprints still match.
        assert any(banner in m.content for m in ctx.raw_messages)

    def test_guard_instruction_precedes_client_instructions(
        self, client, stub_run, monkeypatch
    ):
        self._agent_mode(monkeypatch)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hi"},
                ],
            },
        )
        assert response.status_code == 200
        messages = stub_run["ctx"].messages
        assert messages[0].role == "system"
        assert "Motet session" in messages[0].content
        assert messages[1].content == "be terse"

    def test_guard_can_be_turned_off(self, client, stub_run, monkeypatch):
        self._agent_mode(monkeypatch)
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_SESSION_BANNER_GUARD", "false")
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert all("Motet session" not in m.content for m in stub_run["ctx"].messages)


class TestModeSelection:
    """Mode precedence and the credential ceiling (ADR-0125 §5c)."""

    def test_request_override_disabled_by_default(self, client, stub_run):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "motet_mode": "agent",
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "mode_override_disabled"

    def test_unparseable_mode_value_is_400(self, client, stub_run):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "motet_mode": "agnt",
            },
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "invalid_facade_mode"
        assert error["param"] == "motet_mode"

    def test_unparseable_header_mode_is_400(self, client, stub_run, monkeypatch):
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_ALLOW_REQUEST_MODE_OVERRIDE", "true")
        response = client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Motet-Facade-Mode": "turbo"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_facade_mode"

    def test_alias_cannot_escalate_above_credential(self, client, stub_run):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini:agent",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "mode_not_permitted"

    def test_alias_selects_permitted_mode(self, client, stub_run, monkeypatch):
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "agent")
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini:hosted_tools",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert stub_run["ctx"].mode.value == "hosted_tools"
        assert response.headers["X-Motet-Facade-Mode"] == "hosted_tools"

    def test_bound_mode_applies_without_alias(self, client, stub_run, monkeypatch):
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "agent")
        client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert stub_run["ctx"].mode.value == "agent"

    def test_header_override_honored_when_enabled(self, client, stub_run, monkeypatch):
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_ALLOW_REQUEST_MODE_OVERRIDE", "true")
        monkeypatch.setenv("MOTET_OPENAI_COMPAT_DEFAULT_MODE", "agent")
        client.post(
            "/v1/chat/completions",
            json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Motet-Facade-Mode": "passthrough"},
        )
        assert stub_run["ctx"].mode.value == "passthrough"


class TestChatStreaming:
    """SSE framing for chat.completion.chunk streams."""

    def test_stream_frames_and_sentinel(self, client, stub_stream):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.endswith("data: [DONE]\n\n")

        events = sse_events(response.text)
        assert events[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
        assert [e["choices"][0]["delta"].get("content") for e in events[1:3]] == ["hel", "lo"]
        assert events[-1]["choices"][0]["finish_reason"] == "stop"
        assert all(e["object"] == "chat.completion.chunk" for e in events)

    def test_stream_records_completion_id_for_chaining(
        self, client, stub_stream, no_session_writes
    ):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        events = sse_events(response.text)
        assert no_session_writes and no_session_writes[0][0] == events[0]["id"]

    def test_include_usage_emits_final_usage_chunk(self, client, stub_stream):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        events = sse_events(response.text)
        assert events[-1]["choices"] == []
        assert events[-1]["usage"]["total_tokens"] == 6

    def test_usage_chunk_absent_by_default(self, client, stub_stream):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert all("usage" not in event for event in sse_events(response.text))

    def test_mid_stream_error_emits_error_frame_and_no_sentinel(self, client, monkeypatch):
        from motet.interfaces.api.openai_compat.errors import FacadeError

        def _stream(ctx):
            async def _gen():
                yield ("delta", "partial")
                raise FacadeError(502, "upstream exploded", error_type="api_error")

            return _gen()

        monkeypatch.setattr(execution, "stream", _stream)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert "[DONE]" not in response.text
        assert sse_events(response.text)[-1]["error"]["message"] == "upstream exploded"

    def test_tool_calls_emitted_before_finish(self, client, monkeypatch):
        def _stream(ctx):
            async def _gen():
                yield (
                    "result",
                    {
                        "content": "",
                        "tool_calls_canonical": [
                            {
                                "call_id": "call_1",
                                "tool_name": "mcp.github.list_repos",
                                "arguments_json": "{}",
                            }
                        ],
                    },
                )

            return _gen()

        monkeypatch.setattr(execution, "stream", _stream)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        events = sse_events(response.text)
        tool_delta = events[-2]["choices"][0]["delta"]["tool_calls"][0]
        assert tool_delta["function"]["name"] == "mcp__github__list_repos"
        assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"

    def test_argument_fragments_stream_as_incremental_tool_calls(self, client, monkeypatch):
        """A long tool call reaches the client as it is generated, not only at the end."""

        def _stream(ctx):
            async def _gen():
                yield ("tool_call_delta", {
                    "call_id": "Write_1",
                    "tool_name": "Write",
                    "arguments_delta": '{"path":"a.py",',
                    "first": True,
                })
                yield ("tool_call_delta", {
                    "call_id": "Write_1",
                    "tool_name": "Write",
                    "arguments_delta": '"contents":"x"}',
                    "first": False,
                })
                yield (
                    "result",
                    {
                        "content": "",
                        "finish_reason": "tool_calls",
                        "tool_calls_canonical": [
                            {
                                "call_id": "Write_1",
                                "tool_name": "Write",
                                "arguments_json": '{"path": "a.py", "contents": "x"}',
                            }
                        ],
                    },
                )

            return _gen()

        monkeypatch.setattr(execution, "stream", _stream)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        events = sse_events(response.text)
        fragments = [
            e["choices"][0]["delta"]["tool_calls"][0]
            for e in events
            if e.get("choices") and e["choices"][0].get("delta", {}).get("tool_calls")
        ]

        # Identity once, then arguments against the same index.
        assert len(fragments) == 2, "the completed call must not be re-sent"
        assert fragments[0]["id"] == "Write_1"
        assert fragments[0]["function"]["name"] == "Write"
        assert fragments[0]["index"] == fragments[1]["index"] == 0
        assert "id" not in fragments[1]
        assembled = "".join(f["function"]["arguments"] for f in fragments)
        assert json.loads(assembled) == {"path": "a.py", "contents": "x"}
        assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"

    def test_unstreamed_calls_still_ride_the_final_chunk(self, client, monkeypatch):
        """A call with no streamed fragments is delivered whole, indexed past the streamed one."""

        def _stream(ctx):
            async def _gen():
                yield ("tool_call_delta", {
                    "call_id": "Write_1",
                    "tool_name": "Write",
                    "arguments_delta": '{"path":"a.py"}',
                    "first": True,
                })
                yield (
                    "result",
                    {
                        "content": "",
                        "finish_reason": "tool_calls",
                        "tool_calls_canonical": [
                            {
                                "call_id": "Write_1",
                                "tool_name": "Write",
                                "arguments_json": '{"path": "a.py"}',
                            },
                            {
                                "call_id": "Read_1",
                                "tool_name": "Read",
                                "arguments_json": '{"path": "b.py"}',
                            },
                        ],
                    },
                )

            return _gen()

        monkeypatch.setattr(execution, "stream", _stream)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        events = sse_events(response.text)
        final = events[-2]["choices"][0]["delta"]["tool_calls"]
        assert [c["id"] for c in final] == ["Read_1"]
        assert final[0]["index"] == 1

    def test_stream_emits_reasoning_content_deltas(self, client, monkeypatch):
        def _stream(ctx):
            async def _gen():
                yield ("thinking", {"text": "step1 ", "is_complete": False})
                yield ("thinking", {"text": "step2", "is_complete": False})
                yield ("thinking", {"text": "", "is_complete": True})
                yield ("delta", "answer")
                yield (
                    "result",
                    {
                        "content": "answer",
                        "finish_reason": "stop",
                        "reasoning_content": "step1 step2",
                    },
                )

            return _gen()

        monkeypatch.setattr(execution, "stream", _stream)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "mock/mock-small",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "reasoning_effort": "medium",
            },
        )
        assert response.status_code == 200
        events = sse_events(response.text)
        reasoning = [
            e["choices"][0]["delta"].get("reasoning_content")
            for e in events
            if e.get("choices") and e["choices"][0]["delta"].get("reasoning_content")
        ]
        assert reasoning == ["step1 ", "step2"]
        content = "".join(
            e["choices"][0]["delta"].get("content") or ""
            for e in events
            if e.get("choices")
        )
        assert "answer" in content

    def test_opt_in_sets_enable_thinking_for_capable_model(self, client, stub_run):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "mock/mock-small",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
        )
        assert response.status_code == 200
        settings = stub_run["ctx"].model_settings
        assert settings["enable_thinking"] is True
        assert settings["reasoning_effort"] == "high"

    def test_opt_in_stripped_for_incapable_model(self, client, stub_run):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
        )
        assert response.status_code == 200
        settings = stub_run["ctx"].model_settings
        assert "enable_thinking" not in settings


class TestResponsesEndpoint:
    """Responses API surface."""

    def test_non_streaming_shape(self, client, stub_run, no_session_writes):
        response = client.post(
            "/v1/responses",
            json={"model": "openai/gpt-4o-mini", "input": "hi"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "response"
        assert body["status"] == "completed"
        assert body["output_text"] == "hello there"
        assert body["id"].startswith("resp_")
        assert no_session_writes and no_session_writes[0][0] == body["id"]

    def test_stream_emits_named_events(self, client, stub_stream):
        response = client.post(
            "/v1/responses",
            json={"model": "openai/gpt-4o-mini", "input": "hi", "stream": True},
        )
        assert response.status_code == 200
        names = [
            line[len("event: ") :]
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]
        assert names[0] == "response.created"
        assert "response.output_text.delta" in names
        assert names[-1] == "response.completed"

        deltas = [
            json.loads(line[len("data: ") :])
            for line in response.text.splitlines()
            if line.startswith("data: ") and '"response.output_text.delta"' in line
        ]
        assert [d["delta"] for d in deltas] == ["hel", "lo"]

    def test_stream_sequence_numbers_increase(self, client, stub_stream):
        response = client.post(
            "/v1/responses",
            json={"model": "openai/gpt-4o-mini", "input": "hi", "stream": True},
        )
        sequences = [event["sequence_number"] for event in sse_events(response.text)]
        assert sequences == sorted(sequences)
        assert sequences[0] == 0

    def test_stream_emits_reasoning_before_message(self, client, monkeypatch):
        def _stream(ctx):
            async def _gen():
                yield ("thinking", {"text": "why ", "is_complete": False})
                yield ("thinking", {"text": "", "is_complete": True})
                yield ("delta", "ok")
                yield (
                    "result",
                    {
                        "content": "ok",
                        "finish_reason": "stop",
                        "reasoning_content": "why ",
                    },
                )

            return _gen()

        monkeypatch.setattr(execution, "stream", _stream)

        response = client.post(
            "/v1/responses",
            json={
                "model": "mock/mock-small",
                "input": "hi",
                "stream": True,
                "reasoning": {"effort": "medium"},
            },
        )
        assert response.status_code == 200
        names = [
            line[len("event: ") :]
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]
        assert names[0] == "response.created"
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

        # Every item owns a distinct output_index — two items sharing one index
        # makes the stream unreconstructable.
        indices = [
            event["output_index"]
            for event in sse_events(response.text)
            if event["type"] == "response.output_item.added"
        ]
        assert indices == [0, 1]


    def test_stream_scaffolds_function_call_item_and_argument_deltas(
        self, client, monkeypatch
    ):
        """A function call must be observable from the events, not only the snapshot."""

        def _stream(ctx):
            async def _gen():
                yield ("delta", "writing it")
                yield ("tool_call_delta", {
                    "call_id": "Write_0",
                    "tool_name": "Write",
                    "arguments_delta": '{"path":',
                    "first": True,
                })
                yield ("tool_call_delta", {
                    "call_id": "Write_0",
                    "tool_name": "Write",
                    "arguments_delta": '"a.py"}',
                    "first": False,
                })
                yield (
                    "result",
                    {
                        "content": "writing it",
                        "finish_reason": "tool_calls",
                        "tool_calls_canonical": [
                            {
                                "call_id": "Write_0",
                                "tool_name": "Write",
                                "arguments_json": '{"path": "a.py"}',
                            }
                        ],
                    },
                )

            return _gen()

        monkeypatch.setattr(execution, "stream", _stream)

        response = client.post(
            "/v1/responses",
            json={"model": "openai/gpt-4o-mini", "input": "hi", "stream": True},
        )
        assert response.status_code == 200
        events = sse_events(response.text)
        names = [e["type"] for e in events]

        # The message item is finished before the function call item opens.
        added = [
            e for e in events
            if e["type"] == "response.output_item.added"
            and e["item"]["type"] == "function_call"
        ]
        assert len(added) == 1
        assert added[0]["item"]["call_id"] == "Write_0"
        assert added[0]["item"]["name"] == "Write"
        assert added[0]["item"]["status"] == "in_progress"
        message_done = next(
            i for i, e in enumerate(events)
            if e["type"] == "response.output_item.done" and e["item"]["type"] == "message"
        )
        function_call_added = next(
            i for i, e in enumerate(events)
            if e["type"] == "response.output_item.added"
            and e["item"]["type"] == "function_call"
        )
        assert message_done < function_call_added

        # Arguments arrive incrementally and then as a completed item.
        arg_deltas = [
            e["delta"] for e in events
            if e["type"] == "response.function_call_arguments.delta"
        ]
        assert "".join(arg_deltas) == '{"path":"a.py"}'
        done = next(
            e for e in events if e["type"] == "response.function_call_arguments.done"
        )
        assert done["arguments"] == '{"path":"a.py"}'
        assert done["item_id"] == added[0]["item"]["id"]

        # Output indices follow arrival order: message 0, function call 1.
        assert added[0]["output_index"] == 1
        assert events[message_done]["output_index"] == 0

        # The snapshot keeps the call, and uses the same item id the stream used.
        final = next(e for e in events if e["type"] == "response.completed")
        output = final["response"]["output"]
        fc = [item for item in output if item["type"] == "function_call"]
        assert len(fc) == 1
        assert fc[0]["id"] == added[0]["item"]["id"]

    def test_tool_call_only_turn_emits_no_message_item(self, client, monkeypatch):
        """OpenAI emits no message item for a pure tool-call turn; neither does the snapshot."""

        def _stream(ctx):
            async def _gen():
                yield ("tool_call_delta", {
                    "call_id": "Write_0",
                    "tool_name": "Write",
                    "arguments_delta": '{"path":"a.py"}',
                    "first": True,
                })
                yield (
                    "result",
                    {
                        "content": "",
                        "finish_reason": "tool_calls",
                        "tool_calls_canonical": [
                            {
                                "call_id": "Write_0",
                                "tool_name": "Write",
                                "arguments_json": '{"path": "a.py"}',
                            }
                        ],
                    },
                )

            return _gen()

        monkeypatch.setattr(execution, "stream", _stream)

        response = client.post(
            "/v1/responses",
            json={"model": "openai/gpt-4o-mini", "input": "hi", "stream": True},
        )
        events = sse_events(response.text)
        item_types = [
            e["item"]["type"] for e in events if e["type"] == "response.output_item.added"
        ]
        assert item_types == ["function_call"]

        final = next(e for e in events if e["type"] == "response.completed")
        assert [i["type"] for i in final["response"]["output"]] == ["function_call"]
        # Sole item, so it owns index 0.
        added = next(
            e for e in events
            if e["type"] == "response.output_item.added"
        )
        assert added["output_index"] == 0

    def test_text_after_a_tool_call_opens_a_second_item_without_repeating(
        self, client, monkeypatch
    ):
        """A later message item owns only its own text, not the turn's whole content."""

        def _stream(ctx):
            async def _gen():
                yield ("delta", "before ")
                yield ("tool_call_delta", {
                    "call_id": "Read_0",
                    "tool_name": "Read",
                    "arguments_delta": "{}",
                    "first": True,
                })
                yield ("delta", "after")
                yield ("result", {"content": "before after", "finish_reason": "stop"})

            return _gen()

        monkeypatch.setattr(execution, "stream", _stream)

        response = client.post(
            "/v1/responses",
            json={"model": "openai/gpt-4o-mini", "input": "hi", "stream": True},
        )
        events = sse_events(response.text)
        message_texts = [
            e["item"]["content"][0]["text"]
            for e in events
            if e["type"] == "response.output_item.done" and e["item"]["type"] == "message"
        ]
        assert message_texts == ["before ", "after"]

    def test_reasoning_then_tool_call_indices_follow_arrival(self, client, monkeypatch):
        def _stream(ctx):
            async def _gen():
                yield ("thinking", {"text": "plan", "is_complete": False})
                yield ("tool_call_delta", {
                    "call_id": "Read_0",
                    "tool_name": "Read",
                    "arguments_delta": '{"path":"b.py"}',
                    "first": True,
                })
                yield (
                    "result",
                    {
                        "content": "",
                        "finish_reason": "tool_calls",
                        "reasoning_content": "plan",
                        "tool_calls_canonical": [
                            {
                                "call_id": "Read_0",
                                "tool_name": "Read",
                                "arguments_json": '{"path": "b.py"}',
                            }
                        ],
                    },
                )

            return _gen()

        monkeypatch.setattr(execution, "stream", _stream)

        response = client.post(
            "/v1/responses",
            json={
                "model": "mock/mock-small",
                "input": "hi",
                "stream": True,
                "reasoning": {"effort": "medium"},
            },
        )
        events = sse_events(response.text)
        indices = {
            e["item"]["type"]: e["output_index"]
            for e in events
            if e["type"] == "response.output_item.added"
        }
        assert indices == {"reasoning": 0, "function_call": 1}
        # Reasoning closes before the call opens.
        order = [
            (e["type"], e["item"]["type"])
            for e in events
            if e["type"] in ("response.output_item.added", "response.output_item.done")
        ]
        assert order.index(("response.output_item.done", "reasoning")) < order.index(
            ("response.output_item.added", "function_call")
        )


class TestResolveAgentId:
    """Default agent id for agent-mode clients that omit motet_agent_id (e.g. Cursor)."""

    def test_request_wins_over_policy(self):
        from motet.core.security.facade_policy import FacadeMode, FacadePolicy
        from motet.interfaces.api.openai_compat.wire import ChatCompletionRequest

        req = ChatCompletionRequest(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            motet_agent_id="cursor.backend",
        )
        policy = FacadePolicy(agent_id="core.default", allowed_models=["*"])
        assert routes._resolve_agent_id(req, FacadeMode.AGENT, policy) == "cursor.backend"

    def test_policy_agent_id_used_in_agent_mode_when_request_omits(self):
        from motet.core.security.facade_policy import FacadeMode, FacadePolicy
        from motet.interfaces.api.openai_compat.wire import ChatCompletionRequest

        req = ChatCompletionRequest(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        )
        policy = FacadePolicy(agent_id="cursor.backend", allowed_models=["*"])
        assert routes._resolve_agent_id(req, FacadeMode.AGENT, policy) == "cursor.backend"

    def test_passthrough_ignores_policy_agent_id(self):
        from motet.core.security.facade_policy import FacadeMode, FacadePolicy
        from motet.interfaces.api.openai_compat.wire import ChatCompletionRequest

        req = ChatCompletionRequest(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        )
        policy = FacadePolicy(agent_id="cursor.backend", allowed_models=["*"])
        assert routes._resolve_agent_id(req, FacadeMode.PASSTHROUGH, policy) is None
