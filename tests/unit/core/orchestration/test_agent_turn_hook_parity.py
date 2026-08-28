"""
Motet - Agent Turn Hook Parity Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
    Last Modified: 2026-08-22

Description:
Focused unit tests that validate `core.agent_turn` lifecycle parity for
ADR-0078 hook execution. These tests confirm the default hook mapping uses
`core.prepare_context` and `core.finalize_turn`, and that turn state events are
emitted in the expected PREPARING -> THINKING -> RESPONDING -> COMPLETING order.

Dependencies:
- pytest: test runner and fixtures
- motet.core.orchestration.turn.agent_turn: command under test
- motet.core.agents.registry: agent config models used in setup

Usage:
Run this module directly with pytest:
    pytest tests/unit/core/orchestration/test_agent_turn_hook_parity.py -q

Notes:
- Tests patch agent registry/tool resolution entry points to keep scope narrow.
- A lightweight fake Motet context is used to capture hook and stream behavior
  without requiring distributed worker infrastructure.
"""

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from motet.core.agents.registry import AgentConfig, ToolFilter, TurnHooks
from motet.core.commands.command_data_classes import AgentTurnData
from motet.core.orchestration.turn import agent_turn
# Patch point: `phases` owns the single get_motet_context binding, and
# agent_turn resolves through it at call time, so patching here redirects both.
from motet.core.orchestration.turn import phases as phases_module
from motet.core.types import Message


class _FakeMotet:
    """Minimal MotetContext test double for agent_turn parity checks."""

    def __init__(self) -> None:
        self.task_id = "task-1"
        self.conversation_id = None
        self.tenant_id = "tenant-1"
        self.principal_id = "principal-1"
        self.motet_id = "motet-1"
        self.command_id = "cmd-1"
        self.trace_id = "trace-1"
        self.metadata: Dict[str, Any] = {}
        self.stack = SimpleNamespace(config=SimpleNamespace())
        self.tools = object()
        self.redis = _FakeRedis()

        self.events: List[Tuple[str, Dict[str, Any]]] = []
        self.maybe_calls: List[Tuple[str, Any]] = []
        self.do_calls: List[Tuple[str, Any]] = []
        self.agent_calls: List[Any] = []
        self.finalize_payload: Optional[Any] = None

    def ensure_stream(self, ttl_seconds: int) -> None:
        _ = ttl_seconds

    def stream_event(self, event_type: str, **payload: Any) -> None:
        self.events.append((event_type, payload))

    def observe_events(self, **kwargs: Any):  # noqa: ANN003 - test helper
        _ = kwargs
        return nullcontext()

    def dispatch(self, _commands: Any) -> List[str]:  # noqa: ANN401 - test helper
        return []

    def maybe(self, command_fn: Any, data: Any):  # noqa: ANN401 - test helper
        command_name = getattr(command_fn, "__name__", str(command_fn))
        self.maybe_calls.append((command_name, data))

        if command_name == "conversation_analysis":
            return (
                {
                    "intent": {"primary": "question", "confidence": 0.9, "strategy_hint": "react"},
                    "complexity": {"primary": "simple"},
                    "context": {"needs_clarification": False},
                    "tone": {"emotion": "neutral"},
                    "metadata": {"analysis_mode": "full"},
                },
                None,
            )

        if command_name == "prepare_context":
            return (
                {
                    "prepared_messages": data.messages,
                    "context_info": {
                        "artifact_rag_citations": [
                            {
                                "citation_id": "A1",
                                "source_label": "sample.pdf",
                                "artifact_id": "source-1",
                            }
                        ]
                    },
                },
                None,
            )

        if command_name == "finalize_turn":
            self.finalize_payload = data
            return ({"memory_updated": True}, None)

        return ({}, None)

    def do(self, command_fn: Any, data: Any):  # noqa: ANN401 - test helper
        command_name = getattr(command_fn, "__name__", str(command_fn))
        self.do_calls.append((command_name, data))
        return {}


class _FakeRedis:
    """Redis subset needed by transcript sequence allocation."""

    def __init__(self) -> None:
        self.values: Dict[str, int] = {}
        self.expirations: Dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key: str, seconds: int) -> bool:
        self.expirations[key] = seconds
        return True


def _install_agent_stub(
    monkeypatch: Any,
    fake_motet: _FakeMotet,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    payload = result or {
        "final_response": "final answer",
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 22,
            "total_tokens": 33,
        },
    }

    def _run(_motet: Any, data: Any) -> Dict[str, Any]:
        fake_motet.agent_calls.append(data)
        return payload

    monkeypatch.setattr(
        "motet.core.reasoning.react.run_agent",
        _run,
    )


def _make_agent_config() -> AgentConfig:
    return AgentConfig(
        agent_id="default",
        aliases=["agent", "default"],
        display_name="Motet Agent",
        description="General-purpose agent with full tool discovery.",
        allowed_roles=["*"],
        system_prompt="You are a helpful assistant.",
        tool_filter=ToolFilter(mode="discovery"),
        turn_hooks=TurnHooks(
            conversation_analysis="core.conversation_analysis",
            context_prepare="core.prepare_context",
            finalize="core.finalize_turn",
        ),
        bundle_id=None,
    )


