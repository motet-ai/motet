"""
Motet - OpenAI Compatible Execution Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

Description:
    Unit tests for the facade execution backends (ADR-0125 §5c, §11b, §11d):
    ADR-0029 envelope unwrapping, hosted tool exposure being deny-by-default,
    hosted_tools ``core.agent_loop`` dispatch (ADR-0134 Phase 4), identity-scoped
    tool-listing cache isolation, error sanitization on failure paths, and
    the agent-mode client-tool handback + resume flow (ADR-0125 §5c.1 /
    ADR-0127 / ADR-0134): handback_tools context injection, suspended results
    as OpenAI tool_calls turns, resume via Turn Runtime handles, and
    conversation rebinding on resume.

Dependencies:
    - pytest: test runner with asyncio support
    - motet.interfaces.api.openai_compat.execution: system under test

Usage:
    pytest tests/unit/interfaces/api/test_openai_compat_execution.py

Notes:
    - Distributed command execution is stubbed; these tests cover facade
      dispatch and authorization, not worker behavior
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from motet.core.security.facade_policy import FacadeMode, FacadePolicy
from motet.core.types import Message, Principal
from motet.interfaces.api.openai_compat import execution
from motet.interfaces.api.openai_compat.errors import FacadeError

PRINCIPAL = Principal(id="service-account:facade", tenant_id="t1", motet_id="m1", roles=["member"])


def make_cfg(**overrides) -> SimpleNamespace:
    base = {
        "openai_compat_hosted_tools_allowlist": "",
        "openai_compat_max_tool_iterations": 4,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def make_ctx(mode=FacadeMode.HOSTED_TOOLS, **overrides) -> execution.FacadeContext:
    kwargs = {
        "mode": mode,
        "policy": FacadePolicy(mode=mode, allowed_models=["*"]),
        "principal": PRINCIPAL,
        "cfg": make_cfg(),
        "provider": "openai",
        "registry_key": "gpt-4o-mini",
        "spec": SimpleNamespace(name="gpt-4o-mini", capabilities={"tool_use"}),
        "model_id": "openai/gpt-4o-mini",
        "messages": [Message(role="user", content="hi")],
        "model_settings": {"provider": "openai", "model_name": "gpt-4o-mini"},
        "conversation_id": "conv-1",
    }
    kwargs.update(overrides)
    return execution.FacadeContext(**kwargs)


@pytest.fixture(autouse=True)
def clear_tool_cache():
    """The worker tool listing is cached per identity scope; isolate tests."""
    execution._tool_cache.clear()
    yield
    execution._tool_cache.clear()


class TestUnwrap:
    """ADR-0029 envelopes collapse to the command's data payload."""

    def test_nested_success_envelope(self):
        envelope = {"status": "completed", "result": {"status": "success", "data": {"content": "x"}}}
        assert execution._unwrap(envelope, operation="op") == {"content": "x"}

    def test_flat_command_response(self):
        envelope = {"status": "success", "data": {"content": "x"}}
        assert execution._unwrap(envelope, operation="op") == {"content": "x"}

    def test_outer_error_raises(self):
        with pytest.raises(FacadeError) as exc:
            execution._unwrap({"status": "error", "error": {"message": "boom"}}, operation="op")
        assert exc.value.status_code == 502
        assert "boom" in exc.value.message

    def test_inner_error_raises(self):
        envelope = {"status": "completed", "result": {"status": "error", "error": "inner boom"}}
        with pytest.raises(FacadeError):
            execution._unwrap(envelope, operation="op")

    def test_non_dict_raises(self):
        with pytest.raises(FacadeError):
            execution._unwrap(None, operation="op")

    def test_error_text_is_sanitized(self):
        envelope = {"status": "error", "error": {"message": "bad key sk-abcdefghijklmnop"}}
        with pytest.raises(FacadeError) as exc:
            execution._unwrap(envelope, operation="op")
        assert "sk-abcdefghijklmnop" not in exc.value.to_payload()["error"]["message"]


