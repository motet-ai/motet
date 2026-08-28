"""
Motet - unit tests for turn/hooks.py (#147)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-22

Description:
    Focused unit tests for extracted agent_turn hook helpers, including
    after_finalize fail-soft export hooks.

Dependencies:
    - pytest
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

from motet.core.orchestration.turn.hooks import (
    _leading_system_insert_at,
    resolve_analysis_routing,
    run_after_finalize_hooks,
    run_finalize_hook,
    run_pre_reasoning_hooks,
)
from motet.core.types import Message


class _FakeMotet:
    def __init__(self) -> None:
        self.events: List[Tuple[str, Dict[str, Any]]] = []
        self.maybe_calls: List[str] = []
        self.maybe_data: Dict[str, Any] = {}
        self.dispatch_calls = 0

    def stream_event(self, event_type: str, **payload: Any) -> None:
        self.events.append((event_type, payload))

    def maybe(self, command_fn: Any, data: Any):
        name = getattr(command_fn, "__name__", str(command_fn))
        self.maybe_calls.append(name)
        self.maybe_data[name] = data
        if name == "conversation_analysis":
            return ({"intent": {"strategy_hint": "react"}, "metadata": {}}, None)
        if name == "prepare_context":
            return (
                {
                    "prepared_messages": list(data.messages),
                    "context_info": {"k": "v"},
                },
                None,
            )
        if name == "finalize_turn":
            return ({"ok": True}, None)
        return ({}, None)

    def dispatch(self, _commands: Any) -> List[str]:
        self.dispatch_calls += 1
        return []


def _extract_analysis_metadata(analysis_data: Dict[str, Any]) -> Dict[str, Any]:
    return {"strategy_hint": analysis_data.get("intent", {}).get("strategy_hint")}


def test_leading_system_insert_at() -> None:
    history = [
        Message(role="system", content="a"),
        Message(role="system", content="b"),
        Message(role="user", content="hi"),
    ]
    assert _leading_system_insert_at(history) == 2
    assert _leading_system_insert_at([Message(role="user", content="hi")]) == 0


def test_run_pre_reasoning_hooks_runs_analysis_and_prepare() -> None:
    motet = _FakeMotet()
    history = [Message(role="user", content="help")]
    pending = SimpleNamespace(routing_hint=None, marker=None, status=None, reply=None)
    turn_hooks = SimpleNamespace(
        conversation_analysis="core.conversation_analysis",
        context_prepare="core.prepare_context",
        memory_reset=None,
        context_inject=None,
    )
    agent_config = SimpleNamespace(skill_ids=[], skill_mode="allowlist", skill_max_per_turn=3)
    effective_context: Dict[str, Any] = {}

    class ConversationAnalysisData:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    prep = run_pre_reasoning_hooks(
        motet=motet,
        turn_hooks=turn_hooks,
        history=history,
        pending=pending,
        effective_context=effective_context,
        agent_config=agent_config,
        input_text="help",
        resolved_tools=[],
        schema_exporter=SimpleNamespace(),
        protected_system_prefix=[],
        conversation_analysis=SimpleNamespace(__name__="conversation_analysis"),
        ConversationAnalysisData=ConversationAnalysisData,
        memory_reset=SimpleNamespace(__name__="memory_reset"),
        prepare_context=SimpleNamespace(__name__="prepare_context"),
        extract_analysis_metadata=_extract_analysis_metadata,
        build_pending_action_system_message=lambda *a, **k: "pending",
    )

    assert "conversation_analysis" in motet.maybe_calls
    assert "prepare_context" in motet.maybe_calls
    assert prep.analysis_metadata["strategy_hint"] == "react"
    assert prep.prepared_context_info == {"k": "v"}
    assert any(et == "conversation_analyzed" for et, _ in motet.events)


def test_resolve_analysis_routing_inherits_turn_when_unset() -> None:
    provider, model = resolve_analysis_routing(
        turn_provider="xai",
        turn_model="grok-4.5",
        config=SimpleNamespace(analysis_model=None, analysis_provider=None),
    )
    assert provider == "xai"
    assert model == "grok-4.5"


def test_resolve_analysis_routing_model_pin_keeps_turn_provider() -> None:
    provider, model = resolve_analysis_routing(
        turn_provider="xai",
        turn_model="grok-4.5",
        config=SimpleNamespace(analysis_model="gpt-4o-mini", analysis_provider=None),
    )
    assert provider == "xai"
    assert model == "gpt-4o-mini"


def test_resolve_analysis_routing_full_cheap_pin() -> None:
    provider, model = resolve_analysis_routing(
        turn_provider="xai",
        turn_model="grok-4.5",
        config=SimpleNamespace(
            analysis_model="gpt-4o-mini",
            analysis_provider="openai",
        ),
    )
    assert provider == "openai"
    assert model == "gpt-4o-mini"


def test_resolve_analysis_routing_command_pin_wins() -> None:
    provider, model = resolve_analysis_routing(
        turn_provider="xai",
        turn_model="grok-4.5",
        analysis_provider="anthropic",
        analysis_model="claude-haiku-4-5",
        config=SimpleNamespace(
            analysis_model="gpt-4o-mini",
            analysis_provider="openai",
        ),
    )
    assert provider == "anthropic"
    assert model == "claude-haiku-4-5"


def test_run_pre_reasoning_hooks_pins_turn_model(monkeypatch) -> None:
    monkeypatch.setattr(
        "motet.core.config.Config",
        lambda: SimpleNamespace(analysis_model=None, analysis_provider=None),
    )
    motet = _FakeMotet()
    history = [Message(role="user", content="help")]
    pending = SimpleNamespace(routing_hint=None, marker=None, status=None, reply=None)
    turn_hooks = SimpleNamespace(
        conversation_analysis="core.conversation_analysis",
        context_prepare=None,
        memory_reset=None,
        context_inject=None,
    )

    class ConversationAnalysisData:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    run_pre_reasoning_hooks(
        motet=motet,
        turn_hooks=turn_hooks,
        history=history,
        pending=pending,
        effective_context={},
        agent_config=SimpleNamespace(
            skill_ids=[], skill_mode="allowlist", skill_max_per_turn=3
        ),
        input_text="help",
        resolved_tools=[],
        schema_exporter=SimpleNamespace(),
        protected_system_prefix=[],
        conversation_analysis=SimpleNamespace(__name__="conversation_analysis"),
        ConversationAnalysisData=ConversationAnalysisData,
        memory_reset=SimpleNamespace(__name__="memory_reset"),
        prepare_context=SimpleNamespace(__name__="prepare_context"),
        extract_analysis_metadata=_extract_analysis_metadata,
        build_pending_action_system_message=lambda *a, **k: "pending",
        model_provider="xai",
        model_name="grok-4.5",
    )

    data = motet.maybe_data["conversation_analysis"]
    assert data.analysis_provider == "xai"
    assert data.analysis_model == "grok-4.5"


def test_run_finalize_hook_skips_when_unconfigured() -> None:
    motet = _FakeMotet()
    run_finalize_hook(
        motet=motet,
        turn_hooks=SimpleNamespace(finalize=None),
        finalize_turn=SimpleNamespace(__name__="finalize_turn"),
        history=[],
        final_response="ok",
        qualified_id="core.default",
        finalize_root_turn=True,
        finalize_root_agent_id="core.default",
        reserve_sequence=1,
        pending_action_carry=None,
    )
    assert motet.maybe_calls == []


def test_run_after_finalize_hooks_noop_when_unset() -> None:
    motet = _FakeMotet()
    run_after_finalize_hooks(
        motet=motet,
        turn_hooks=SimpleNamespace(after_finalize=None),
        history=[Message(role="user", content="hi")],
        final_response="ok",
        qualified_id="langfuse-cms.prompt-manager",
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )
    assert motet.maybe_calls == []


def test_run_after_finalize_hooks_invokes_registered_command(monkeypatch) -> None:
    motet = _FakeMotet()
    seen: Dict[str, Any] = {}

    class _Impl:
        __name__ = "record_turn_to_langfuse"

        def __call__(self, data: Any = None, **kwargs: Any) -> Dict[str, Any]:
            return {"ok": True}

    class _Reg:
        implementation = _Impl()

    def _fake_create(name: str, **kwargs: Any) -> Any:
        seen["name"] = name
        seen["kwargs"] = kwargs
        return SimpleNamespace(**kwargs)

    import motet.core.commands.command_data_classes as cdc
    from motet.core.commands.command_type_registry import command_type_registry

    def _fake_get(name: str) -> Any:
        if name == "langfuse-cms.record_turn_to_langfuse":
            return _Reg()
        return None

    monkeypatch.setattr(command_type_registry, "get", _fake_get)
    monkeypatch.setattr(cdc, "create_command_data", _fake_create)

    run_after_finalize_hooks(
        motet=motet,
        turn_hooks=SimpleNamespace(
            after_finalize=["langfuse-cms.record_turn_to_langfuse"]
        ),
        history=[Message(role="user", content="hi")],
        final_response="hello",
        qualified_id="langfuse-cms.prompt-manager",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        cost_usd=0.001,
        model="openai/gpt-4o-mini",
        context={"langfuse_prompt_source": "langfuse"},
    )

    assert "record_turn_to_langfuse" in motet.maybe_calls
    assert seen["name"] == "langfuse-cms.record_turn_to_langfuse"
    assert seen["kwargs"]["assistant_response"] == "hello"
    assert seen["kwargs"]["usage"]["prompt_tokens"] == 10
    assert seen["kwargs"]["context"]["langfuse_prompt_source"] == "langfuse"
