"""
Motet - OpenAI Compatible Facade API Integration Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-28

Description:
    Integration coverage for the OpenAI-compatible facade (ADR-0125) at the HTTP
    boundary, against real Redis and real ``sa_*`` service account tokens.

    These tests exercise everything that happens before a model call: router
    mounting behind the feature flag, authentication, per-credential facade
    policy resolved from live service account records, model allowlisting, mode
    precedence and the credential ceiling, capability rejection, OpenAI error
    envelope shape, and the response_id -> conversation mapping persisted in
    Redis.

    Requests that would reach a worker are deliberately excluded here so the
    suite stays fast and worker-free. Worker-backed inference lives in
    test_openai_compat_worker_e2e.py behind the ``distributed`` marker.

Dependencies:
    - httpx: ASGI transport client for in-process HTTP calls
    - motet.interfaces.http.create_app: FastAPI application under test
    - motet.core.security.service_accounts: real sa_* token issuance
    - Redis: service account records and facade session mappings

Usage:
    docker-compose -f tests/docker-compose.test.yml run --rm test-runner \\
        python -m pytest tests/integration/api/test_openai_compat_api.py -v

Notes:
    - The facade is off by default, so every test enables it explicitly
    - The allowlist is deny-by-default, so tests set it per case
    - mock/mock-small is used as the allowlisted model because it always exists
      in the static registry and needs no provider credentials
"""

from __future__ import annotations

import os
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import pytest

from motet.core.config import Config
from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.security.service_accounts import ServiceAccountManager
from motet.core.types import Principal
from motet.interfaces.api.openai_compat import sessions
from motet.interfaces.api.openai_compat.errors import FacadeError
from motet.interfaces.http import create_app

pytestmark = pytest.mark.integration

MOCK_MODEL = "mock/mock-small"
OTHER_MODEL = "openai/gpt-4o-mini"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _async_redis(isolated_async_redis):
    """Session mappings and policy lookups all run against real async Redis."""


@pytest.fixture
def sa_factory():
    """Issue real service account tokens, optionally carrying facade policy."""
    redis_client = get_sync_redis_client("test_openai_compat_api")
    manager = ServiceAccountManager(redis_client)
    issued: List[str] = []

    def create(
        *,
        facade_mode: Optional[str] = None,
        allowed_models: Optional[List[str]] = None,
        tenant_id: str = "test-tenant",
        name: str = "test-openai-compat",
    ) -> str:
        token = manager.create_service_account(
            name=f"{name}-{uuid.uuid4().hex[:8]}",
            tenant_id=tenant_id,
            motet_id="production",
            roles=["admin", "user"],
            created_by="test@example.com",
            expires_days=1,
            facade_mode=facade_mode,
            allowed_models=allowed_models,
        )
        issued.append(token)
        return token

    yield create

    for token in issued:
        try:
            manager.revoke_service_account(token)
        except Exception:
            pass


@pytest.fixture
def facade_env(monkeypatch):
    """Enable the facade and return a setter for per-test configuration."""

    def configure(**overrides: str) -> None:
        env: Dict[str, str] = {
            "MOTET_OPENAI_COMPAT_ENABLED": "true",
            "MOTET_OPENAI_COMPAT_DEFAULT_ALLOWED_MODELS": MOCK_MODEL,
            # Clear operator .env default agent (e.g. cursor.backend) for hermetic runs.
            "MOTET_OPENAI_COMPAT_DEFAULT_AGENT_ID": "",
            "MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS": "false",
            "MOTET_REDIS_URL": os.getenv("MOTET_REDIS_URL", "redis://localhost:6379/0"),
        }
        env.update(overrides)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

    configure()
    return configure


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=30,
    )


