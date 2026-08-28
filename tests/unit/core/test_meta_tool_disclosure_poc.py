"""
Motet - Meta-Tool Disclosure PoC Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-30

Description:
    Offline tests for the meta-tool disclosure PoC fake catalog, dispatch, and
    scoring helpers. No provider API keys required.

Dependencies:
    - pytest
    - tests.fixtures.meta_tool_disclosure_poc

Usage:
    pytest tests/unit/core/test_meta_tool_disclosure_poc.py -q
"""

from __future__ import annotations

import json

from tests.fixtures.meta_tool_disclosure_poc import (
    SCENARIOS,
    WIRE_TOOL_CALL,
    WIRE_TOOLS_SEARCH,
    RoundTrace,
    dispatch_meta_tool,
    format_summary_table,
    score_scenario,
    search_catalog,
    validate_and_call,
)


def test_poc_search_returns_schemas_for_schedule_intent() -> None:
    matches = search_catalog("remind me tomorrow", limit=5)
    names = {m["name"] for m in matches}
    assert "core.schedule_command" in names
    sched = next(m for m in matches if m["name"] == "core.schedule_command")
    assert "json_schema" in sched
    assert "message" in sched["json_schema"]["properties"]


def test_poc_tool_call_validates_required_fields() -> None:
    bad = validate_and_call("core.schedule_command", {"message": "hi"})
    assert bad["ok"] is False
    assert bad["error"] == "validation_error"
    assert "expected_schema" in bad

    good = validate_and_call(
        "core.schedule_command",
        {"message": "call Mom", "when": "tomorrow 9am"},
    )
    assert good["ok"] is True
    assert good["tool_name"] == "core.schedule_command"


def test_poc_dispatch_search_and_call() -> None:
    search_obs = json.loads(
        dispatch_meta_tool(WIRE_TOOLS_SEARCH, {"query": "calendar tomorrow"})
    )
    assert search_obs["ok"] is True
    assert search_obs["matches"]
    names = {m["name"] for m in search_obs["matches"]}
    assert "mcp.google_workspace.list_events" in names or (
        "mcp.google_workspace.list_calendars" in names
    )

    call_obs = json.loads(
        dispatch_meta_tool(
            WIRE_TOOL_CALL,
            {
                "tool_name": "mcp.google_workspace.list_events",
                "parameters": {"calendar_id": "primary"},
            },
        )
    )
    assert call_obs["ok"] is True


def test_poc_score_idle_and_need_paths() -> None:
    idle = next(s for s in SCENARIOS if s.scenario_id == "idle")
    need = next(s for s in SCENARIOS if s.scenario_id == "need_schedule")

    clean = score_scenario(idle, case_id="t/m", traces=[], rounds=1)
    assert clean.verdict == "PASS"

    polluted = score_scenario(
        idle,
        case_id="t/m",
        traces=[
            RoundTrace(
                tool_names=[WIRE_TOOL_CALL],
                tool_call_targets=["core.schedule_command"],
                successful_calls=["core.schedule_command"],
            )
        ],
        rounds=1,
    )
    assert polluted.verdict == "FAIL"
    assert polluted.reason == "idle_tool_call"

    ok = score_scenario(
        need,
        case_id="t/m",
        traces=[
            RoundTrace(
                tool_names=[WIRE_TOOLS_SEARCH],
                search_queries=["reminder"],
            ),
            RoundTrace(
                tool_names=[WIRE_TOOL_CALL],
                tool_call_targets=["core.schedule_command"],
                successful_calls=["core.schedule_command"],
            ),
        ],
        rounds=2,
    )
    assert ok.verdict == "PASS"

    no_call = score_scenario(
        need,
        case_id="t/m",
        traces=[
            RoundTrace(
                tool_names=[WIRE_TOOLS_SEARCH],
                search_queries=["reminder"],
            )
        ],
        rounds=1,
    )
    assert no_call.verdict == "FAIL"
    assert no_call.reason == "no_tool_call"


def test_poc_summary_table_includes_totals() -> None:
    text = format_summary_table(
        [
            score_scenario(
                next(s for s in SCENARIOS if s.scenario_id == "idle"),
                case_id="openai/x",
                traces=[],
                rounds=1,
            )
        ]
    )
    assert "PASS=" in text
    assert "idle" in text
