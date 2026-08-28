"""
Motet - Agentic Loop Repeat / Stall Progress Rail Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Unit tests for the loop's progress rail, which replaced the per-call duplicate
    veto (ADR-0050 introduced the veto; it was escapable by perturbing a parameter
    and refused legitimate re-reads after a mutation). Repeat calls now execute;
    a run of iterations that request nothing new stops the turn instead.

    Covers: repeats are executed and marked, novelty detection drives
    stalled_iterations, mutation-then-re-read is not penalized, and the rail fires
    at MAX_STALLED_ITERATIONS with a non-empty assistant message.

Usage:
    pytest tests/unit/core/test_agentic_loop_repeat_progress.py
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

from motet.core.reasoning.react.agentic_loop import (
    MAX_STALLED_ITERATIONS,
    _maybe_stop_for_stall,
)
from motet.core.reasoning.react.loop_execution import (
    ToolCallBuildResult,
    build_unique_tool_calls,
    _generate_tool_signature,
)
from motet.core.reasoning.react.agentic_loop_data import (
    AgenticLoopData,
)


def _data(**overrides: Any) -> AgenticLoopData:
    defaults: Dict[str, Any] = dict(input="refactor the module", max_tools=10)
    defaults.update(overrides)
    return AgenticLoopData(**defaults)


def _call(name: str, params: Dict[str, Any], call_id: str = "c1") -> Dict[str, Any]:
    return {"call_id": call_id, "tool_name": name, "arguments": params}


def _filter(
    calls: List[Dict[str, Any]], data: AgenticLoopData
) -> ToolCallBuildResult:
    return build_unique_tool_calls(calls, data, MagicMock(), 1)


# --- repeats execute -------------------------------------------------------------


def test_repeat_call_is_executed_and_marked() -> None:
    """The veto is gone: a repeat reaches execution so the model gets real data."""
    data = _data()
    _filter([_call("core.file_read", {"path": "a.py"})], data)
    result = _filter([_call("core.file_read", {"path": "a.py"}, "c2")], data)

    assert [tc["tool_name"] for tc in result.unique_tool_calls] == ["core.file_read"]
    assert result.unique_tool_calls[0]["is_repeat"] is True
    assert result.had_novel_tool_call is False


def test_explicit_catalog_rejects_a_tool_the_model_was_not_given() -> None:
    """spawn_agents children pass declared schemas; a hallucinated name must not run."""
    data = _data(tools=[{"name": "core.web_search"}])
    result = _filter(
        [
            _call("core.web_search", {"query": "rds"}),
            _call("core.http_get_browser", {"url": "https://example.com"}, "c2"),
        ],
        data,
    )

    assert [tc["tool_name"] for tc in result.unique_tool_calls] == ["core.web_search"]
    denied = [row for row in result.provider_executed_results if row["status"] == "error"]
    assert [row["tool_name"] for row in denied] == ["core.http_get_browser"]
    assert "declared catalog" in denied[0]["result"]


def test_discovery_turns_have_no_catalog_allowlist() -> None:
    """tools is None means the shortlist path, not an empty cage."""
    data = _data()
    result = _filter([_call("core.http_get_browser", {"url": "https://example.com"})], data)

    assert [tc["tool_name"] for tc in result.unique_tool_calls] == ["core.http_get_browser"]
    assert result.provider_executed_results == []


def test_repeat_does_not_inject_a_synthetic_tool_message() -> None:
    """The old path appended a "duplicate ... adjust parameters" result instead.

    That message was the dead end the model had to escape by nudging parameters.
    """
    data = _data()
    _filter([_call("core.file_read", {"path": "a.py"})], data)
    _filter([_call("core.file_read", {"path": "a.py"}, "c2")], data)

    assert data.conversation_history == []


def test_novel_call_is_recorded_once() -> None:
    data = _data()
    _filter([_call("core.file_read", {"path": "a.py"})], data)
    _filter([_call("core.file_read", {"path": "a.py"}, "c2")], data)

    assert data.executed_signatures == [
        _generate_tool_signature("core.file_read", {"path": "a.py"})
    ]


def test_differing_parameters_are_novel() -> None:
    data = _data()
    _filter([_call("core.file_read", {"path": "a.py"})], data)
    result = _filter([_call("core.file_read", {"path": "b.py"}, "c2")], data)

    assert result.had_novel_tool_call is True
    assert len(data.executed_signatures) == 2


def test_iteration_mixing_repeat_and_novel_counts_as_progress() -> None:
    """One new call is enough — a re-read beside real work must not be penalized."""
    data = _data()
    _filter([_call("core.file_read", {"path": "a.py"})], data)
    result = _filter(
        [
            _call("core.file_read", {"path": "a.py"}, "c2"),
            _call("core.file_read", {"path": "b.py"}, "c3"),
        ],
        data,
    )

    assert result.had_novel_tool_call is True


def test_iteration_with_nothing_executable_is_not_a_stall() -> None:
    """No executable calls means no repetition to judge."""
    data = _data()
    result = _filter([], data)

    assert result.unique_tool_calls == []
    assert result.had_novel_tool_call is True


# --- the progress rail -----------------------------------------------------------


def _stall(data: AgenticLoopData, had_novel: bool) -> Any:
    return _maybe_stop_for_stall(
        MagicMock(),
        data,
        ToolCallBuildResult(
            unique_tool_calls=[_call("core.file_read", {"path": "a.py"})],
            provider_executed_results=[],
            had_novel_tool_call=had_novel,
        ),
        1,
        {},
        [],
    )


def test_repeat_only_iterations_accumulate() -> None:
    data = _data()
    for expected in range(1, MAX_STALLED_ITERATIONS):
        assert _stall(data, had_novel=False) is None
        assert data.stalled_iterations == expected


def test_novel_call_resets_the_counter() -> None:
    data = _data(stalled_iterations=MAX_STALLED_ITERATIONS - 1)
    assert _stall(data, had_novel=True) is None
    assert data.stalled_iterations == 0


def test_rail_stops_the_turn_at_the_threshold() -> None:
    data = _data(stalled_iterations=MAX_STALLED_ITERATIONS - 1)
    result = _stall(data, had_novel=False)

    assert result is not None
    assert result["stop_reason"] == "stalled"
    # Budget/stop paths must surface a non-empty assistant message (ADR-0127).
    assert result["final_response"].strip()


def test_mutation_between_reads_never_reaches_the_rail() -> None:
    """Read → write → read is the sequence the old veto broke.

    The re-read executes (it is no longer refused) and, because the write reset the
    counter, an edit-verify cycle can repeat indefinitely without tripping the rail.
    """
    data = _data()
    read = [_call("core.file_read", {"path": "a.py"})]

    for cycle in range(4):
        for calls in (
            read if cycle == 0 else [dict(read[0], call_id=f"r{cycle}")],
            [_call("core.file_write", {"path": "a.py", "content": f"v{cycle}"}, f"w{cycle}")],
        ):
            result = _filter(calls, data)
            assert _stall(data, had_novel=result.had_novel_tool_call) is None

    assert data.stalled_iterations == 0