class TestToolAllowlist:
    """Hosted tool exposure is deny-by-default (ADR-0125 §11b)."""

    @pytest.mark.parametrize(
        "pattern,name,expected",
        [
            ("*", "core.web_search", True),
            ("core.web_search", "core.web_search", True),
            ("core.web_search", "core.file_read", False),
            ("mcp.github.*", "mcp.github.list_repos", True),
            ("mcp.github.*", "mcp.slack.post", False),
        ],
    )
    def test_pattern_matching(self, pattern, name, expected):
        assert execution._tool_allowed(name, [pattern]) is expected

    @pytest.mark.asyncio
    async def test_no_allowlist_exposes_nothing(self, monkeypatch):
        called = False

        async def _list(ctx):
            nonlocal called
            called = True
            return [{"name": "core.web_search", "description": "", "schema": None}]

        monkeypatch.setattr(execution, "_list_worker_tools", _list)
        schemas = await execution.hosted_tool_schemas(make_ctx())

        assert schemas == []
        assert called is False, "empty allowlist should not even query the registry"

    @pytest.mark.asyncio
    async def test_allowlisted_tools_become_schemas(self, monkeypatch):
        async def _list(ctx):
            return [
                {
                    "name": "core.web_search",
                    "description": "search",
                    "schema": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
                {"name": "core.file_read", "description": "read", "schema": None},
            ]

        monkeypatch.setattr(execution, "_list_worker_tools", _list)
        ctx = make_ctx(cfg=make_cfg(openai_compat_hosted_tools_allowlist="core.web_search"))
        schemas = await execution.hosted_tool_schemas(ctx)

        assert [s.name for s in schemas] == ["core.web_search"]
        assert schemas[0].json_schema["properties"]["q"]["type"] == "string"

    @pytest.mark.asyncio
    async def test_unusable_schema_falls_back_to_open_object(self, monkeypatch):
        async def _list(ctx):
            return [{"name": "core.file_read", "description": "read", "schema": {"path": "x"}}]

        monkeypatch.setattr(execution, "_list_worker_tools", _list)
        ctx = make_ctx(cfg=make_cfg(openai_compat_hosted_tools_allowlist="core.*"))
        schemas = await execution.hosted_tool_schemas(ctx)

        assert schemas[0].json_schema["type"] == "object"
        assert schemas[0].json_schema["additionalProperties"] is True


class TestToolCacheScoping:
    """The tool-listing cache must not leak one identity's listing to another."""

    @pytest.mark.asyncio
    async def test_cache_is_keyed_by_identity_scope(self, monkeypatch):
        listings: List[str] = []

        async def _execute(command, *, operation):
            listings.append(operation)
            return {
                "tools": [{"name": f"core.tool_{len(listings)}", "description": "", "schema": None}]
            }

        monkeypatch.setattr(execution, "_execute", _execute)

        ctx_a = make_ctx()
        ctx_b = make_ctx(
            principal=Principal(
                id="service-account:other", tenant_id="t2", motet_id="m1", roles=["member"]
            )
        )

        first = await execution._list_worker_tools(ctx_a)
        second = await execution._list_worker_tools(ctx_b)
        cached = await execution._list_worker_tools(ctx_a)

        assert len(listings) == 2, "different identity scopes must not share a cache entry"
        assert first == cached, "same scope should hit the cache within the TTL"
        assert first != second


class TestHostedToolLoop:
    """hosted_tools dispatches core.agent_loop; ownership lives in Turn Runtime."""

    @pytest.fixture
    def hosted_env(self, monkeypatch):
        async def _list(ctx):
            return [
                {
                    "name": "core.web_search",
                    "description": "search",
                    "schema": {"type": "object", "properties": {}},
                }
            ]

        monkeypatch.setattr(execution, "_list_worker_tools", _list)
        return make_ctx(cfg=make_cfg(openai_compat_hosted_tools_allowlist="core.web_search"))

    @pytest.mark.asyncio
    async def test_plain_answer_stops_immediately(self, monkeypatch, hosted_env):
        async def _execute(command, *, operation):
            return {"final_response": "done", "stop_reason": "stop"}

        monkeypatch.setattr(execution, "_execute", _execute)
        result = await execution.run_hosted_tools(hosted_env)
        assert result["content"] == "done"
        assert result["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_client_owned_call_returns_to_client(self, monkeypatch, hosted_env):
        async def _execute(command, *, operation):
            return {
                "suspended": True,
                "stop_reason": "suspended",
                "final_response": "",
                "handed_back_tool_calls": [
                    {
                        "tool_call_id": "c1",
                        "tool_name": "client.edit_file",
                        "parameters": {},
                    }
                ],
            }

        monkeypatch.setattr(execution, "_execute", _execute)
        result = await execution.run_hosted_tools(hosted_env)
        assert result["finish_reason"] == "tool_calls"
        assert result["tool_calls_canonical"][0]["tool_name"] == "client.edit_file"

    @pytest.mark.asyncio
    async def test_agent_command_carries_allowlist_and_handback(self, monkeypatch, hosted_env):
        from motet.core.types import CanonicalToolSchema

        hosted_env.tools = [CanonicalToolSchema(name="client.edit_file", json_schema={"type": "object"})]
        captured = {}

        async def _execute(command, *, operation):
            captured["command"] = command
            captured["data"] = command.data
            return {"final_response": "ok", "stop_reason": "stop"}

        monkeypatch.setattr(execution, "_execute", _execute)
        await execution.run_hosted_tools(hosted_env)
        assert captured["command"].get_command_type() == "core.agent_loop"
        data = captured["data"]
        names = []
        for schema in data.tools or []:
            names.append(schema.name if hasattr(schema, "name") else schema.get("name"))
        assert "core.web_search" in names
        assert "client.edit_file" in names
        handback = []
        for schema in data.handback_tools or []:
            handback.append(schema.name if hasattr(schema, "name") else schema.get("name"))
        assert handback == ["client.edit_file"]
        assert data.agent_id == execution.HOSTED_TOOLS_LOOP_ID
        assert data.inject_meta_tools is False
        assert data.max_iterations == hosted_env.cfg.openai_compat_max_tool_iterations
        assert data.tools is not None

    @pytest.mark.asyncio
    async def test_hosted_tools_resumes_matching_checkpoint(self, monkeypatch, hosted_env):
        async def _maybe(ctx):
            return "suspend-abc", ctx.messages, [{"tool_call_id": "c1", "content": "ok"}]

        async def _run_resume(ctx, checkpoint_id, history, observations):
            assert checkpoint_id == "suspend-abc"
            return {"content": "resumed hosted", "finish_reason": "stop"}

        monkeypatch.setattr(execution, "_maybe_resume", _maybe)
        monkeypatch.setattr(execution, "_run_resume", _run_resume)
        result = await execution.run_hosted_tools(hosted_env)
        assert result["content"] == "resumed hosted"

    @pytest.mark.asyncio
    async def test_budget_stop_reports_length(self, monkeypatch, hosted_env):
        async def _execute(command, *, operation):
            return {"final_response": "", "stop_reason": "max_iterations"}

        monkeypatch.setattr(execution, "_execute", _execute)
        result = await execution.run_hosted_tools(hosted_env)
        assert result["finish_reason"] == "length"

    @pytest.mark.asyncio
    async def test_usage_from_loop_payload(self, monkeypatch, hosted_env):
        async def _execute(command, *, operation):
            return {
                "final_response": "done",
                "stop_reason": "stop",
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 7,
                    "total_tokens": 37,
                },
            }

        monkeypatch.setattr(execution, "_execute", _execute)
        result = await execution.run_hosted_tools(hosted_env)
        assert result["prompt_tokens"] == 30
        assert result["completion_tokens"] == 7

    def test_empty_allowlist_does_not_enable_discovery(self):
        ctx = make_ctx(cfg=make_cfg(openai_compat_hosted_tools_allowlist=""))
        data = execution.build_hosted_tools_agent_data(ctx, [])
        assert data.tools == []
        assert data.handback_tools is None
        assert data.agent_id == execution.HOSTED_TOOLS_LOOP_ID
        assert data.inject_meta_tools is False


class TestAgentContext:
    """Agent mode reuses the native chat context contract."""

    def test_context_carries_identity_and_routing(self):
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default")
        context = execution._agent_context(ctx)

        assert context["agent_id"] == "core.default"
        assert context["conversation_id"] == "conv-1"
        assert context["principal_roles"] == ["member"]
        assert context["surface_id"] == "openai_compat"
        assert context["model_provider"] == "openai"
        assert context["model_name"] == "gpt-4o-mini"

    def test_surface_comes_from_agent_allowlist(self, monkeypatch):
        class _Cfg:
            def __init__(self, ids):
                self.allowed_surface_ids = ids

        class _Registry:
            def get(self, key):
                if key == "cursor.backend":
                    return _Cfg(["cursor_ide"])
                return _Cfg(None)

        def _resolve(raw):
            text = (raw or "").strip()
            if text in {"cursor", "cursor.backend"}:
                return "cursor.backend"
            return text if "." in text else f"core.{text}"

        def _allowlist(*, qualified_agent_id, config_allowed_surface_ids=None, **_kwargs):
            if qualified_agent_id == "cursor.backend":
                return ["cursor_ide"]
            return None  # all surfaces → facade default

        monkeypatch.setattr("motet.core.agents.resolve_agent_id", _resolve)
        monkeypatch.setattr("motet.core.agents.get_agent_registry", lambda: _Registry())
        monkeypatch.setattr(
            "motet.core.surfaces.resolve_effective_allowlist",
            _allowlist,
        )

        assert execution._surface_id_for_agent("cursor.backend") == "cursor_ide"
        assert execution._surface_id_for_agent("core.default") == "openai_compat"
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="cursor.backend")
        assert execution._agent_context(ctx)["surface_id"] == "cursor_ide"

    def test_agent_result_shape_is_translatable(self):
        result = execution._agent_result("hello", citations=[{"id": "a"}])
        assert result["content"] == "hello"
        assert result["finish_reason"] == "stop"
        assert result["total_tokens"] == 0

    def test_agent_result_includes_turn_aggregated_usage(self):
        result = execution._agent_result(
            "hello",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "reasoning_tokens": 4,
            },
        )
        assert result["prompt_tokens"] == 10
        assert result["completion_tokens"] == 20
        assert result["total_tokens"] == 30
        assert result["reasoning_tokens"] == 4

    def test_usage_from_agent_response_prefers_raw_usage(self):
        response = SimpleNamespace(
            usage_tokens_input=1,
            usage_tokens_output=2,
            raw={"usage": {"prompt_tokens": 9, "completion_tokens": 8, "total_tokens": 17}},
        )
        assert execution._usage_from_agent_response(response)["total_tokens"] == 17

    @pytest.mark.asyncio
    async def test_stream_agent_maps_end_usage(self, monkeypatch):
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default")
        usage = {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}

        class _Orchestrator:
            async def stream_events(self, *_args, **_kwargs):
                yield {"event": "token", "data": "hi"}
                yield {"event": "end", "content": "hi", "usage": usage}

        monkeypatch.setattr(
            execution,
            "_build_stack",
            lambda _ctx: SimpleNamespace(orchestrator=_Orchestrator()),
        )

        events = []
        async for item in execution.stream_agent(ctx):
            events.append(item)

        assert ("delta", "hi") in events
        result = next(value for kind, value in events if kind == "result")
        assert result["prompt_tokens"] == 5
        assert result["completion_tokens"] == 7
        assert result["total_tokens"] == 12

    @pytest.mark.asyncio
    async def test_stream_agent_forwards_thinking(self, monkeypatch):
        ctx = make_ctx(
            mode=FacadeMode.AGENT,
            agent_id="core.default",
            model_settings={
                "provider": "mock",
                "model_name": "mock-small",
                "enable_thinking": True,
                "reasoning_effort": "medium",
            },
        )

        class _Orchestrator:
            async def stream_events(self, *_args, **_kwargs):
                yield {"event": "thinking", "text": "ponder ", "is_complete": False}
                yield {"event": "thinking", "text": "", "is_complete": True}
                yield {"event": "token", "data": "hi"}
                yield {"event": "end", "content": "hi", "usage": {}}

        monkeypatch.setattr(
            execution,
            "_build_stack",
            lambda _ctx: SimpleNamespace(orchestrator=_Orchestrator()),
        )

        events = []
        async for item in execution.stream_agent(ctx):
            events.append(item)

        thinking = [value for kind, value in events if kind == "thinking"]
        assert thinking[0]["text"] == "ponder "
        assert thinking[0]["is_complete"] is False
        result = next(value for kind, value in events if kind == "result")
        assert result["reasoning_content"] == "ponder "

    def test_agent_context_passes_enable_thinking(self):
        ctx = make_ctx(
            mode=FacadeMode.AGENT,
            agent_id="core.default",
            model_settings={
                "provider": "mock",
                "model_name": "mock-small",
                "enable_thinking": True,
                "reasoning_effort": "high",
            },
        )
        context = execution._agent_context(ctx)
        assert context["enable_thinking"] is True
        assert context["reasoning_effort"] == "high"

    def test_agent_context_omits_thinking_when_not_enabled(self):
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default")
        context = execution._agent_context(ctx)
        assert "enable_thinking" not in context