def test_agent_turn_uses_prepare_and_finalize_hooks(monkeypatch):
    """agent_turn runs prepare_context before reasoning and finalize_turn after."""
    fake_motet = _FakeMotet()
    agent_config = _make_agent_config()
    fake_registry = SimpleNamespace(get=lambda _qid: agent_config)

    monkeypatch.setattr(phases_module, "get_motet_context", lambda: fake_motet)
    monkeypatch.setattr("motet.core.agents.resolve_agent_id", lambda _raw: "core.default")
    monkeypatch.setattr("motet.core.agents.get_agent_registry", lambda: fake_registry)
    monkeypatch.setattr("motet.core.agents.resolve_tools", lambda *args, **kwargs: [])
    _install_agent_stub(monkeypatch, fake_motet)

    result = agent_turn.__wrapped__(
        data=AgentTurnData(
            agent_id="default",
            messages=[Message(role="user", content="help me")],
            context={},
        )
    )

    maybe_names = [name for name, _ in fake_motet.maybe_calls]
    assert "prepare_context" in maybe_names
    assert "finalize_turn" in maybe_names
    assert "context_prepare" not in maybe_names
    assert "finalize" not in maybe_names

    assert fake_motet.finalize_payload is not None
    assert fake_motet.finalize_payload.assistant_response == "final answer"
    assert result["final_response"] == "final answer"


def test_agent_turn_emits_turn_states_in_expected_order(monkeypatch):
    """agent_turn emits PREPARING, THINKING, RESPONDING, COMPLETING in order."""
    fake_motet = _FakeMotet()
    agent_config = _make_agent_config()
    fake_registry = SimpleNamespace(get=lambda _qid: agent_config)

    monkeypatch.setattr(phases_module, "get_motet_context", lambda: fake_motet)
    monkeypatch.setattr("motet.core.agents.resolve_agent_id", lambda _raw: "core.default")
    monkeypatch.setattr("motet.core.agents.get_agent_registry", lambda: fake_registry)
    monkeypatch.setattr("motet.core.agents.resolve_tools", lambda *args, **kwargs: [])
    _install_agent_stub(monkeypatch, fake_motet)

    agent_turn.__wrapped__(
        data=AgentTurnData(
            agent_id="default",
            messages=[Message(role="user", content="what next?")],
            context={},
        )
    )

    turn_states = [payload.get("state") for event_type, payload in fake_motet.events if event_type == "turn"]
    assert turn_states == ["PREPARING", "THINKING", "RESPONDING", "COMPLETING"]

    start_events = [payload for event_type, payload in fake_motet.events if event_type == "start"]
    assert len(start_events) == 1
    assert start_events[0].get("command_type") == "agent_turn"

    event_types = [event_type for event_type, _ in fake_motet.events]
    assert event_types[0] == "start"
    assert event_types[-1] == "end"
    end_payload = fake_motet.events[-1][1]
    assert end_payload["artifact_rag_citations"] == [
        {
            "citation_id": "A1",
            "source_label": "sample.pdf",
            "artifact_id": "source-1",
        }
    ]
    assert end_payload["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 22,
        "total_tokens": 33,
    }


def test_agent_turn_preserves_multimodal_message_fields(monkeypatch):
    """agent_turn preserves content_parts and attachments through prepare_context."""
    fake_motet = _FakeMotet()
    agent_config = _make_agent_config()
    fake_registry = SimpleNamespace(get=lambda _qid: agent_config)

    monkeypatch.setattr(phases_module, "get_motet_context", lambda: fake_motet)
    monkeypatch.setattr("motet.core.agents.resolve_agent_id", lambda _raw: "core.default")
    monkeypatch.setattr("motet.core.agents.get_agent_registry", lambda: fake_registry)
    monkeypatch.setattr("motet.core.agents.resolve_tools", lambda *args, **kwargs: [])
    _install_agent_stub(monkeypatch, fake_motet)

    agent_turn.__wrapped__(
        data=AgentTurnData(
            agent_id="default",
            messages=[
                {
                    "role": "user",
                    "content": "what does this say?",
                    "content_parts": [
                        {"type": "text", "text": "what does this say?"},
                        {"type": "text", "text": "--- Page 1 (OCR) --- example"},
                    ],
                    "attachments": [
                        {
                            "artifact_id": "artifact-123",
                            "filename": "doc.pdf",
                            "content_type": "application/pdf",
                            "bytes": 12345,
                        }
                    ],
                }
            ],
            context={},
        )
    )

    agent_calls = fake_motet.agent_calls
    assert agent_calls, "Expected run_agent to be invoked"
    agent_data = agent_calls[0]

    history = list(getattr(agent_data, "conversation_history", None) or [])
    user_messages = [m for m in history if getattr(m, "role", "") == "user"]
    assert user_messages, "Expected user message in run_agent history"
    latest_user = user_messages[-1]
    assert getattr(latest_user, "content_parts", None), "content_parts should be preserved"
    assert getattr(latest_user, "attachments", None), "attachments should be preserved"


def test_agent_turn_extracts_loop_response(monkeypatch):
    """agent_turn extracts text from the agent loop result."""
    fake_motet = _FakeMotet()
    agent_config = _make_agent_config()
    fake_registry = SimpleNamespace(get=lambda _qid: agent_config)

    monkeypatch.setattr(phases_module, "get_motet_context", lambda: fake_motet)
    monkeypatch.setattr("motet.core.agents.resolve_agent_id", lambda _raw: "core.default")
    monkeypatch.setattr("motet.core.agents.get_agent_registry", lambda: fake_registry)
    monkeypatch.setattr("motet.core.agents.resolve_tools", lambda *args, **kwargs: [])
    _install_agent_stub(
        monkeypatch,
        fake_motet,
        result={"final_response": "nested final answer"},
    )

    result = agent_turn.__wrapped__(
        data=AgentTurnData(
            agent_id="default",
            messages=[Message(role="user", content="help me")],
            context={},
        )
    )

    assert result["final_response"] == "nested final answer"
