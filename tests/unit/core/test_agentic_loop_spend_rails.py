"""
Motet - Agentic Loop Spend-Rail Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Unit coverage for the dollar, prompt-token, and tool-time ceilings.
    A live 20-iteration research turn spent $0.42 and 609k prompt tokens
    before max_iterations fired; spawn children also burned minutes of
    Playwright join time. These rails stop that earlier and do not write
    a Continue checkpoint. Tool time is 0 (off) unless AgentData sets it.

Usage:
    pytest tests/unit/core/test_agentic_loop_spend_rails.py
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

from motet.core.reasoning.react.agent import _resolve_spend_rails
from motet.core.reasoning.react.agent_data import AgentData
from motet.core.reasoning.react.agentic_loop import _maybe_stop_for_spend
from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData


class _FakeMotet:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def stream_event(self, name: str, **fields: Any) -> None:
        self.events.append({"name": name, **fields})


def _loop_data(
    *,
    max_cost_usd: float = 0.0,
    max_prompt_tokens: int = 0,
    max_tool_time_ms: int = 0,
) -> AgenticLoopData:
    return AgenticLoopData(
        input="research this",
        conversation_history=[],
        max_cost_usd=max_cost_usd,
        max_prompt_tokens=max_prompt_tokens,
        max_tool_time_ms=max_tool_time_ms,
        stream_key="task:t:response",
    )


def test_cost_ceiling_stops_without_a_continue_invitation() -> None:
    motet = _FakeMotet()
    result = _maybe_stop_for_spend(
        motet,
        _loop_data(max_cost_usd=0.50),
        iterations_used=8,
        accumulated_usage={"cost_usd": 0.51, "prompt_tokens": 10_000},
        accumulated_media=[],
    )

    assert result is not None
    assert result["stop_reason"] == "max_cost"
    assert "cost ceiling" in result["final_response"]
    assert "Please continue to keep working" not in result["final_response"]
    assert motet.events[0]["reason"] == "max_cost"


def test_prompt_token_ceiling_stops_the_turn() -> None:
    motet = _FakeMotet()
    result = _maybe_stop_for_spend(
        motet,
        _loop_data(max_prompt_tokens=200_000),
        iterations_used=10,
        accumulated_usage={"cost_usd": 0.20, "prompt_tokens": 200_000},
        accumulated_media=[],
    )

    assert result is not None
    assert result["stop_reason"] == "max_prompt_tokens"
    assert "200000" in result["final_response"]


def test_tool_time_ceiling_stops_the_turn() -> None:
    motet = _FakeMotet()
    result = _maybe_stop_for_spend(
        motet,
        _loop_data(max_tool_time_ms=60_000),
        iterations_used=4,
        accumulated_usage={"cost_usd": 0.05, "prompt_tokens": 8_000, "tool_time_ms": 60_000},
        accumulated_media=[],
    )

    assert result is not None
    assert result["stop_reason"] == "max_tool_time"
    assert "60.0s" in result["final_response"]
    assert "Please continue to keep working" not in result["final_response"]
    assert motet.events[0]["reason"] == "max_tool_time"


def test_tool_time_under_the_ceiling_continues() -> None:
    motet = _FakeMotet()
    result = _maybe_stop_for_spend(
        motet,
        _loop_data(max_tool_time_ms=60_000),
        iterations_used=2,
        accumulated_usage={"cost_usd": 0.05, "prompt_tokens": 8_000, "tool_time_ms": 59_999},
        accumulated_media=[],
    )
    assert result is None


def test_zero_disables_a_rail() -> None:
    motet = _FakeMotet()
    result = _maybe_stop_for_spend(
        motet,
        _loop_data(max_cost_usd=0.0, max_prompt_tokens=0, max_tool_time_ms=0),
        iterations_used=20,
        accumulated_usage={
            "cost_usd": 9.99,
            "prompt_tokens": 2_000_000,
            "tool_time_ms": 600_000,
        },
        accumulated_media=[],
    )
    assert result is None


def test_under_the_ceiling_continues() -> None:
    motet = _FakeMotet()
    result = _maybe_stop_for_spend(
        motet,
        _loop_data(max_cost_usd=0.75, max_prompt_tokens=200_000),
        iterations_used=3,
        accumulated_usage={"cost_usd": 0.10, "prompt_tokens": 20_000},
        accumulated_media=[],
    )
    assert result is None


def test_resolve_spend_rails_inherits_config_when_unset() -> None:
    with patch(
        "motet.core.config.Config",
        return_value=type("C", (), {"agent_max_cost_usd": 0.75, "agent_max_prompt_tokens": 200000})(),
    ):
        cost, tokens, tool_time = _resolve_spend_rails(
            AgentData(agent_id="core.default", input="hi")
        )
    assert cost == 0.75
    assert tokens == 200000
    assert tool_time == 0


def test_resolve_spend_rails_explicit_zero_disables() -> None:
    cost, tokens, tool_time = _resolve_spend_rails(
        AgentData(
            agent_id="core.default",
            input="hi",
            max_cost_usd=0.0,
            max_prompt_tokens=0,
            max_tool_time_ms=0,
        )
    )
    assert cost == 0.0
    assert tokens == 0
    assert tool_time == 0


def test_resolve_spend_rails_spawn_sets_tool_time() -> None:
    cost, tokens, tool_time = _resolve_spend_rails(
        AgentData(
            agent_id="core.default.spawn-1",
            input="hi",
            max_cost_usd=0.20,
            max_prompt_tokens=80_000,
            max_tool_time_ms=60_000,
        )
    )
    assert cost == 0.20
    assert tokens == 80_000
    assert tool_time == 60_000
