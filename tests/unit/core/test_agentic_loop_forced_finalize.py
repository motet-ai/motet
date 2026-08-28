"""
Motet - Agentic Loop Forced-Finalize Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Unit coverage for the tools-off write-up the loop issues after a rail
    stop. A budget stop used to return scaffolding; finalize turns that into
    findings while leaving stop_reason as the rail so parent Continue still
    applies.

Usage:
    pytest tests/unit/core/test_agentic_loop_forced_finalize.py
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from motet.core.commands.response_models import CommandExecutionError
from motet.core.reasoning.react.agentic_loop import (
    BUDGET_FINALIZE_PREFIX,
    RAIL_FINALIZE_REASONS,
    _forced_finalize_message,
    _try_finalize_writeup,
)
from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData
from motet.core.reasoning.react.loop_results import build_loop_result
from motet.core.types import Message


class _FakeMotet:
    def __init__(self, writeup: str = "RDS on-demand is $0.12/hour.") -> None:
        self.writeup = writeup
        self.events: List[Dict[str, Any]] = []
        self.last_stream_data: Any = None
        self.fail: Optional[Exception] = None
        self.tenant_id = "t"
        self.principal_id = "p"
        self.motet_id = "m"
        self.task_id = "task"
        self.command_id = "cmd"
        self.conversation_id = "c"
        self.distributed_context = None

    def do(self, _cmd: Any, data: Any = None, **_kwargs: Any) -> Dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        self.last_stream_data = data
        return {
            "final_content": self.writeup,
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "reasoning_tokens": 0,
            "tool_time_ms": 0,
            "cost_usd": 0.001,
        }

    def stream_event(self, name: str, **fields: Any) -> None:
        self.events.append({"name": name, **fields})


def _usage() -> Dict[str, Any]:
    return {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 0,
        "tool_time_ms": 0,
        "cost_usd": 0.05,
    }


def _loop_data(*, inject_meta_tools: bool = True) -> AgenticLoopData:
    return AgenticLoopData(
        input="research pricing",
        conversation_history=[
            Message(role="system", content="You are one parallel slice."),
            Message(role="user", content="Find RDS pricing"),
            Message(role="assistant", content="", tool_calls_canonical=None),
            Message(role="tool", content="us-east-1 on-demand $0.12"),
        ],
        max_iterations=10,
        remaining_iterations=0,
        inject_meta_tools=inject_meta_tools,
        stream_key="task:t:response",
    )


def test_tool_time_rail_is_finalized() -> None:
    assert "max_tool_time" in RAIL_FINALIZE_REASONS


def test_finalize_calls_the_model_with_no_tools() -> None:
    motet = _FakeMotet()
    usage = _usage()
    data = _loop_data()

    text = _try_finalize_writeup(
        motet, data, stop_reason="max_iterations", accumulated_usage=usage
    )

    assert text == "RDS on-demand is $0.12/hour."
    assert motet.last_stream_data is not None
    assert motet.last_stream_data.tools == []
    assert data.conversation_history[-1].role == "assistant"
    assert data.conversation_history[-1].content == text
    assert data.conversation_history[-2].content.startswith(BUDGET_FINALIZE_PREFIX)
    assert "max_iterations" in data.conversation_history[-2].content
    assert usage["prompt_tokens"] == 112
    assert usage["cost_usd"] == pytest.approx(0.051)
    assert data.model_calls_used == 1
    assert motet.events[-1]["name"] == "agentic_loop_finalized"


def test_finalize_failure_falls_back_and_drops_the_notice() -> None:
    motet = _FakeMotet()
    motet.fail = CommandExecutionError(
        error_type="Timeout",
        message="model timed out",
        details={},
        recoverable=True,
        command_type="core.model_stream",
        command_id="m1",
    )
    data = _loop_data()
    before = len(data.conversation_history)

    message, finalized = _forced_finalize_message(
        motet,
        data,
        fallback="Maximum iterations reached. Please continue to keep working on this task.",
        stop_reason="max_iterations",
        accumulated_usage=_usage(),
    )

    assert finalized is False
    assert "Please continue" in message
    assert len(data.conversation_history) == before
    assert not any(
        isinstance(msg.content, str) and msg.content.startswith(BUDGET_FINALIZE_PREFIX)
        for msg in data.conversation_history
    )


def test_hosted_tools_turns_are_not_finalized() -> None:
    motet = _FakeMotet()
    data = _loop_data(inject_meta_tools=False)

    assert (
        _try_finalize_writeup(
            motet, data, stop_reason="max_iterations", accumulated_usage=_usage()
        )
        is None
    )
    assert motet.last_stream_data is None


def test_empty_writeup_is_not_finalized() -> None:
    motet = _FakeMotet(writeup="   ")
    data = _loop_data()
    text = _try_finalize_writeup(
        motet, data, stop_reason="max_cost", accumulated_usage=_usage()
    )
    assert text is None


def test_build_loop_result_marks_finalized() -> None:
    result = build_loop_result(
        "partial findings",
        [],
        10,
        "max_iterations",
        _usage(),
        finalized=True,
    )
    assert result["finalized"] is True
    assert result["stop_reason"] == "max_iterations"
    assert result["final_response"] == "partial findings"


def test_build_loop_result_omits_finalized_by_default() -> None:
    result = build_loop_result("done", [], 1, "stop", _usage())
    assert "finalized" not in result


def test_persist_carries_finalized_for_nested_children() -> None:
    from motet.core.orchestration.turn.runtime import materialize_intent
    from motet.core.reasoning.react.agentic_loop import _budget_stop_result
    from motet.core.reasoning.react.loop_intents import is_turn_intent

    data = _loop_data()
    data.parent_agent_id = "core.default"
    motet = _FakeMotet()
    intent = _budget_stop_result(
        motet,
        data,
        message="RDS on-demand is $0.12/hour.",
        stop_reason="max_iterations",
        iterations_used=10,
        accumulated_usage=_usage(),
        accumulated_media=[],
        finalized=True,
    )
    assert is_turn_intent(intent)
    assert intent["finalized"] is True

    with patch(
        "motet.core.orchestration.turn.runtime.persist.persist_budget_continue_checkpoint",
        return_value="should-not-write",
    ) as persist:
        result = materialize_intent(motet, data, intent)

    persist.assert_not_called()
    assert result["finalized"] is True
    assert result["stop_reason"] == "max_iterations"
    assert result["final_response"] == "RDS on-demand is $0.12/hour."
