"""
Motet - unit tests for turn cost propagation into export hooks (ADR-0018)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-27

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

from motet.core.orchestration.turn.complete import extract_turn_cost, resolve_turn_model
from motet.core.reasoning.react.loop_results import accumulate_usage, build_loop_result


def _seeded_accumulator() -> Dict[str, Any]:
    """Zeroed token counters as agentic_loop seeds them at turn start."""
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 0,
        "tool_time_ms": 0,
    }


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