# ---------------------------------------------------------------------------
# Agent-mode client tools: handback + resume (ADR-0125 §5c.1 / ADR-0127)
# ---------------------------------------------------------------------------

CLIENT_TOOL = {
    "name": "edit_file",
    "description": "Edit a file in the client workspace",
    "json_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
}

HANDED_BACK = [
    {"tool_call_id": "call_1", "tool_name": "edit_file", "parameters": {"path": "a.py"}},
]


def resume_messages() -> List[Message]:
    """A tool-loop continuation: history ending in assistant tool_calls + tool results."""
    return [
        Message(role="user", content="fix the bug"),
        Message(
            role="assistant",
            content="",
            tool_calls_canonical=[{"call_id": "call_1", "tool_name": "edit_file", "arguments_json": "{}"}],
        ),
        Message(role="tool", content="edited ok", tool_call_id="call_1", name="edit_file"),
    ]


class TestHandbackContext:
    """Client-declared tools ride into the agent stack as handback tools."""

    def test_client_tools_become_handback_tools(self):
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default", tools=[dict(CLIENT_TOOL)])
        context = execution._agent_context(ctx)
        assert context["handback_tools"] == [CLIENT_TOOL]

    def test_no_client_tools_means_no_handback_key(self):
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default")
        assert "handback_tools" not in execution._agent_context(ctx)

    def test_disabled_flag_ignores_client_tools(self):
        ctx = make_ctx(
            mode=FacadeMode.AGENT,
            agent_id="core.default",
            tools=[dict(CLIENT_TOOL)],
            cfg=make_cfg(openai_compat_agent_client_tools=False),
        )
        assert "handback_tools" not in execution._agent_context(ctx)