def auth(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def error_of(response: httpx.Response) -> Dict[str, Any]:
    """Extract the OpenAI error object, asserting the envelope is well formed."""
    body = response.json()
    assert "error" in body, f"expected an OpenAI error envelope, got {body}"
    error = body["error"]
    for field in ("message", "type", "param", "code"):
        assert field in error, f"error envelope missing '{field}': {error}"
    return error


def chat_body(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": MOCK_MODEL,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Mounting and authentication
# ---------------------------------------------------------------------------


async def test_facade_is_absent_when_flag_disabled(monkeypatch, sa_factory):
    """The facade must not exist unless explicitly enabled.

    Sets the flag to false rather than deleting it: Config reads env_file=.env
    as a fallback, so on a dev machine with MOTET_OPENAI_COMPAT_ENABLED=true in
    a local .env, delenv alone would leave the facade mounted (#145). A real
    environment variable takes precedence over the .env file.
    """
    monkeypatch.setenv("MOTET_OPENAI_COMPAT_ENABLED", "false")
    token = sa_factory()

    async with _client(create_app()) as client:
        response = await client.get("/v1/models", headers=auth(token))

    assert response.status_code == 404


async def test_facade_mounts_at_configured_prefix(facade_env, sa_factory):
    """The mount prefix is configurable for deployments that reserve /v1."""
    facade_env(MOTET_OPENAI_COMPAT_PREFIX="/openai/v1")
    token = sa_factory()

    async with _client(create_app()) as client:
        moved = await client.get("/openai/v1/models", headers=auth(token))
        default = await client.get("/v1/models", headers=auth(token))

    assert moved.status_code == 200
    assert default.status_code == 404


async def test_models_requires_authentication(facade_env):
    """Unauthenticated facade calls are rejected, not served anonymously."""
    async with _client(create_app()) as client:
        response = await client.get("/v1/models")

    assert response.status_code == 401


async def test_chat_completion_requires_authentication(facade_env):
    """Inference routes reject unauthenticated callers before any model work."""
    async with _client(create_app()) as client:
        response = await client.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Model listing and allowlisting
# ---------------------------------------------------------------------------


async def test_models_reflect_config_allowlist(facade_env, sa_factory):
    """A credential with no bound allowlist inherits the configured default."""
    token = sa_factory()

    async with _client(create_app()) as client:
        response = await client.get("/v1/models", headers=auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [entry["id"] for entry in body["data"]] == [MOCK_MODEL]
    card = body["data"][0]
    assert card["object"] == "model"
    assert card["owned_by"] == "mock"


async def test_models_are_empty_when_allowlist_unset(facade_env, sa_factory):
    """Deny-by-default: enabling the facade alone exposes no models."""
    facade_env(MOTET_OPENAI_COMPAT_DEFAULT_ALLOWED_MODELS="")
    token = sa_factory()

    async with _client(create_app()) as client:
        response = await client.get("/v1/models", headers=auth(token))

    assert response.status_code == 200
    assert response.json()["data"] == []


async def test_service_account_allowlist_overrides_config(facade_env, sa_factory):
    """Policy stored on the live token record wins over configuration."""
    token = sa_factory(allowed_models=[OTHER_MODEL])

    async with _client(create_app()) as client:
        response = await client.get("/v1/models", headers=auth(token))

    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()["data"]] == [OTHER_MODEL]


async def test_provider_wildcard_allowlist_expands(facade_env, sa_factory):
    """A provider wildcard lists every model that provider offers."""
    token = sa_factory(allowed_models=["mock/*"])

    async with _client(create_app()) as client:
        response = await client.get("/v1/models", headers=auth(token))

    ids = [entry["id"] for entry in response.json()["data"]]
    assert MOCK_MODEL in ids
    assert all(entry.startswith("mock/") for entry in ids)


async def test_retrieve_allowlisted_model(facade_env, sa_factory):
    """A single model card is retrievable by its facade id."""
    token = sa_factory()

    async with _client(create_app()) as client:
        response = await client.get(f"/v1/models/{MOCK_MODEL}", headers=auth(token))

    assert response.status_code == 200
    assert response.json()["id"] == MOCK_MODEL


async def test_retrieve_denied_model_is_indistinguishable_from_unknown(
    facade_env, sa_factory
):
    """A denied model reports not-found so the allowlist is not disclosed."""
    token = sa_factory()

    async with _client(create_app()) as client:
        denied = await client.get(f"/v1/models/{OTHER_MODEL}", headers=auth(token))
        unknown = await client.get("/v1/models/no-such-model", headers=auth(token))

    assert denied.status_code == 404
    assert unknown.status_code == 404
    assert error_of(denied)["code"] == "model_not_found"
    assert error_of(unknown)["code"] == "model_not_found"
    assert error_of(denied)["type"] == "not_found_error"


async def test_chat_completion_rejects_denied_model(facade_env, sa_factory):
    """Inference on a non-allowlisted model fails before reaching a worker."""
    token = sa_factory()

    async with _client(create_app()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=chat_body(model=OTHER_MODEL),
            headers=auth(token),
        )

    assert response.status_code == 404
    error = error_of(response)
    assert error["code"] == "model_not_found"
    assert error["param"] == "model"


async def test_chat_completion_requires_a_model(facade_env, sa_factory):
    """An empty model is a client error, not a silent default."""
    token = sa_factory()

    async with _client(create_app()) as client:
        response = await client.post(
            "/v1/chat/completions", json=chat_body(model=""), headers=auth(token)
        )

    assert response.status_code == 400
    assert error_of(response)["code"] == "missing_model"


# ---------------------------------------------------------------------------
# Unsupported parameters and capabilities
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,param",
    [
        ({"n": 2}, "n"),
        ({"logprobs": True}, "logprobs"),
        ({"top_logprobs": 3}, "top_logprobs"),
    ],
)
async def test_chat_completion_rejects_unsupported_parameters(
    facade_env, sa_factory, overrides, param
):
    """Parameters Motet cannot honor are refused rather than ignored."""
    token = sa_factory()

    async with _client(create_app()) as client:
        response = await client.post(
            "/v1/chat/completions", json=chat_body(**overrides), headers=auth(token)
        )

    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "unsupported_parameter"
    assert error["param"] == param


async def test_chat_completion_rejects_tools_for_model_without_tool_support(
    facade_env, sa_factory
):
    """Passthrough refuses tools the resolved model cannot call."""
    token = sa_factory()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Look up weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        }
    ]

    async with _client(create_app()) as client:
        response = await client.post(
            "/v1/chat/completions", json=chat_body(tools=tools), headers=auth(token)
        )

    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "unsupported_capability"
    assert error["param"] == "tools"


