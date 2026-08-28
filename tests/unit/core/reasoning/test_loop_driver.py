"""
Motet - Agentic Loop Driver Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Unit tests for ADR-0132 ``run_agentic_loop``: in-process continuation,
    terminal suspend/success, cancel-before-iteration, error propagation,
    safety bound, per-iteration metadata stamp, and Turn Runtime intent
    materialization (ADR-0134).
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from motet.core.commands.distributed_types import (
    AGENTIC_LOOP_ITERATION_META_KEY,
    agentic_loop_iteration_metadata_fields,
    parse_agentic_loop_iteration,
)
from motet.core.commands.response_models import CommandExecutionError
from motet.core.distributed.task_control import TASK_CANCELLED_CODE
from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData
from motet.core.reasoning.react.loop_driver import (
    AGENTIC_LOOP_CONTINUE_KEY,
    agentic_loop_continue,
    is_agentic_loop_continue,
    run_agentic_loop,
    stamp_agentic_loop_iteration,
)
from motet.core.types import Message


def _loop_data(**overrides: Any) -> AgenticLoopData:
    defaults: Dict[str, Any] = dict(
        input="hello",
        conversation_history=[Message(role="user", content="hello")],
        remaining_iterations=3,
        max_iterations=3,
        stream_key="task:t1:response",
        usage_accumulator={"prompt_tokens": 1},
    )
    defaults.update(overrides)
    return AgenticLoopData(**defaults)


def _motet() -> MagicMock:
    motet = MagicMock()
    motet.task_id = None
    motet.cancel_scopes = []
    motet.command_id = "cmd-agent"
    motet.command_type = "core.agent_loop"
    motet.metadata = {}
    return motet


def test_continue_payload_keeps_loop_data_in_memory() -> None:
    original = _loop_data(remaining_iterations=2, used_tool_names=["core.help"])
    payload = agentic_loop_continue(original)
    assert is_agentic_loop_continue(payload)
    restored = payload[AGENTIC_LOOP_CONTINUE_KEY]
    assert restored is original
    assert restored.remaining_iterations == 2
    assert restored.used_tool_names == ["core.help"]
    assert restored.conversation_history[0].content == "hello"


def test_run_agentic_loop_stops_on_terminal_result() -> None:
    motet = _motet()
    with patch(
        "motet.core.reasoning.react.agentic_loop.agentic_loop",
        return_value={"suspended": True, "stop_reason": "suspended"},
    ) as iterate:
        result = run_agentic_loop(motet, _loop_data())
    assert result["suspended"] is True
    assert result["observation_cache"] == {}
    assert result["executed_signatures"] == []
    assert iterate.call_count == 1


def test_run_agentic_loop_materializes_turn_intent() -> None:
    from motet.core.reasoning.react.loop_intents import INTENT_HANDBACK, turn_intent

    motet = _motet()
    intent = turn_intent(INTENT_HANDBACK, unique_tool_calls=[], content="")
    terminal = {
        "suspended": True,
        "stop_reason": "suspended",
        "checkpoint_id": "cp-1",
    }
    with patch(
        "motet.core.reasoning.react.agentic_loop.agentic_loop",
        return_value=intent,
    ), patch(
        "motet.core.orchestration.turn.runtime.materialize_intent",
        return_value=terminal,
    ) as materialize:
        result = run_agentic_loop(motet, _loop_data())
    materialize.assert_called_once()
    assert result["checkpoint_id"] == "cp-1"
    assert result["suspended"] is True


def test_run_agentic_loop_continues_then_terminal() -> None:
    first = _loop_data(remaining_iterations=2)
    second = _loop_data(remaining_iterations=1)
    motet = _motet()
    with patch(
        "motet.core.reasoning.react.agentic_loop.agentic_loop",
        side_effect=[
            agentic_loop_continue(second),
            {"final_response": "done", "stop_reason": "completed"},
        ],
    ) as iterate:
        result = run_agentic_loop(motet, first)
    assert result["final_response"] == "done"
    assert iterate.call_count == 2
    second_data = iterate.call_args_list[1].args[0]
    assert second_data is second
    assert second_data.remaining_iterations == 1


def test_run_agentic_loop_reraises_command_error() -> None:
    motet = _motet()
    with patch(
        "motet.core.reasoning.react.agentic_loop.agentic_loop",
        side_effect=CommandExecutionError(
            error_type="Timeout",
            message="failed",
            details={},
            recoverable=False,
            command_type="core.model_stream",
            command_id="c1",
        ),
    ):
        with pytest.raises(CommandExecutionError):
            run_agentic_loop(motet, _loop_data())


def test_run_agentic_loop_safety_bound() -> None:
    motet = _motet()

    def _always_continue(_data: AgenticLoopData) -> Dict[str, Any]:
        return agentic_loop_continue(
            _loop_data(remaining_iterations=1, max_iterations=1)
        )

    with patch(
        "motet.core.reasoning.react.agentic_loop.agentic_loop",
        side_effect=_always_continue,
    ) as iterate:
        with pytest.raises(RuntimeError, match="safety bound"):
            run_agentic_loop(
                motet, _loop_data(remaining_iterations=1, max_iterations=1)
            )
    assert iterate.call_count == 3  # max(1, 1, 1) + 2


def test_run_agentic_loop_cancels_before_iteration() -> None:
    motet = _motet()
    motet.task_id = "t1"
    motet.cancel_scopes = ["t1"]
    with patch(
        "motet.core.distributed.task_control.is_cancelled", return_value=True
    ):
        with patch(
            "motet.core.reasoning.react.agentic_loop.agentic_loop"
        ) as iterate:
            with pytest.raises(CommandExecutionError) as exc:
                run_agentic_loop(motet, _loop_data())
    iterate.assert_not_called()
    assert exc.value.error_type == "TaskCancelled"
    assert exc.value.details["code"] == TASK_CANCELLED_CODE
    assert exc.value.command_type == "core.agent_loop"


def test_run_agentic_loop_does_not_dispatch_via_motet_do() -> None:
    motet = _motet()
    with patch(
        "motet.core.reasoning.react.agentic_loop.agentic_loop",
        return_value={"final_response": "ok", "stop_reason": "completed"},
    ):
        run_agentic_loop(motet, _loop_data())
    motet.do.assert_not_called()


def test_run_agentic_loop_clears_motet_context() -> None:
    from motet.core.commands.motet_context import get_motet_context

    motet = _motet()
    with patch(
        "motet.core.reasoning.react.agentic_loop.agentic_loop",
        return_value={"final_response": "ok", "stop_reason": "completed"},
    ):
        run_agentic_loop(motet, _loop_data())
    with pytest.raises(RuntimeError, match="MotetContext not available"):
        get_motet_context()


def test_run_agentic_loop_restores_parent_motet_context() -> None:
    from motet.core.commands.motet_context import (
        _clear_motet_context,
        _set_motet_context,
        get_motet_context,
    )

    parent = _motet()
    parent.command_id = "cmd-parent"
    child = _motet()
    child.command_id = "cmd-child"
    _set_motet_context(parent)
    try:
        with patch(
            "motet.core.reasoning.react.agentic_loop.agentic_loop",
            return_value={"final_response": "ok", "stop_reason": "completed"},
        ):
            run_agentic_loop(child, _loop_data())
        assert get_motet_context() is parent
    finally:
        _clear_motet_context()


def test_run_agentic_loop_rebinding_survives_child_clearing_context() -> None:
    """In-process child commands used to ``_clear_motet_context``; continue still works."""
    from motet.core.commands.motet_context import (
        _clear_motet_context,
        get_motet_context,
    )

    motet = _motet()
    calls = {"n": 0}

    def _iteration(_data: AgenticLoopData) -> Dict[str, Any]:
        calls["n"] += 1
        assert get_motet_context() is motet
        _clear_motet_context()
        if calls["n"] == 1:
            return agentic_loop_continue(_loop_data(remaining_iterations=2))
        return {"final_response": "ok", "stop_reason": "completed"}

    with patch(
        "motet.core.reasoning.react.agentic_loop.agentic_loop",
        side_effect=_iteration,
    ):
        result = run_agentic_loop(motet, _loop_data())
    assert result["final_response"] == "ok"
    assert calls["n"] == 2
    with pytest.raises(RuntimeError, match="MotetContext not available"):
        get_motet_context()


def test_parse_agentic_loop_iteration() -> None:
    assert parse_agentic_loop_iteration(3) == 3
    assert parse_agentic_loop_iteration("4") == 4
    assert parse_agentic_loop_iteration(0) is None
    assert parse_agentic_loop_iteration(True) is None
    assert parse_agentic_loop_iteration("nope") is None


def test_agentic_loop_iteration_metadata_fields() -> None:
    assert agentic_loop_iteration_metadata_fields(None) == {}
    assert agentic_loop_iteration_metadata_fields({"other": 1}) == {}
    assert agentic_loop_iteration_metadata_fields(
        {AGENTIC_LOOP_ITERATION_META_KEY: "2"}
    ) == {AGENTIC_LOOP_ITERATION_META_KEY: 2}


def test_stamp_agentic_loop_iteration_writes_metadata() -> None:
    motet = _motet()
    stamp_agentic_loop_iteration(motet, 3)
    assert motet.metadata[AGENTIC_LOOP_ITERATION_META_KEY] == 3


def test_agentic_loop_data_current_iteration() -> None:
    assert _loop_data(remaining_iterations=3, max_iterations=3).current_iteration == 1
    assert _loop_data(remaining_iterations=1, max_iterations=3).current_iteration == 3


def test_run_agentic_loop_stamps_iteration_on_metadata() -> None:
    motet = _motet()
    first = _loop_data(remaining_iterations=3, max_iterations=3)
    second = _loop_data(remaining_iterations=2, max_iterations=3)
    seen: list[int] = []

    def _iterate(data: AgenticLoopData) -> Dict[str, Any]:
        seen.append(int(motet.metadata[AGENTIC_LOOP_ITERATION_META_KEY]))
        if data.remaining_iterations == 3:
            return agentic_loop_continue(second)
        return {"final_response": "done", "stop_reason": "completed"}

    with patch(
        "motet.core.reasoning.react.agentic_loop.agentic_loop",
        side_effect=_iterate,
    ):
        run_agentic_loop(motet, first)
    assert seen == [1, 2]
    assert first.current_iteration == 1
    assert second.current_iteration == 2