class TestToolCallDeltaGate:
    """Only fragments of client-owned calls may reach the client."""

    def test_client_owned_fragments_are_released(self):
        gate = execution.ToolCallDeltaGate({"Write"})
        first = gate.feed(
            {"call_id": "Write_1", "tool_name": "Write", "arguments_delta": '{"path":'}
        )
        assert first == {
            "call_id": "Write_1",
            "tool_name": "Write",
            "arguments_delta": '{"path":',
            "first": True,
        }
        second = gate.feed(
            {"call_id": "Write_1", "tool_name": "Write", "arguments_delta": '"a.py"}'}
        )
        assert second is not None and second["first"] is False

    def test_motet_owned_call_is_withheld(self):
        """A client that saw this would try to run a tool it does not have."""
        gate = execution.ToolCallDeltaGate({"Write"})
        assert gate.feed(
            {"call_id": "h1", "tool_name": "core.help", "arguments_delta": "{}"}
        ) is None

    def test_fragments_before_the_name_are_buffered_not_dropped(self):
        """Whatever the client assembles must be the whole argument string."""
        gate = execution.ToolCallDeltaGate({"Write"})
        assert gate.feed({"call_id": "Write_1", "arguments_delta": '{"pa'}) is None
        released = gate.feed(
            {"call_id": "Write_1", "tool_name": "Write", "arguments_delta": 'th":"a.py"}'}
        )
        assert released is not None
        assert released["arguments_delta"] == '{"path":"a.py"}'

    def test_no_declared_tools_means_no_forwarding(self):
        gate = execution.ToolCallDeltaGate(set())
        assert gate.feed(
            {"call_id": "c1", "tool_name": "Write", "arguments_delta": "{}"}
        ) is None

    def test_wire_mcp_name_matches_canonical_allowlist(self):
        """Client declared mcp.x.y; Chat Completions deltas still say mcp__x__y."""
        gate = execution.ToolCallDeltaGate({"mcp.google_workspace.list_docs"})
        released = gate.feed(
            {
                "call_id": "call_1",
                "tool_name": "mcp__google_workspace__list_docs",
                "arguments_delta": '{"folder":',
            }
        )
        assert released is not None
        assert released["first"] is True
        assert released["arguments_delta"] == '{"folder":'
        # Frame keeps the wire name the provider emitted; only the check is canonical.
        assert released["tool_name"] == "mcp__google_workspace__list_docs"

    def test_declared_names_read_from_dict_and_model_tools(self):
        ctx = make_ctx(mode=FacadeMode.AGENT, tools=[dict(CLIENT_TOOL)])
        assert execution._declared_tool_names(ctx) == {CLIENT_TOOL["name"]}


