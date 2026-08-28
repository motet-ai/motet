"""
Motet - Agentic Loop Budget Wrap-Up Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Unit coverage for the trailing budget notice the loop injects when
    remaining Motet-tool rounds are almost gone. The model does not otherwise
    see remaining_iterations; the notice is a user message so the cached
    system prefix stays intact (ADR-0124).

Usage:
    pytest tests/unit/core/test_agentic_loop_budget_wrap_up.py
"""

from __future__ import annotations

from motet.core.reasoning.react.agentic_loop import (
    BUDGET_WRAP_UP_PREFIX,
    _maybe_append_budget_wrap_up,
)
from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData
from motet.core.types import Message


def _loop_data(
    *,
    remaining: int,
    max_iterations: int = 20,
    inject_meta_tools: bool = True,
    extra_messages: list[Message] | None = None,
) -> AgenticLoopData:
    history = [
        Message(role="system", content="You are Motet's assistant."),
        Message(role="user", content="research pricing"),
    ]
    if extra_messages:
        history.extend(extra_messages)
    return AgenticLoopData(
        input="research pricing",
        conversation_history=history,
        max_iterations=max_iterations,
        remaining_iterations=remaining,
        inject_meta_tools=inject_meta_tools,
        stream_key="task:t:response",
    )


def _wrap_ups(data: AgenticLoopData) -> list[str]:
    return [
        msg.content
        for msg in data.conversation_history
        if isinstance(msg.content, str) and msg.content.startswith(BUDGET_WRAP_UP_PREFIX)
    ]


def test_no_notice_while_budget_is_comfortable() -> None:
    data = _loop_data(remaining=5)
    assert _maybe_append_budget_wrap_up(data) is False
    assert _wrap_ups(data) == []
    assert [msg.role for msg in data.conversation_history] == ["system", "user"]


def test_notice_fires_on_the_last_two_rounds() -> None:
    data = _loop_data(remaining=2)
    assert _maybe_append_budget_wrap_up(data) is True
    texts = _wrap_ups(data)
    assert len(texts) == 1
    assert "Iteration 19 of 20" in texts[0]
    assert "2 rounds left" in texts[0]
    assert data.conversation_history[-1].role == "user"
    assert data.conversation_history[0].role == "system"
    assert data.conversation_history[0].content == "You are Motet's assistant."


def test_last_round_tells_the_model_not_to_call_tools() -> None:
    data = _loop_data(remaining=1, max_iterations=10)
    assert _maybe_append_budget_wrap_up(data) is True
    text = _wrap_ups(data)[0]
    assert "Iteration 10 of 10" in text
    assert "Last round" in text
    assert "Do not call tools" in text


def test_a_new_notice_replaces_the_previous_one() -> None:
    first = Message(
        role="user",
        content=f"{BUDGET_WRAP_UP_PREFIX} Iteration 19 of 20. Two rounds left.",
    )
    data = _loop_data(remaining=1, extra_messages=[first])
    assert _maybe_append_budget_wrap_up(data) is True
    texts = _wrap_ups(data)
    assert len(texts) == 1
    assert "Iteration 20 of 20" in texts[0]


def test_refreshed_budget_drops_a_stale_last_round_notice() -> None:
    """Continue resets remaining_iterations; the old notice must not linger."""
    stale = Message(
        role="user",
        content=f"{BUDGET_WRAP_UP_PREFIX} Iteration 20 of 20. Last round.",
    )
    data = _loop_data(remaining=20, extra_messages=[stale])
    assert _maybe_append_budget_wrap_up(data) is False
    assert _wrap_ups(data) == []


def test_hosted_tools_turns_do_not_mutate_client_messages() -> None:
    data = _loop_data(remaining=1, inject_meta_tools=False)
    assert _maybe_append_budget_wrap_up(data) is False
    assert _wrap_ups(data) == []
    assert len(data.conversation_history) == 2