async def test_chat_completion_rejects_structured_output_for_incapable_model(
    facade_env, sa_factory
):
    """Passthrough refuses response_format the resolved model cannot honor."""
    token = sa_factory()

    async with _client(create_app()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=chat_body(response_format={"type": "json_object"}),
            headers=auth(token),
        )

    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "unsupported_capability"
    assert error["param"] == "response_format"


# ---------------------------------------------------------------------------
# Mode precedence and the credential ceiling
# ---------------------------------------------------------------------------


async def test_request_mode_override_is_disabled_by_default(facade_env, sa_factory):
    """Request-level mode selection is refused unless an operator enables it."""
    token = sa_factory()

    async with _client(create_app()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=chat_body(motet_mode="agent"),
            headers=auth(token),
        )

    assert response.status_code == 400
    assert error_of(response)["code"] == "mode_override_disabled"


async def test_request_mode_override_header_is_disabled_by_default(
    facade_env, sa_factory
):
    """The header form of the override obeys the same switch as the body form."""
    token = sa_factory()

    async with _client(create_app()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=chat_body(),
            headers={**auth(token), "X-Motet-Facade-Mode": "agent"},
        )

    assert response.status_code == 400
    assert error_of(response)["code"] == "mode_override_disabled"


async def test_alias_mode_above_credential_ceiling_is_forbidden(facade_env, sa_factory):
    """A model alias cannot escalate past the mode bound to the credential."""
    token = sa_factory(facade_mode="passthrough", allowed_models=[MOCK_MODEL])

    async with _client(create_app()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=chat_body(model=f"{MOCK_MODEL}:agent"),
            headers=auth(token),
        )

    assert response.status_code == 403
    error = error_of(response)
    assert error["code"] == "mode_not_permitted"
    assert error["type"] == "permission_error"


async def test_request_override_above_credential_ceiling_is_forbidden(
    facade_env, sa_factory
):
    """With overrides enabled, the ceiling still applies."""
    facade_env(MOTET_OPENAI_COMPAT_ALLOW_REQUEST_MODE_OVERRIDE="true")
    token = sa_factory(facade_mode="hosted_tools", allowed_models=[MOCK_MODEL])

    async with _client(create_app()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=chat_body(motet_mode="agent"),
            headers=auth(token),
        )

    assert response.status_code == 403
    assert error_of(response)["code"] == "mode_not_permitted"