class TestSuspendedAgentResult:
    """A suspended turn renders as a standard OpenAI tool_calls turn."""

    def test_handed_back_calls_become_tool_calls(self):
        result = execution._suspended_agent_result(
            content="on it",
            handed_back=HANDED_BACK,
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        )
        assert result["finish_reason"] == "tool_calls"
        assert result["content"] == "on it"
        assert result["total_tokens"] == 12
        call = result["tool_calls_canonical"][0]
        assert call["call_id"] == "call_1"
        assert call["tool_name"] == "edit_file"
        assert call["arguments_json"] == '{"path": "a.py"}'

    @pytest.mark.asyncio
    async def test_run_agent_maps_suspended_chat_response(self, monkeypatch):
        response = SimpleNamespace(
            content="checking...",
            citations=None,
            raw={
                "suspended": True,
                "handed_back_tool_calls": HANDED_BACK,
                "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
            },
        )

        class _Stack:
            async def chat(self, *_args, **_kwargs):
                return response

        monkeypatch.setattr(execution, "_build_stack", lambda _ctx: _Stack())
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default", tools=[dict(CLIENT_TOOL)])

        result = await execution.run_agent(ctx)

        assert result["finish_reason"] == "tool_calls"
        assert result["tool_calls_canonical"][0]["tool_name"] == "edit_file"
        assert result["total_tokens"] == 5

    @pytest.mark.asyncio
    async def test_stream_agent_maps_suspended_event(self, monkeypatch):
        class _Orchestrator:
            async def stream_events(self, *_args, **_kwargs):
                yield {"event": "token", "data": "let me "}
                yield {
                    "event": "suspended",
                    "content": "let me edit that",
                    "checkpoint_id": "suspend-abc",
                    "handed_back_tool_calls": HANDED_BACK,
                    "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
                }

        monkeypatch.setattr(
            execution,
            "_build_stack",
            lambda _ctx: SimpleNamespace(orchestrator=_Orchestrator()),
        )
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default", tools=[dict(CLIENT_TOOL)])

        events = []
        async for item in execution.stream_agent(ctx):
            events.append(item)

        result = next(value for kind, value in events if kind == "result")
        assert result["finish_reason"] == "tool_calls"
        assert result["tool_calls_canonical"][0]["call_id"] == "call_1"
        assert result["content"] == "let me edit that"


