"""
Unit tests for issue #188 budget-stop Continue contract.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import patch

from motet.core.checkpoints import CheckpointKind, TurnCheckpoint
from motet.core.orchestration.turn.budget_continue import (
    BUDGET_STOP_FALLBACK_MESSAGE,
    BUDGET_STOP_REASONS,
    CONTINUE_AFTER_BUDGET_USER_MESSAGE,
    CONTINUE_STEERING_SYSTEM_MESSAGE,
    STOP_REASON_HEADER,
    budget_continue_tip,
    inject_budget_continue_steering,
    is_budget_stop,
    try_build_budget_continue_loop_data,
)
from motet.core.orchestration.turn.complete import extract_response_text
from motet.core.orchestration.turn.outcome import classify_loop_outcome
from motet.core.types import Message
from motet.interfaces.api.openai_compat import execution, routes


def test_budget_stop_reasons_are_stable() -> None:
    assert BUDGET_STOP_REASONS == frozenset({"max_iterations", "max_model_calls"})
    assert is_budget_stop("max_iterations")
    assert is_budget_stop("max_model_calls")
    assert not is_budget_stop("stalled")
    assert not is_budget_stop("suspended")
    assert not is_budget_stop(None)


def test_continue_message_and_header_constants() -> None:
    assert CONTINUE_AFTER_BUDGET_USER_MESSAGE == "Continue working on this task."
    assert STOP_REASON_HEADER == "X-Motet-Stop-Reason"
    assert "Please continue" in BUDGET_STOP_FALLBACK_MESSAGE
    assert "continuation" in CONTINUE_STEERING_SYSTEM_MESSAGE.lower()


def test_budget_continue_tip_names_reason() -> None:
    tip = budget_continue_tip("max_iterations")
    assert "max_iterations" in tip
    assert CONTINUE_AFTER_BUDGET_USER_MESSAGE in tip


def test_inject_budget_continue_steering_before_last_user() -> None:
    history = [
        Message(role="user", content="find the docs"),
        Message(role="assistant", content="working…"),
        Message(role="user", content=CONTINUE_AFTER_BUDGET_USER_MESSAGE),
    ]
    out = inject_budget_continue_steering(history, stop_reason="max_model_calls")
    assert out[-1].role == "user"
    assert out[-1].content == CONTINUE_AFTER_BUDGET_USER_MESSAGE
    assert out[-2].role == "system"
    assert "max_model_calls" in (out[-2].content or "")


def test_loop_result_stop_reason_is_visible_to_the_turn() -> None:
    """run_agent results keep stop_reason at the top level for Continue."""
    result = {
        "final_response": "partial work",
        "tool_results": [],
        "iterations_used": 10,
        "stop_reason": "max_iterations",
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }
    assert classify_loop_outcome(result).stop_reason == "max_iterations"
    assert extract_response_text(result) == "partial work"


def test_agent_result_carries_stop_reason() -> None:
    result = execution._agent_result(
        "done for now",
        stop_reason="max_model_calls",
    )
    assert result["stop_reason"] == "max_model_calls"
    assert result["finish_reason"] == "stop"
    assert result["content"] == "done for now"


def test_apply_session_banner_prepends_continue_tip_before_banner(monkeypatch) -> None:
    tip = budget_continue_tip("max_iterations")
    ctx = SimpleNamespace(
        mode=routes.FacadeMode.AGENT,
        cfg=SimpleNamespace(openai_compat_session_banner="every"),
        conversation_id="openai-abc",
        conversation_is_new=True,
    )
    monkeypatch.setattr(
        routes.sessions,
        "build_session_banner",
        lambda _cid, **_kw: "\n\n---\n_Motet session `openai-abc` - tracked 2026-08-07 00:00 UTC_",
    )
    out = routes._apply_session_banner(
        ctx,
        {"content": "partial", "stop_reason": "max_iterations"},
    )
    assert tip.strip() in out["content"]
    assert out["content"].index(tip.strip()) < out["content"].index("Motet session")
    # Banner must remain end-anchored for continuity parsing.
    assert out["content"].rstrip().endswith("_")


def test_correlation_headers_include_stop_reason() -> None:
    ctx = SimpleNamespace(
        task_id="task-1",
        conversation_id="conv-1",
        mode=SimpleNamespace(value="agent"),
        model_id="gpt-test",
        trace_id=None,
    )
    headers = routes._correlation_headers(ctx, stop_reason="max_iterations")
    assert headers[STOP_REASON_HEADER] == "max_iterations"
    assert headers["X-Motet-Task-Id"] == "task-1"


def test_try_build_budget_continue_loop_data_applies_fresh_budget() -> None:
    checkpoint = TurnCheckpoint(
        checkpoint_id="budget-xyz",
        checkpoint_kind=CheckpointKind.BUDGET_CONTINUE,
        budget_stop_reason="max_iterations",
        motet_id="default",
        tenant_id="tenant-a",
        principal_id="user-1",
        conversation_id="conv-1",
        input="research the site",
        used_tool_names=["mcp.browser.navigate"],
        executed_signatures=["nav:1"],
        max_iterations=5,
        remaining_iterations=0,
        max_model_calls=15,
        model_calls_used=15,
        model_provider="openai",
        model_name="gpt-4.1-mini",
        conversation_history=[
            {"role": "user", "content": "research the site"},
            {"role": "assistant", "content": "partial"},
        ],
    )
    motet = SimpleNamespace(
        conversation_id="conv-1",
        tenant_id="tenant-a",
        motet_id="default",
        principal_id="user-1",
        task_id="task-9",
    )
    history = [
        Message(role="user", content="research the site"),
        Message(role="assistant", content="partial"),
        Message(role="user", content=CONTINUE_AFTER_BUDGET_USER_MESSAGE),
    ]

    with patch(
        "motet.core.checkpoints.find_latest_checkpoint_for_conversation",
        return_value=checkpoint,
    ):
        loop_data = try_build_budget_continue_loop_data(
            motet,
            history=history,
            stream_key="task:task-9:response",
            max_iterations=20,
            max_model_calls=60,
        )

    assert loop_data is not None
    assert loop_data.max_iterations == 20
    assert loop_data.remaining_iterations == 20
    assert loop_data.max_model_calls == 60
    assert loop_data.model_calls_used == 0
    assert loop_data.used_tool_names == ["mcp.browser.navigate"]
    assert loop_data.executed_signatures == ["nav:1"]
    assert loop_data.usage_accumulator is None
    assert any(
        getattr(m, "role", None) == "system"
        and "max_iterations" in str(getattr(m, "content", ""))
        for m in loop_data.conversation_history
    )


def test_try_build_budget_continue_loop_data_miss_returns_none() -> None:
    motet = SimpleNamespace(
        conversation_id="conv-missing",
        tenant_id="tenant-a",
        motet_id="default",
        principal_id="user-1",
    )
    with patch(
        "motet.core.checkpoints.find_latest_checkpoint_for_conversation",
        return_value=None,
    ):
        assert (
            try_build_budget_continue_loop_data(
                motet,
                history=[Message(role="user", content=CONTINUE_AFTER_BUDGET_USER_MESSAGE)],
                stream_key="task:t:response",
                max_iterations=20,
            )
            is None
        )


def test_budget_stop_result_attaches_checkpoint_id() -> None:
    loop_mod = importlib.import_module("motet.core.reasoning.react.agentic_loop")
    from motet.core.orchestration.turn.runtime import materialize_intent
    from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData
    from motet.core.reasoning.react.loop_intents import is_turn_intent

    data = AgenticLoopData(
        input="go",
        conversation_history=[Message(role="user", content="go")],
        max_iterations=5,
        remaining_iterations=0,
        max_model_calls=15,
        model_calls_used=5,
        stream_key="task:t:response",
        used_tool_names=["core.help"],
    )
    motet = SimpleNamespace(
        motet_id="default",
        tenant_id="t",
        principal_id="p",
        task_id="task",
        conversation_id="conv",
    )

    intent = loop_mod._budget_stop_result(
        motet,
        data,
        message="Maximum iterations reached.",
        stop_reason="max_iterations",
        iterations_used=5,
        accumulated_usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        accumulated_media=[],
    )
    assert is_turn_intent(intent)

    with patch(
        "motet.core.orchestration.turn.runtime.persist.persist_budget_continue_checkpoint",
        return_value="budget-test",
    ) as persist:
        result = materialize_intent(motet, data, intent)

    persist.assert_called_once()
    assert result["stop_reason"] == "max_iterations"
    assert result["budget_continue_checkpoint_id"] == "budget-test"
    assert result.get("suspended") is not True


def test_budget_stop_skips_continue_for_nested_subagent() -> None:
    """Nested loops are not user turns; Continue would steal the conversation index."""
    loop_mod = importlib.import_module("motet.core.reasoning.react.agentic_loop")
    from motet.core.orchestration.turn.runtime import materialize_intent
    from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData
    from motet.core.reasoning.react.loop_intents import is_turn_intent

    data = AgenticLoopData(
        input="research pricing",
        conversation_history=[Message(role="user", content="research pricing")],
        max_iterations=10,
        remaining_iterations=0,
        max_model_calls=30,
        model_calls_used=10,
        stream_key="task:t:response",
        agent_id="researcher",
        parent_agent_id="core.default",
    )
    motet = SimpleNamespace(
        motet_id="default",
        tenant_id="t",
        principal_id="p",
        task_id="task",
        conversation_id="conv",
    )

    intent = loop_mod._budget_stop_result(
        motet,
        data,
        message="Maximum iterations reached.",
        stop_reason="max_iterations",
        iterations_used=10,
        accumulated_usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        accumulated_media=[],
    )
    assert is_turn_intent(intent)

    with patch(
        "motet.core.orchestration.turn.runtime.persist.persist_budget_continue_checkpoint",
        return_value="budget-should-not-write",
    ) as persist:
        result = materialize_intent(motet, data, intent)

    persist.assert_not_called()
    assert result["stop_reason"] == "max_iterations"
    assert "budget_continue_checkpoint_id" not in result
    assert result.get("suspended") is not True


def test_budget_stop_continue_uses_parent_not_spawn_name() -> None:
    """A spawn-looking agent_id without parent_agent_id is still a user turn."""
    loop_mod = importlib.import_module("motet.core.reasoning.react.agentic_loop")
    from motet.core.orchestration.turn.runtime import materialize_intent
    from motet.core.reasoning.react.agentic_loop_data import AgenticLoopData
    from motet.core.reasoning.react.loop_intents import is_turn_intent

    data = AgenticLoopData(
        input="go",
        conversation_history=[Message(role="user", content="go")],
        max_iterations=5,
        remaining_iterations=0,
        max_model_calls=15,
        model_calls_used=5,
        stream_key="task:t:response",
        agent_id="core.default.spawn-2",
    )
    motet = SimpleNamespace(
        motet_id="default",
        tenant_id="t",
        principal_id="p",
        task_id="task",
        conversation_id="conv",
    )
    intent = loop_mod._budget_stop_result(
        motet,
        data,
        message="Maximum iterations reached.",
        stop_reason="max_iterations",
        iterations_used=5,
        accumulated_usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        accumulated_media=[],
    )
    assert is_turn_intent(intent)

    with patch(
        "motet.core.orchestration.turn.runtime.persist.persist_budget_continue_checkpoint",
        return_value="budget-user-turn",
    ) as persist:
        result = materialize_intent(motet, data, intent)

    persist.assert_called_once()
    assert result["budget_continue_checkpoint_id"] == "budget-user-turn"