async def test_model_alias_does_not_break_model_resolution(facade_env, sa_factory):
    """A mode alias is stripped before the model is resolved."""
    token = sa_factory()

    async with _client(create_app()) as client:
        response = await client.get(
            f"/v1/models/{MOCK_MODEL}:passthrough", headers=auth(token)
        )

    assert response.status_code == 200
    assert response.json()["id"] == MOCK_MODEL


# ---------------------------------------------------------------------------
# Session mapping against real Redis
# ---------------------------------------------------------------------------


def _principal(principal_id: str = "sa:test", tenant_id: str = "test-tenant") -> Principal:
    return Principal(id=principal_id, tenant_id=tenant_id, roles=["user"])


async def test_response_id_maps_to_conversation(facade_env):
    """A recorded response id resolves back to its Motet conversation."""
    cfg = Config()
    principal = _principal()
    response_id = f"resp_{uuid.uuid4().hex}"
    conversation_id = sessions.new_conversation_id()

    await sessions.remember_response(response_id, conversation_id, principal, cfg)
    resolved = await sessions.lookup_response_conversation(response_id, principal)

    assert resolved == conversation_id


async def test_response_id_lookup_enforces_ownership(facade_env):
    """One principal cannot resume another principal's response chain."""
    cfg = Config()
    owner = _principal("sa:owner")
    intruder = _principal("sa:intruder")
    response_id = f"resp_{uuid.uuid4().hex}"

    await sessions.remember_response(
        response_id, sessions.new_conversation_id(), owner, cfg
    )

    with pytest.raises(FacadeError) as exc_info:
        await sessions.lookup_response_conversation(response_id, intruder)

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "response_not_found"


async def test_unknown_response_id_resolves_to_nothing(facade_env):
    """An unrecognized response id is simply absent, not an error."""
    cfg = Config()
    principal = _principal()
    known_id = f"resp_{uuid.uuid4().hex}"
    conversation_id = sessions.new_conversation_id()
    await sessions.remember_response(known_id, conversation_id, principal, cfg)

    # Proving the known id resolves first keeps this from passing merely because
    # the lookup itself failed.
    assert await sessions.lookup_response_conversation(known_id, principal) == (
        conversation_id
    )
    assert (
        await sessions.lookup_response_conversation(
            f"resp_{uuid.uuid4().hex}", principal
        )
        is None
    )


async def test_previous_response_id_reuses_recorded_conversation(facade_env):
    """Chaining by previous_response_id continues the same conversation."""
    from motet.interfaces.api.openai_compat.wire import ResponsesRequest

    cfg = Config()
    principal = _principal()
    response_id = f"resp_{uuid.uuid4().hex}"
    conversation_id = sessions.new_conversation_id()
    await sessions.remember_response(response_id, conversation_id, principal, cfg)

    request = ResponsesRequest(
        model=MOCK_MODEL,
        input="follow up",
        previous_response_id=response_id,
    )
    resolved = await sessions.resolve_conversation_id(request, principal, cfg)

    assert resolved == conversation_id


async def test_conversation_and_previous_response_id_are_mutually_exclusive(facade_env):
    """Two conflicting session selectors is a client error, not a guess."""
    from motet.interfaces.api.openai_compat.wire import ResponsesRequest

    request = ResponsesRequest(
        model=MOCK_MODEL,
        input="hello",
        conversation="conv_abc",
        previous_response_id="resp_abc",
    )

    with pytest.raises(FacadeError) as exc_info:
        await sessions.resolve_conversation_id(request, _principal(), Config())

    assert exc_info.value.status_code == 400


async def test_fresh_request_mints_a_new_conversation(facade_env):
    """Absent any session hint, each request gets its own conversation."""
    from motet.interfaces.api.openai_compat.wire import ChatCompletionRequest

    cfg = Config()
    principal = _principal()
    request = ChatCompletionRequest(
        model=MOCK_MODEL, messages=[{"role": "user", "content": "hi"}]
    )

    first = await sessions.resolve_conversation_id(request, principal, cfg)
    second = await sessions.resolve_conversation_id(request, principal, cfg)

    assert first != second
    assert first.startswith("openai-")