class TestResumeDetection:
    """Trailing role=tool messages resolve to a suspended-turn checkpoint."""

    def test_split_extracts_history_and_observations(self):
        split = execution._split_trailing_observations(resume_messages())
        assert split is not None
        history, observations = split
        assert [m.role for m in history] == ["user", "assistant"]
        assert observations == [{"tool_call_id": "call_1", "content": "edited ok"}]

    def test_normal_turn_is_not_a_resume(self):
        assert execution._split_trailing_observations([Message(role="user", content="hi")]) is None

    def test_trailing_tool_without_assistant_tool_calls_is_not_a_resume(self):
        messages = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="answer"),
            Message(role="tool", content="stray", tool_call_id="call_x"),
        ]
        assert execution._split_trailing_observations(messages) is None

    def test_tool_result_without_id_is_not_a_resume(self):
        messages = resume_messages()
        messages.append(Message(role="tool", content="no id"))
        assert execution._split_trailing_observations(messages) is None

    @pytest.mark.asyncio
    async def test_matching_checkpoint_triggers_resume(self, monkeypatch):
        class _Checkpoint:
            checkpoint_id = "suspend-abc"
            conversation_id = "openai-seed-conv"

        async def _to_thread(fn, *args, **kwargs):
            return _Checkpoint()

        monkeypatch.setattr(execution.asyncio, "to_thread", _to_thread)
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default", messages=resume_messages())

        resume = await execution._maybe_resume(ctx)

        assert resume is not None
        checkpoint_id, history, observations = resume
        assert checkpoint_id == "suspend-abc"
        assert history[-1].role == "assistant"
        assert observations[0]["tool_call_id"] == "call_1"

    def test_rebind_ctx_conversation(self):
        """Tool-result POSTs that mint a fresh id must adopt the suspend conversation."""
        ctx = make_ctx(
            mode=FacadeMode.AGENT,
            agent_id="core.default",
            messages=resume_messages(),
            conversation_id="openai-freshly-minted",
        )
        bound = execution._rebind_ctx_conversation(
            ctx, checkpoint_id="suspend-abc", conversation_id="openai-seed-conv"
        )
        assert bound == "openai-seed-conv"
        assert ctx.conversation_id == "openai-seed-conv"

    @pytest.mark.asyncio
    async def test_matching_checkpoint_rebinds_ctx_before_return(self, monkeypatch):
        class _Checkpoint:
            checkpoint_id = "suspend-abc"
            conversation_id = "openai-seed-conv"

        async def _to_thread(fn, *args, **kwargs):
            return _Checkpoint()

        monkeypatch.setattr(execution.asyncio, "to_thread", _to_thread)
        ctx = make_ctx(
            mode=FacadeMode.AGENT,
            agent_id="core.default",
            messages=resume_messages(),
            conversation_id="openai-freshly-minted",
        )
        resume = await execution._maybe_resume(ctx)
        assert resume is not None
        assert ctx.conversation_id == "openai-seed-conv"

    @pytest.mark.asyncio
    async def test_unknown_tool_call_ids_fall_through_to_fresh_turn(self, monkeypatch):
        async def _to_thread(fn, *args, **kwargs):
            return None

        monkeypatch.setattr(execution.asyncio, "to_thread", _to_thread)
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default", messages=resume_messages())
        assert await execution._maybe_resume(ctx) is None

    @pytest.mark.asyncio
    async def test_client_tools_flag_does_not_skip_resume_lookup(self, monkeypatch):
        """Hosted mixed-turn resume must work even when agent client tools are off."""

        class _Handle:
            checkpoint_id = "suspend-abc"
            conversation_id = "openai-seed-conv"

        async def _to_thread(fn, *args, **kwargs):
            return _Handle()

        monkeypatch.setattr(execution.asyncio, "to_thread", _to_thread)
        ctx = make_ctx(
            mode=FacadeMode.AGENT,
            agent_id="core.default",
            messages=resume_messages(),
            cfg=make_cfg(openai_compat_agent_client_tools=False),
        )
        resume = await execution._maybe_resume(ctx)
        assert resume is not None
        assert resume[0] == "suspend-abc"


