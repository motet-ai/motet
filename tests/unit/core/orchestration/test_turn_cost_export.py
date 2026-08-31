"""
Motet - unit tests for turn cost propagation into export hooks (ADR-0018)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Covers the cost chain that feeds ``turn_hooks.after_finalize`` exports: the
    agentic loop sums each priced model call into a top-level ``cost_usd``,
    the turn
    reads it back with ``extract_turn_cost``. Regression guard: before this
    chain existed, ``agent_turn`` looked for a ``cost_usd`` nobody produced, so
    every exported turn reported no cost while token counts looked correct.

Dependencies:
    - pytest
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from motet.core.orchestration.turn.complete import (
    extract_thinking_text,
    extract_tool_summaries,
    extract_spawn_children,
    extract_turn_cost,
    resolve_turn_model,
)
from motet.core.reasoning.react.loop_results import (
    accumulate_usage,
    build_loop_result,
    empty_usage_accumulator,
    extend_spawn_children,
    summarize_tool_results,
    usage_stream_fields,
)


def _seeded_accumulator() -> Dict[str, Any]:
    """Zeroed token counters as agentic_loop seeds them at turn start."""
    return empty_usage_accumulator()


def test_accumulate_usage_sums_cost_across_model_calls() -> None:
    acc = _seeded_accumulator()
    accumulate_usage(acc, {"prompt_tokens": 100, "completion_tokens": 10, "cost_usd": 0.0002})
    accumulate_usage(acc, {"prompt_tokens": 200, "completion_tokens": 20, "cost_usd": 0.0003})

    assert acc["prompt_tokens"] == 300
    assert acc["cost_usd"] == pytest.approx(0.0005)


def test_accumulate_usage_omits_cost_when_no_call_reported_one() -> None:
    acc = _seeded_accumulator()
    accumulate_usage(acc, {"prompt_tokens": 50, "completion_tokens": 5})

    # Absent, not zero: a $0.00 on an unpriced turn reads as "this was free".
    assert "cost_usd" not in acc


def test_accumulate_usage_tolerates_tool_only_updates() -> None:
    acc = _seeded_accumulator()
    accumulate_usage(acc, {"cost_usd": 0.001})
    accumulate_usage(acc, {"tool_time_ms": 42})

    assert acc["tool_time_ms"] == 42
    assert acc["cost_usd"] == pytest.approx(0.001)


def test_usage_stream_fields_keep_cost_top_level_and_omit_unknown() -> None:
    acc = _seeded_accumulator()
    accumulate_usage(acc, {"prompt_tokens": 10, "completion_tokens": 2, "cost_usd": 0.25})

    fields = usage_stream_fields(acc)

    assert fields["cost_usd"] == pytest.approx(0.25)
    assert fields["prompt_tokens"] == 10
    assert acc["cost_usd"] == pytest.approx(0.25)

    unpriced = usage_stream_fields(_seeded_accumulator())
    assert "cost_usd" not in unpriced
    assert usage_stream_fields({"cost_usd": 0}) == {}
    assert usage_stream_fields({"cost_usd": "0.25"})["cost_usd"] == pytest.approx(0.25)


def test_terminal_result_keeps_zero_cost_stream_frame_omits_it() -> None:
    acc = _seeded_accumulator()
    accumulate_usage(acc, {"prompt_tokens": 4, "cost_usd": 0.0})

    result = build_loop_result("done", [], 1, "stop", acc)
    assert result["cost_usd"] == 0.0
    assert "cost_usd" not in usage_stream_fields(acc)


def test_build_loop_result_surfaces_cost_top_level_not_inside_usage() -> None:
    acc = _seeded_accumulator()
    accumulate_usage(acc, {"prompt_tokens": 10, "completion_tokens": 2, "cost_usd": 0.25})

    result = build_loop_result("done", [], 1, "stop", acc)

    assert result["cost_usd"] == pytest.approx(0.25)
    # usage stays the token envelope the UI and OpenAI-compat facade read.
    assert "cost_usd" not in result["usage"]
    assert result["usage"]["prompt_tokens"] == 10


def test_build_loop_result_does_not_mutate_the_accumulator() -> None:
    acc = _seeded_accumulator()
    accumulate_usage(acc, {"prompt_tokens": 1, "cost_usd": 0.5})

    build_loop_result("done", [], 1, "stop", acc)

    # The same accumulator is carried into a suspension checkpoint, so the
    # terminal result builder must not strip cost out of it.
    assert acc["cost_usd"] == pytest.approx(0.5)


def test_build_loop_result_omits_cost_when_unpriced() -> None:
    result = build_loop_result("done", [], 1, "stop", _seeded_accumulator())
    assert "cost_usd" not in result
    assert "thinking_text" not in result


def test_build_loop_result_includes_thinking_text() -> None:
    result = build_loop_result(
        "done",
        [],
        1,
        "stop",
        _seeded_accumulator(),
        thinking_text="  look this up  ",
    )
    assert result["thinking_text"] == "look this up"


def test_extract_thinking_text_prefers_display_field() -> None:
    assert extract_thinking_text({"thinking_text": "a", "reasoning_content": "b"}) == "a"
    assert extract_thinking_text({"data": {"reasoning_content": " nested "}}) == "nested"
    assert extract_thinking_text({"final_response": "hi"}) is None


def test_summarize_tool_results_uses_tool_call_target_name() -> None:
    rows = summarize_tool_results(
        [
            {
                "tool_call_id": "c1",
                "tool_name": "core.tool_call",
                "status": "success",
                "result": {"preview": "CNN homepage"},
            }
        ],
        step=2,
        tool_calls=[
            {
                "tool_call_id": "c1",
                "tool_name": "core.tool_call",
                "parameters": {"tool_name": "core.browse_page", "parameters": {}},
            }
        ],
    )
    assert rows == [
        {
            "tool_name": "core.browse_page",
            "status": "success",
            "preview": "CNN homepage",
            "step": 2,
        },
    ]


def test_summarize_tool_results_uses_workflow_meta_on_tool_call() -> None:
    rows = summarize_tool_results(
        [
            {
                "tool_name": "core.tool_call",
                "status": "success",
                "result": {
                    "ok": True,
                    "meta": {"tool_name": "workflow_navigate_screenshot", "kind": "workflow"},
                },
            }
        ]
    )
    assert rows[0]["tool_name"] == "workflow_navigate_screenshot"


def test_summarize_tool_results_includes_duration_ms() -> None:
    rows = summarize_tool_results(
        [
            {
                "tool_name": "core.http_get_browser",
                "status": "success",
                "result": {"preview": "ok"},
                "duration_ms": 2364,
            }
        ],
        step=2,
    )
    assert rows[0]["duration_ms"] == 2364


def test_summarize_tool_results_includes_step() -> None:
    rows = summarize_tool_results(
        [{"tool_name": "core.browse_page", "status": "success", "result": {"preview": "ok"}}],
        step=1,
    )
    assert rows == [
        {"tool_name": "core.browse_page", "status": "success", "preview": "ok", "step": 1},
    ]


def test_extract_tool_summaries_reads_loop_and_nested_shapes() -> None:
    rows = [{"tool_name": "core.browse_page", "status": "success", "preview": "ok"}]
    assert extract_tool_summaries({"tool_summaries": rows}) == rows
    assert extract_tool_summaries({"data": {"tool_summaries": rows}}) == rows
    assert extract_tool_summaries({"final_response": "hi"}) is None
    assert extract_tool_summaries({"tool_summaries": [{"status": "success"}]}) is None


def test_extract_spawn_children_reads_loop_and_nested_shapes() -> None:
    rows = [
        {
            "child_conversation_id": "iso-abc",
            "agent_id": "core.default.spawn-1",
            "title": "research pricing",
        }
    ]
    assert extract_spawn_children({"spawn_children": rows}) == rows
    assert extract_spawn_children({"data": {"spawn_children": rows}}) == rows
    assert extract_spawn_children({"meta": {"spawn_children": rows}}) == rows
    assert extract_spawn_children({"result": {"meta": {"spawn_children": rows}}}) == rows
    assert extract_spawn_children({"final_response": "hi"}) is None
    assert extract_spawn_children({"spawn_children": [{"title": "no id"}]}) is None


def test_extract_spawn_children_overlays_completed_pointer_over_early() -> None:
    early = {
        "child_conversation_id": "iso-a",
        "agent_id": "core.default.spawn-1",
        "title": "research pricing",
    }
    complete = {
        "child_conversation_id": "iso-a",
        "agent_id": "core.default.spawn-1",
        "title": "research pricing",
        "preview": "price is 12",
        "thinking_text": "look up the list price",
        "cost_usd": 0.004,
    }
    merged = extract_spawn_children(
        {"spawn_children": [early], "meta": {"spawn_children": [complete]}}
    )
    assert merged == [complete]


def test_extend_spawn_children_accumulates_from_envelope_meta() -> None:
    acc: list = []
    first = {
        "child_conversation_id": "iso-a",
        "title": "one",
    }
    second = {
        "child_conversation_id": "iso-b",
        "title": "two",
    }
    extend_spawn_children(acc, {"meta": {"spawn_children": [first]}})
    extend_spawn_children(acc, {"result": {"meta": {"spawn_children": [second]}}})
    assert [row["child_conversation_id"] for row in acc] == ["iso-a", "iso-b"]


def test_extend_spawn_children_overlays_completed_pointer() -> None:
    acc: list = []
    early = {
        "child_conversation_id": "iso-a",
        "agent_id": "core.default.spawn-1",
        "title": "one",
    }
    complete = {
        "child_conversation_id": "iso-a",
        "agent_id": "core.default.spawn-1",
        "title": "one",
        "preview": "Sacramento Austin",
        "thinking_text": "pick two capitals",
        "cost_usd": 0.009,
    }
    extend_spawn_children(acc, {"meta": {"spawn_children": [early]}})
    extend_spawn_children(acc, {"meta": {"spawn_children": [complete]}})
    assert len(acc) == 1
    assert acc[0]["preview"] == "Sacramento Austin"
    assert acc[0]["thinking_text"] == "pick two capitals"
    assert acc[0]["cost_usd"] == 0.009
    result = build_loop_result(
        "done",
        [],
        1,
        "stop",
        empty_usage_accumulator(),
        spawn_children=acc,
    )
    assert result["spawn_children"] == acc


def test_extract_turn_cost_reads_loop_and_nested_shapes() -> None:
    # run_agent / core.agent_loop return the loop result verbatim.
    assert extract_turn_cost({"cost_usd": 0.004}) == pytest.approx(0.004)
    # Nested envelopes still work if a caller wraps the loop result.
    assert extract_turn_cost({"data": {"cost_usd": 0.007}}) == pytest.approx(0.007)
    # A genuine zero-cost turn (local inference) is not the same as no cost.
    assert extract_turn_cost({"cost_usd": 0.0}) == 0.0


def test_extract_turn_cost_returns_none_when_absent_or_untyped() -> None:
    assert extract_turn_cost({"usage": {"total_tokens": 10}}) is None
    assert extract_turn_cost({"cost_usd": "0.01"}) is None
    assert extract_turn_cost({"cost_usd": True}) is None
    assert extract_turn_cost(None) is None


def test_resolve_turn_model_prefers_result_then_falls_back_to_config() -> None:
    assert resolve_turn_model({"model": "anthropic/claude"}) == "anthropic/claude"
    assert resolve_turn_model({"data": {"model_name": "gpt-4o-mini"}}) == "gpt-4o-mini"
    assert (
        resolve_turn_model({}, provider="openai", model_name="gpt-4o-mini")
        == "openai/gpt-4o-mini"
    )
    assert resolve_turn_model({}, model_name="gpt-4o-mini") == "gpt-4o-mini"
    assert resolve_turn_model({}) is None


def test_answer_without_tools_emits_usage_and_surfaces_priced_cost() -> None:
    from unittest.mock import Mock

    from motet.core.orchestration.turn.no_tools import answer_without_tools

    motet = Mock()
    motet.stream_key = "task:stream"
    motet.do.return_value = {
        "final_content": "hello",
        "finish_reason": "stop",
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "total_tokens": 15,
        "cost_usd": 0.0025,
    }

    payload = answer_without_tools(motet, messages=[], reason="trivial")

    assert payload["cost_usd"] == pytest.approx(0.0025)
    motet.stream_event.assert_called_once()
    args, kwargs = motet.stream_event.call_args
    assert args == ("usage",)
    assert kwargs["stream_key"] == "task:stream"
    assert kwargs["cost_usd"] == pytest.approx(0.0025)
    assert kwargs["prompt_tokens"] == 12