class TestResumeExecution:
    """Resume outcomes and errors map to OpenAI wire behavior."""

    def _resume_env(self, monkeypatch, loop_result: Dict[str, Any]):
        async def _maybe(ctx):
            history, observations = execution._split_trailing_observations(ctx.messages)
            return "suspend-abc", history, observations

        captured: Dict[str, Any] = {}

        def _command(ctx, checkpoint_id, history, observations):
            captured.update(
                checkpoint_id=checkpoint_id, history=history, observations=observations
            )
            return "resume-command"

        async def _execute(command, *, operation):
            return loop_result

        monkeypatch.setattr(execution, "_maybe_resume", _maybe)
        monkeypatch.setattr(execution, "_resume_command", _command)
        monkeypatch.setattr(execution, "_execute", _execute)
        return captured

    @pytest.mark.asyncio
    async def test_completed_resume_returns_final_answer(self, monkeypatch):
        captured = self._resume_env(
            monkeypatch,
            {
                "final_response": "bug fixed",
                "stop_reason": "stop",
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            },
        )
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default", messages=resume_messages())

        result = await execution.run_agent(ctx)

        assert result["content"] == "bug fixed"
        assert result["finish_reason"] == "stop"
        assert result["total_tokens"] == 11
        assert captured["checkpoint_id"] == "suspend-abc"
        assert captured["observations"] == [{"tool_call_id": "call_1", "content": "edited ok"}]

    @pytest.mark.asyncio
    async def test_resuspended_resume_hands_back_again(self, monkeypatch):
        self._resume_env(
            monkeypatch,
            {
                "final_response": "",
                "stop_reason": "suspended",
                "handed_back_tool_calls": [
                    {"tool_call_id": "call_2", "tool_name": "edit_file", "parameters": {}}
                ],
            },
        )
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default", messages=resume_messages())

        result = await execution.run_agent(ctx)

        assert result["finish_reason"] == "tool_calls"
        assert result["tool_calls_canonical"][0]["call_id"] == "call_2"

    @pytest.mark.asyncio
    async def test_wire_usage_is_per_request_not_turn_cumulative(self, monkeypatch):
        """Cursor reads usage as this call's cost and budgets its context off it.

        The loop's `usage` is the whole turn (accumulator seeded from the
        checkpoint); reporting it verbatim told the client one response cost
        millions of tokens, which triggered transcript summarization every turn.
        """
        self._resume_env(
            monkeypatch,
            {
                "final_response": "bug fixed",
                "stop_reason": "stop",
                "usage": {
                    "prompt_tokens": 3_977_284,
                    "completion_tokens": 17_044,
                    "total_tokens": 3_994_328,
                },
                "usage_this_request": {
                    "prompt_tokens": 48_000,
                    "completion_tokens": 500,
                    "total_tokens": 48_500,
                },
            },
        )
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default", messages=resume_messages())

        result = await execution.run_agent(ctx)

        assert result["prompt_tokens"] == 48_000
        assert result["total_tokens"] == 48_500

    @pytest.mark.asyncio
    async def test_wire_usage_falls_back_to_loop_total_when_delta_missing(self, monkeypatch):
        """An older worker's result predates the delta; a wrong number beats none."""
        self._resume_env(
            monkeypatch,
            {
                "final_response": "bug fixed",
                "stop_reason": "stop",
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            },
        )
        ctx = make_ctx(mode=FacadeMode.AGENT, agent_id="core.default", messages=resume_messages())

        result = await execution.run_agent(ctx)

        assert result["total_tokens"] == 11

    @pytest.mark.parametrize(
        "message,status,code",
        [
            (
                "resume_turn: checkpoint 'x' belongs to a different principal",
                404,
                "checkpoint_not_found",
            ),
            ("resume_turn: checkpoint 'x' not found or expired", 404, "checkpoint_not_found"),
            (
                "resume_turn: observation for unknown tool_call_id 'call_evil'",
                400,
                "invalid_tool_observations",
            ),
        ],
    )
    def test_resume_errors_map_to_client_errors(self, message, status, code):
        mapped = execution._map_resume_error(FacadeError(502, message))
        assert mapped.status_code == status
        assert mapped.code == code

    def test_unrelated_errors_pass_through(self):
        original = FacadeError(502, "worker pool exhausted")
        assert execution._map_resume_error(original) is original
